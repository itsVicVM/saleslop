"""Append-only send journal (source of truth) + derived state snapshot + run lock.

Files in KIT_OUTREACH_STATE_DIR:
  journal.jsonl  one JSON event per line, fsync'ed; never rewritten
  state.json     derived snapshot for humans/other tools, rebuilt from the journal
  .lock          OS file lock, released automatically if the process dies

Events: attempt -> sent | failed (transient, address stays sendable) ; bounce ; paused ; resumed.
An `attempt` with no outcome (crash mid-send) counts as contacted: at-most-once delivery.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

JOURNAL = "journal.jsonl"
STATE = "state.json"
LOCK = ".lock"


class LockedError(Exception):
    pass


@dataclass
class MailboxStats:
    by_day: dict[str, int] = field(default_factory=dict)     # local day -> sends (incl. uncertain)
    sends: list[datetime] = field(default_factory=list)       # ts of each counted send
    bounces: list[datetime] = field(default_factory=list)
    paused: dict | None = None                                # last `paused` event, None if active
    resumed_at: datetime | None = None

    @property
    def first_day(self) -> str | None:
        days = [d for d, n in self.by_day.items() if n > 0]
        return min(days) if days else None


@dataclass
class View:
    contacted: dict[str, str] = field(default_factory=dict)  # email -> mailbox id
    bounced: set[str] = field(default_factory=set)
    uncertain: set[str] = field(default_factory=set)
    mailboxes: dict[str, MailboxStats] = field(default_factory=dict)
    last_mailbox: str | None = None
    bad_lines: int = 0

    def mb(self, mailbox_id: str) -> MailboxStats:
        return self.mailboxes.setdefault(mailbox_id, MailboxStats())


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Journal:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.path = self.dir / JOURNAL
        self._lock_fh = None
        self._replay()

    # ------------------------------------------------------------------ reading
    def _replay(self) -> None:
        self.pending: dict[str, dict] = {}
        self.view = View()
        self._torn_tail = False
        if not self.path.exists():
            return
        with self.path.open("rb") as fh:
            if fh.seek(0, os.SEEK_END) > 0:
                fh.seek(-1, os.SEEK_END)
                self._torn_tail = fh.read(1) != b"\n"
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    self.view.bad_lines += 1   # torn last line after a crash
                    continue
                self._apply(self.view, ev, self.pending)
        self._refresh_uncertain()

    def _refresh_uncertain(self) -> None:
        self.view.uncertain = {ev["email"] for ev in self.pending.values()}

    @staticmethod
    def _apply(view: View, ev: dict, pending: dict) -> None:
        kind = ev.get("event")
        if kind == "attempt":
            pending[ev["message_id"]] = ev
            st = view.mb(ev["mailbox"])
            st.by_day[ev["day"]] = st.by_day.get(ev["day"], 0) + 1
            st.sends.append(_ts(ev["ts"]))
            view.contacted[ev["email"]] = ev["mailbox"]
            view.last_mailbox = ev["mailbox"]
        elif kind == "sent":
            pending.pop(ev["message_id"], None)
        elif kind == "failed":
            att = pending.pop(ev["message_id"], None)
            if att:  # the server definitely did not take it: undo the attempt, address may be retried
                st = view.mb(att["mailbox"])
                st.by_day[att["day"]] -= 1
                st.sends.remove(_ts(att["ts"]))
                if view.contacted.get(att["email"]) == att["mailbox"]:
                    del view.contacted[att["email"]]
        elif kind == "error":
            pass  # outcome unknown: the attempt stays pending (= uncertain, never retried)
        elif kind == "bounce":
            if ev.get("message_id"):
                pending.pop(ev["message_id"], None)
            if ev["email"] in view.bounced:
                return
            view.bounced.add(ev["email"])
            if ev.get("mailbox"):
                view.mb(ev["mailbox"]).bounces.append(_ts(ev["ts"]))
        elif kind == "paused":
            view.mb(ev["mailbox"]).paused = ev
        elif kind == "resumed":
            st = view.mb(ev["mailbox"])
            st.paused = None
            st.resumed_at = _ts(ev["ts"])

    # ------------------------------------------------------------------ writing
    def append(self, event: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        if self._torn_tail:   # don't glue a new event onto a half-written line
            line = "\n" + line
            self._torn_tail = False
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self._apply(self.view, event, self.pending)
        self._refresh_uncertain()

    def write_snapshot(self, extra: dict | None = None) -> None:
        v = self.view
        snap = {
            "contacted": len(v.contacted), "bounced": len(v.bounced), "uncertain": sorted(v.uncertain),
            "mailboxes": {
                mid: {"by_day": st.by_day, "first_day": st.first_day, "sends": len(st.sends),
                      "bounces": len(st.bounces), "paused": st.paused}
                for mid, st in sorted(v.mailboxes.items())
            },
            **(extra or {}),
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (STATE + ".tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self.dir / STATE)

    # ------------------------------------------------------------------ lock
    def lock(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.dir / LOCK, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            raise LockedError(f"another outreach run holds {self.dir / LOCK}") from e
        self._lock_fh = fh
        try:
            self._replay()  # An earlier writer may have finished since this instance was opened.
        except BaseException:
            self.unlock()
            raise

    def unlock(self) -> None:
        if self._lock_fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._lock_fh.seek(0)
                msvcrt.locking(self._lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_fh.close()
            self._lock_fh = None


def bounce_rate(st: MailboxStats, now: datetime, window_days: int) -> tuple[int, int]:
    """(bounces, sends) inside the rolling window, not earlier than the last resume."""
    start = now - timedelta(days=window_days)
    if st.resumed_at and st.resumed_at > start:
        start = st.resumed_at
    return sum(1 for t in st.bounces if t >= start), sum(1 for t in st.sends if t >= start)
