"""Himalaya CLI backend: list the INBOX, read raw messages, send raw MIME, add/remove an account section.

Tests inject a subprocess runner. Before connecting an account, check `himalaya --version`
and `himalaya envelope list --help` against LIST_ARGS, READ_ARGS and SEND_ARGS below.

Known pitfall: `message send` exits non-zero when saving to the Sent folder fails AFTER the SMTP server accepted
the message (wrong folder alias). A non-zero exit is therefore "outcome unknown", never a safe "retry".
"""
from __future__ import annotations

import json
import os
import re
import sys
import shutil
import subprocess
from pathlib import Path

LIST_ARGS = ["envelope", "list", "-a", "{account}", "-s", "{size}", "--json"]
READ_ARGS = ["message", "read", "-a", "{account}", "--raw", "{uid}"]
SEND_ARGS = ["message", "send", "-a", "{account}"]


def default_runner(argv, timeout=60, input_bytes=None):
    p = subprocess.run(argv, capture_output=True, timeout=timeout, input=input_bytes)
    return p.returncode, p.stdout or b"", p.stderr or b""


def _fill(args, **kw):
    return [a.format(**kw) for a in args]


def _host_port(url: str, default_port: int) -> tuple[str, int]:
    m = re.match(r"(?:[a-z]+://)?([^:/]+)(?::(\d+))?", url or "")
    if not m:
        raise ValueError(f"not a server URL: {url!r}")
    return m.group(1), int(m.group(2) or default_port)


class Himalaya:
    def __init__(self, binary: str = "himalaya", runner=None, config_path: str | None = None):
        self.binary = binary
        self.run = runner or default_runner
        self.config_path = Path(os.path.expanduser(config_path or "~/.config/himalaya/config.toml"))

    # ------------------------------------------------------------------ reading
    def list_ids(self, account: str, size: int = 40) -> list[int] | None:
        """INBOX ids, ascending. None when the command failed (the caller logs and retries next poll)."""
        code, out, err = self.run([self.binary] + _fill(LIST_ARGS, account=account, size=size), timeout=90)
        if code != 0:
            print(f"[mail] list {account} failed: {err.decode('utf-8', 'replace')[-150:]}", file=sys.stderr)
            return None
        try:
            data = json.loads(out.decode("utf-8", "replace"))
        except ValueError:
            return None
        envs = data.get("envelopes", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        return sorted(int(e["id"]) for e in envs if isinstance(e, dict) and str(e.get("id", "")).isdigit())

    def read_raw(self, account: str, uid) -> bytes | None:
        code, out, err = self.run([self.binary] + _fill(READ_ARGS, account=account, uid=uid), timeout=60)
        if code != 0 or not out:
            print(f"[mail] read {account}/{uid} failed: {err.decode('utf-8', 'replace')[-150:]}", file=sys.stderr)
            return None
        return out

    # ------------------------------------------------------------------ sending
    def send_raw(self, account: str, raw: bytes) -> tuple[str, str]:
        """('sent'|'unknown', detail). Never 'failed': Himalaya does not tell a refused message from a failed
        save-to-Sent, so a non-zero exit may still mean the recipient got it."""
        try:
            code, out, err = self.run([self.binary] + _fill(SEND_ARGS, account=account), timeout=90, input_bytes=raw)
        except (OSError, subprocess.SubprocessError) as e:
            return "unknown", str(e)[:300]
        if code == 0:
            return "sent", ""
        return "unknown", (err or out).decode("utf-8", "replace")[-300:]

    # ------------------------------------------------------------------ config (no secrets are read)
    def config_accounts(self) -> dict:
        """{account: {"email", "smtp"}} from the TOML config; understands `smtp.server = "smtp://host:port"` and the
        older `message.send.backend.host/port` keys."""
        try:
            txt = self.config_path.read_text(encoding="utf-8")
        except OSError:
            return {}
        out, cur = {}, None
        for line in txt.splitlines():
            m = re.match(r"\s*\[accounts\.([^\].]+)\]\s*$", line)
            if m:
                cur = m.group(1).strip('"')
                out[cur] = {}
                continue
            if re.match(r"\s*\[", line):
                cur = None
                continue
            if not cur:
                continue
            m = re.match(r'\s*(email|smtp\.server|message\.send\.backend\.host|message\.send\.backend\.port)\s*=\s*"?([^"\n]*)"?', line)
            if m:
                out[cur][m.group(1)] = m.group(2).strip()
        res = {}
        for acc, kv in out.items():
            smtp = kv.get("smtp.server", "")
            if not smtp and kv.get("message.send.backend.host"):
                smtp = f"smtp://{kv['message.send.backend.host']}:{kv.get('message.send.backend.port', '587')}"
            res[acc] = {"email": kv.get("email", ""), "smtp": smtp}
        return res

    def config_layout(self) -> str:
        """'backend' when the existing config uses backend.* / message.send.backend.* keys, else 'url'
        (imap.server / smtp.server URLs). Releases differ; a new section follows what the file already uses."""
        try:
            txt = self.config_path.read_text(encoding="utf-8")
        except OSError:
            return "url"
        return "backend" if re.search(r"^\s*backend\.type\s*=", txt, re.M) else "url"

    def add_account(self, account: str, address: str, imap: str, smtp: str, starttls: bool, secret_path: Path) -> None:
        """Appends one [accounts.NAME] section (password read by `cat <secret file>`; the file is created by the
        caller, chmod 600). A backup of the config is kept next to it."""
        if self.config_path.exists():
            shutil.copy(self.config_path, str(self.config_path) + f".bak.pre-{account}")
        if self.config_layout() == "backend":
            ih, ip = _host_port(imap, 993)
            sh, sp = _host_port(smtp, 587 if starttls else 465)
            section = (f'\n[accounts.{account}]\nemail = "{address}"\n'
                       f'backend.type = "imap"\nbackend.host = "{ih}"\nbackend.port = {ip}\nbackend.encryption.type = "tls"\n'
                       f'backend.login = "{address}"\nbackend.auth.type = "password"\nbackend.auth.cmd = "cat {secret_path}"\n'
                       f'message.send.backend.type = "smtp"\nmessage.send.backend.host = "{sh}"\nmessage.send.backend.port = {sp}\n'
                       f'message.send.backend.encryption.type = "{"start-tls" if starttls else "tls"}"\n'
                       f'message.send.backend.login = "{address}"\nmessage.send.backend.auth.type = "password"\n'
                       f'message.send.backend.auth.cmd = "cat {secret_path}"\nfolder.aliases.inbox = "INBOX"\n')
        else:
            section = (f'\n[accounts.{account}]\nemail = "{address}"\n'
                       f'imap.server = "{imap}"\nimap.sasl.plain.username = "{address}"\n'
                       f'imap.sasl.plain.password.command = "cat {secret_path}"\n'
                       f'smtp.server = "{smtp}"\n' + ("smtp.starttls = true\n" if starttls else "")
                       + f'smtp.sasl.login.username = "{address}"\nsmtp.sasl.login.password.command = "cat {secret_path}"\n'
                       'folder.aliases.inbox = "INBOX"\n')
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "a", encoding="utf-8") as fh:
            fh.write(section)

    def remove_account(self, account: str) -> bool:
        try:
            cfg = self.config_path.read_text(encoding="utf-8")
        except OSError:
            return False
        new = re.sub(r"\n?\[accounts\." + re.escape(account) + r"\]\n(?:(?!\[accounts\.).*\n?)*", "\n", cfg)
        if new == cfg:
            return False
        shutil.copy(self.config_path, str(self.config_path) + f".bak.del-{account}")
        self.config_path.write_text(new, encoding="utf-8")
        return True

    def has_account(self, account: str) -> bool:
        return account in self.config_accounts()
