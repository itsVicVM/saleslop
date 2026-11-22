"""File-backed state of the mail bot under KIT_HOME/mail_state:
  mailboxes.json   runtime per mailbox: last_uid / seen_ids (poll cursor), notify/reply overrides, send_ok, alerts
  connected.json   mailboxes added later with the connect CLI or the bot (pack mailboxes are not copied here)
  cards/<id>.json  one email card
  prompts.json     ops-chat message id -> what the operator's reply to it means
  seen.jsonl / quiet.jsonl / sent.jsonl   logs (sent.jsonl is the record of every email sent by the bot)
  bounce_queue.jsonl  failed recipients not yet written to the outreach journal (it was locked)
  tokens/          Gmail OAuth tokens (chmod 600)
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

OPEN_CARD = ("new", "drafted", "awaiting_edit", "awaiting_text", "failed", "unknown", "sending")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class MailState:
    """Read-modify-write helpers hold one lock: the poll thread and button callbacks write the same files."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self._lock = threading.RLock()

    def _p(self, name: str) -> Path:
        return self.dir / name

    def load(self, name: str, default):
        try:
            return json.loads(self._p(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def save(self, name: str, data) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self._p(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)

    def append(self, name: str, record: dict) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self._p(name), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": _now_iso(), **record}, ensure_ascii=False) + "\n")

    def read_lines(self, name: str) -> list:
        try:
            lines = self._p(name).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    # ------------------------------------------------------------------ mailboxes runtime
    def box(self, mailbox_id: str) -> dict:
        return dict(self.load("mailboxes.json", {}).get(mailbox_id) or {})

    def update_box(self, mailbox_id: str, **kw) -> dict:
        with self._lock:
            data = self.load("mailboxes.json", {})
            cur = dict(data.get(mailbox_id) or {})
            cur.update(kw)
            data[mailbox_id] = cur
            self.save("mailboxes.json", data)
            return cur

    def drop_box(self, mailbox_id: str) -> None:
        with self._lock:
            data = self.load("mailboxes.json", {})
            data.pop(mailbox_id, None)
            self.save("mailboxes.json", data)

    # ------------------------------------------------------------------ cards
    def card_path(self, sid: str) -> Path:
        return self._p("cards") / f"{''.join(c for c in sid if c.isalnum())}.json"

    def save_card(self, card: dict) -> None:
        with self._lock:
            self.save(str(self.card_path(card["id"]).relative_to(self.dir)), card)

    def load_card(self, sid: str):
        try:
            return json.loads(self.card_path(sid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def cards(self) -> list:
        d = self._p("cards")
        if not d.exists():
            return []
        out = []
        for p in sorted(d.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    # ------------------------------------------------------------------ prompts (force-reply messages)
    def prompts(self) -> dict:
        return self.load("prompts.json", {})

    def set_prompt(self, message_id, payload: dict) -> None:
        with self._lock:
            p = self.prompts()
            p[str(message_id)] = payload
            self.save("prompts.json", p)

    def pop_prompt(self, message_id):
        with self._lock:
            p = self.prompts()
            val = p.pop(str(message_id), None)
            self.save("prompts.json", p)
            return val

    def drop_prompts(self, keep: dict) -> None:
        """Rewrite prompts.json with only `keep` (expired prompts removed)."""
        with self._lock:
            self.save("prompts.json", keep)
