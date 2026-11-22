"""Transports behind one interface: send(mailbox, message).

- smtp:     stdlib smtplib, secrets per mailbox from <env_prefix>_SMTP_* env vars
- himalaya: Himalaya CLI, raw message on stdin, account from <env_prefix>_HIMALAYA_ACCOUNT
- outbox:   writes .eml files to a directory (demos/e2e; never leaves the host)

Errors: RecipientRejected = the server refused this recipient (counted as a bounce);
MailboxError = the mailbox can't send right now (auth, network, policy) - the run stops
using it, the lead stays sendable.
"""
from __future__ import annotations

import shlex
import smtplib
import subprocess
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .config import Mailbox, OutreachConfig


class TransportError(Exception):
    pass


class RecipientRejected(TransportError):
    pass


class MailboxError(TransportError):
    """retryable=True: the server definitely did not take the message (connect/login failed,
    or it answered with an SMTP error code). retryable=False: outcome unknown (connection
    dropped mid-send, CLI failed after possibly sending) - the lead is treated as contacted."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class Transport(Protocol):
    name: str
    real: bool

    def send(self, mailbox: Mailbox, msg: EmailMessage) -> None: ...


def build_message(mailbox: Mailbox, to: str, subject: str, body: str, now: datetime) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((mailbox.sender_name, mailbox.address))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = format_datetime(now)
    msg["Message-ID"] = make_msgid(domain=mailbox.domain)
    msg.set_content(body)
    return msg


class SmtpTransport:
    name = "smtp"
    real = True

    def __init__(self, env: Mapping[str, str], smtp_module=smtplib, timeout: float = 60):
        self.env = env
        self.smtp = smtp_module
        self.timeout = timeout

    def _settings(self, mb: Mailbox) -> dict:
        p = mb.env_prefix
        security = self.env.get(f"{p}_SMTP_SECURITY", "ssl")
        return {
            "host": self.env[f"{p}_SMTP_HOST"],
            "port": int(self.env.get(f"{p}_SMTP_PORT") or (465 if security == "ssl" else 587)),
            "user": self.env.get(f"{p}_SMTP_USER") or mb.address,
            "password": self.env[f"{p}_SMTP_PASSWORD"],
            "security": security,
        }

    def send(self, mailbox: Mailbox, msg: EmailMessage) -> None:
        s = self._settings(mailbox)
        try:
            if s["security"] == "ssl":
                conn = self.smtp.SMTP_SSL(s["host"], s["port"], timeout=self.timeout)
            else:
                conn = self.smtp.SMTP(s["host"], s["port"], timeout=self.timeout)
        except (smtplib.SMTPException, OSError) as e:
            raise MailboxError(f"connect: {type(e).__name__}: {_short(e)}") from e
        with conn:
            try:
                if s["security"] == "starttls":
                    conn.starttls()
                conn.login(s["user"], s["password"])
            except (smtplib.SMTPException, OSError) as e:
                raise MailboxError(f"login: {type(e).__name__}: {_short(e)}") from e
            try:
                refused = conn.send_message(msg)
            except smtplib.SMTPRecipientsRefused as e:
                if any(400 <= code < 500 for code, _ in e.recipients.values()):
                    raise MailboxError(f"temporary recipient refusal: {_short(e.recipients)}") from e
                raise RecipientRejected(_short(e.recipients)) from e
            except smtplib.SMTPResponseException as e:
                raise MailboxError(f"{e.smtp_code} {_short(e.smtp_error)}") from e
            except (smtplib.SMTPException, OSError) as e:
                # connection lost mid-transaction: the server may have accepted the message
                raise MailboxError(f"send: {type(e).__name__}: {_short(e)}", retryable=False) from e
        if refused:
            raise RecipientRejected(_short(refused))


class HimalayaTransport:
    name = "himalaya"
    real = True

    def __init__(self, env: Mapping[str, str], cmd_template: str,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run, timeout: float = 120):
        self.env = env
        self.cmd_template = cmd_template
        self.runner = runner
        self.timeout = timeout

    def argv(self, mailbox: Mailbox) -> list[str]:
        account = self.env[f"{mailbox.env_prefix}_HIMALAYA_ACCOUNT"]
        return [part.replace("{account}", account) for part in shlex.split(self.cmd_template)]

    def send(self, mailbox: Mailbox, msg: EmailMessage) -> None:
        try:
            res = self.runner(self.argv(mailbox), input=msg.as_bytes(), capture_output=True, timeout=self.timeout)
        except subprocess.SubprocessError as e:
            raise MailboxError(f"himalaya: {type(e).__name__}: {_short(e)}", retryable=False) from e
        except OSError as e:
            raise MailboxError(f"himalaya: {type(e).__name__}: {_short(e)}") from e
        if res.returncode != 0:
            # himalaya may fail after the SMTP send (e.g. saving to Sent): outcome unknown
            raise MailboxError(f"himalaya exit {res.returncode}: {_short(res.stderr or '')}", retryable=False)


class OutboxTransport:
    name = "outbox"
    real = False

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def send(self, mailbox: Mailbox, msg: EmailMessage) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        mid = msg["Message-ID"].strip("<>").replace("@", "_at_")
        (self.dir / f"{mailbox.id}_{mid}.eml").write_bytes(msg.as_bytes())


def make_transport(cfg: OutreachConfig, env: Mapping[str, str]) -> Transport:
    if cfg.transport == "smtp":
        return SmtpTransport(env)
    if cfg.transport == "himalaya":
        return HimalayaTransport(env, cfg.himalaya_cmd)
    if cfg.transport == "outbox":
        return OutboxTransport(cfg.outbox_dir)
    raise ValueError(f"unknown transport {cfg.transport!r}")


def _short(obj, limit: int = 300) -> str:
    text = obj.decode("utf-8", "replace") if isinstance(obj, bytes) else str(obj)
    text = " ".join(text.split())
    return text[:limit]
