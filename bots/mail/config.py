"""Mail bot config: the "mail" block of KIT_OPS_CONFIG (built by kit_env.py --module ops from mail.* of the pack)
plus host env. The mail bot runs inside the ops bot process and shares its company/persona/LLM settings.

Host env (not in the pack):
  KIT_HOME                       deployment dir; state goes to KIT_HOME/mail_state/
  <env_prefix>_HIMALAYA_ACCOUNT  Himalaya account name of a himalaya mailbox (same convention as kit/outreach)
  KIT_MAIL_GMAIL_CLIENT_SECRET   path to the OAuth client JSON (from mail.bot.gmail_client_secret: env:NAME)
  KIT_MAIL_HIMALAYA_BIN          default "himalaya"
  KIT_MAIL_HIMALAYA_CONFIG       default ~/.config/himalaya/config.toml (only the connect CLI writes it)
  KIT_OUTREACH_STATE_DIR         when set, failed recipients of bounce notices go to the outreach journal
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PROVIDERS = ("himalaya", "gmail_api", "ms_graph")
HOSTED_PRESETS = (
    {"id": "yandex", "imap": "imaps://imap.yandex.com:993", "smtp": "smtps://smtp.yandex.com:465", "starttls": False},
    {"id": "zoho-us-business", "imap": "imaps://imappro.zoho.com:993", "smtp": "smtps://smtppro.zoho.com:465",
     "starttls": False},
)
REPLY_MODES = ("off", "draft")   # no "auto" for mail: every email goes out only after the Send button
ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")


class MailConfigError(Exception):
    pass


@dataclass
class Mailbox:
    id: str
    address: str
    provider: str
    env_prefix: str = ""
    sender_name: str = ""
    outreach: bool = False            # pack flag: may kit/outreach send from it (the mail bot only shows it)
    notify: bool = True               # pack default, runtime override in mail_state
    reply: str = "off"                # off: draft on button press; draft: draft for every incoming email
    source: str = "pack"              # pack | connect (added later with the connect CLI / bot)
    himalaya_account: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class MailConfig:
    mailboxes: list
    poll_seconds: int = 180
    presets: list = field(default_factory=list)
    tracker_columns: dict = field(default_factory=dict)
    tracker_tab: str = ""
    home: Path | None = None
    gmail_client_secret: str = ""
    himalaya_bin: str = "himalaya"
    himalaya_config: str = ""
    outreach_state_dir: str = ""

    @property
    def state_dir(self) -> Path:
        return self.home / "mail_state"

    def mailbox(self, mailbox_id: str):
        return next((m for m in self.mailboxes if m.id == mailbox_id), None)

    @classmethod
    def from_dict(cls, data: dict, *, home: Path, env=None) -> "MailConfig":
        env = os.environ if env is None else env
        if not isinstance(data, dict) or not data.get("enabled"):
            raise MailConfigError("mail bot is not enabled (mail.bot.enabled in the pack)")
        boxes, seen = [], set()
        for i, m in enumerate(data.get("mailboxes") or []):
            where = f"mail.mailboxes[{i}]"
            if not isinstance(m, dict) or not m.get("id") or not m.get("address"):
                raise MailConfigError(f"{where}: id and address are required")
            if m["id"] in seen:
                raise MailConfigError(f"{where}: duplicate id {m['id']!r}")
            seen.add(m["id"])
            provider = m.get("provider", "himalaya")
            if provider not in PROVIDERS:
                raise MailConfigError(f"{where}: provider must be one of {PROVIDERS}")
            reply = m.get("reply", "off")
            if reply not in REPLY_MODES:
                raise MailConfigError(f"{where}: reply must be one of {REPLY_MODES} (mail has no auto mode)")
            prefix = m.get("env_prefix", "")
            boxes.append(Mailbox(
                id=str(m["id"]), address=str(m["address"]).lower(), provider=provider, env_prefix=prefix,
                sender_name=m.get("sender_name", ""), outreach=bool(m.get("outreach", False)),
                notify=bool(m.get("notify", True)), reply=reply,
                himalaya_account=(env.get(f"{prefix}_HIMALAYA_ACCOUNT", "") if prefix else "")))
        poll = int(data.get("poll_seconds", 180))
        if poll < 30:
            raise MailConfigError("mail.bot.poll_seconds must be >= 30")
        tracker = data.get("tracker") or {}
        presets = list((data.get("connect") or {}).get("presets") or [])
        configured_ids = {p["id"] for p in presets}
        presets.extend(dict(p) for p in HOSTED_PRESETS if p["id"] not in configured_ids)
        return cls(mailboxes=boxes, poll_seconds=poll, presets=presets,
                   tracker_columns=dict(tracker.get("columns") or {}), tracker_tab=tracker.get("tab") or "",
                   home=Path(home), gmail_client_secret=env.get("KIT_MAIL_GMAIL_CLIENT_SECRET", ""),
                   himalaya_bin=env.get("KIT_MAIL_HIMALAYA_BIN", "himalaya"),
                   himalaya_config=env.get("KIT_MAIL_HIMALAYA_CONFIG", ""),
                   outreach_state_dir=env.get("KIT_OUTREACH_STATE_DIR", ""))

    def problems(self) -> list:
        """What will not work at runtime (printed by the connect CLI `check` and at bot start)."""
        out = []
        for m in self.mailboxes:
            if m.provider == "ms_graph":
                for suffix in ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET"):
                    name = f"{m.env_prefix}_{suffix}"
                    if not os.environ.get(name):
                        out.append(f"mailbox {m.id}: {name} is not set")
            if m.provider == "himalaya" and not m.himalaya_account:
                out.append(f"mailbox {m.id}: {m.env_prefix or '<env_prefix>'}_HIMALAYA_ACCOUNT is not set")
            if m.provider == "gmail_api" and not self.gmail_client_secret:
                out.append(f"mailbox {m.id}: KIT_MAIL_GMAIL_CLIENT_SECRET (mail.bot.gmail_client_secret) is not set")
        return out
