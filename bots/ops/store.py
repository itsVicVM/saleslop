"""File-backed state under KIT_HOME/ops_state: pending reply items (one JSON per item), settings overrides,
mutes, human takeovers, daily auto-send counters, CRM links, the outreach switch. Plus the shared send log (JSONL)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .textutil import is_muted, normalize_handle

OPEN_STATUSES = ("waiting", "drafted", "awaiting_approval", "awaiting_edit_text", "awaiting_own_text")
OUTREACH_OFF_FILE = "outreach_off.json"   # {"telegram": [account, ...], "mail": [mailbox id, ...]}


def outreach_off_path(home: Path) -> Path:
    return Path(home) / "ops_state" / OUTREACH_OFF_FILE


def runtime_outreach_off(home, channel: str) -> set:
    """Accounts/mailboxes the operator switched off for outreach in the ops bot. Read by sender.py, followup.py,
    the mail bot and (through KIT_OUTREACH_OFF_FILE) kit/outreach. Unreadable file = nothing switched off:
    the pack-level flag still applies on its own."""
    try:
        data = json.loads(outreach_off_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    return {str(x) for x in (data.get(channel) or [])} if isinstance(data, dict) else set()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Store:
    def __init__(self, state_dir: Path, send_log_path: Path):
        self.dir = Path(state_dir)
        self.pending_dir = self.dir / "pending_replies"
        self.send_log_path = Path(send_log_path)

    def ensure(self) -> None:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.send_log_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ json helpers
    def load(self, name: str, default):
        p = self.dir / name
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def save(self, name: str, data) -> None:
        self.ensure()
        p = self.dir / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    # ------------------------------------------------------------------ pending items
    def item_path(self, item_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in item_id)
        return self.pending_dir / f"{safe}.json"

    def save_item(self, item: dict) -> None:
        self.save(str(self.item_path(item["id"]).relative_to(self.dir)), item)

    def load_item(self, item_id: str):
        p = self.item_path(item_id)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def items(self):
        if not self.pending_dir.exists():
            return []
        out = []
        for p in sorted(self.pending_dir.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def find_item(self, **match):
        for it in self.items():
            if all(it.get(k) == v for k, v in match.items()):
                return it
        return None

    def find_by_ops_message(self, message_id):
        for it in self.items():
            if message_id and message_id in (it.get("card_message_id"), it.get("prompt_message_id")):
                return it
        return None

    # ------------------------------------------------------------------ settings (runtime overrides of the pack)
    def settings(self) -> dict:
        return self.load("settings.json", {})

    def update_settings(self, **kw) -> dict:
        s = self.settings()
        s.update(kw)
        self.save("settings.json", s)
        return s

    # ------------------------------------------------------------------ outreach switch (runtime, can only narrow the pack)
    def outreach_off(self, channel: str) -> set:
        return {str(x) for x in (self.load(OUTREACH_OFF_FILE, {}).get(channel) or [])}

    def set_outreach_off(self, channel: str, ident: str, off: bool) -> None:
        data = self.load(OUTREACH_OFF_FILE, {})
        cur = set(data.get(channel) or [])
        cur = cur | {ident} if off else cur - {ident}
        data[channel] = sorted(cur)
        self.save(OUTREACH_OFF_FILE, data)

    # ------------------------------------------------------------------ mutes
    def is_muted(self, peer: str) -> bool:
        return is_muted(self.load("muted_peers.json", {"handles": []}).get("handles", []), normalize_handle(peer))

    def mute(self, peer: str, until: dt.datetime | None = None) -> None:
        data = self.load("muted_peers.json", {"handles": []})
        h = normalize_handle(peer)
        entry = {"handle": h, "until": until.isoformat()} if until else h
        if not is_muted(data["handles"], h):
            data["handles"].append(entry)
        self.save("muted_peers.json", data)

    # ------------------------------------------------------------------ human takeover
    def set_takeover(self, peer: str, until: dt.datetime) -> None:
        data = self.load("takeover.json", {})
        data[normalize_handle(peer)] = until.isoformat()
        self.save("takeover.json", data)

    def takeover_until(self, peer: str, now: dt.datetime):
        until = self.load("takeover.json", {}).get(normalize_handle(peer))
        if not until:
            return None
        u = dt.datetime.fromisoformat(until)
        return u if u > now else None

    # ------------------------------------------------------------------ daily auto-send counter
    def auto_sent_today(self, day: str) -> int:
        return int(self.load("auto_sent.json", {}).get(day, 0))

    def count_auto_sent(self, day: str) -> int:
        data = self.load("auto_sent.json", {})
        data = {k: v for k, v in data.items() if k >= day}  # keep only today
        data[day] = int(data.get(day, 0)) + 1
        self.save("auto_sent.json", data)
        return data[day]

    # ------------------------------------------------------------------ CRM links (confirmed by the operator)
    def crm_link(self, peer: str):
        return self.load("crm_links.json", {}).get(normalize_handle(peer))

    def set_crm_link(self, peer: str, lead_id) -> None:
        data = self.load("crm_links.json", {})
        data[normalize_handle(peer)] = lead_id
        self.save("crm_links.json", data)

    # ------------------------------------------------------------------ send log (shared with sender.py)
    def log(self, record: dict) -> None:
        self.ensure()
        rec = {"ts": _now().isoformat(), "channel": "telegram", **record}
        with open(self.send_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def log_rows(self):
        if not self.send_log_path.exists():
            return
        with open(self.send_log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def has_prior(self, peer: str, direction: str, account: str | None = None) -> bool:
        want = normalize_handle(peer)
        for r in self.log_rows():
            if r.get("channel", "telegram") != "telegram" or r.get("direction") != direction:
                continue
            if account and r.get("account") != account:
                continue
            if direction == "in" and r.get("kind") in ("bot", "autoreply"):
                continue
            if normalize_handle(r.get("peer")) == want:
                return True
        return False

    def outgoing_logged(self, account: str, message_id) -> bool:
        for r in self.log_rows():
            if r.get("direction") == "out" and r.get("account") == account and str(r.get("message_id")) == str(message_id):
                return True
        return False

    def history(self, peer: str, limit: int):
        want = normalize_handle(peer)
        rows = [(str(r.get("ts", ""))[:16].replace("T", " "), r.get("direction"), r.get("text") or "")
                for r in self.log_rows() if normalize_handle(r.get("peer")) == want and r.get("kind") not in ("bot", "autoreply")]
        return rows[-limit:]
