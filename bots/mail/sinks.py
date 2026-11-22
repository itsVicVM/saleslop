"""Adapters of the mail bot to the rest of the kit: bounce notices -> kit/outreach journal, tracker rows from
Google Sheets (one read per 10 minutes, columns from sheets.columns of the pack)."""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from pathlib import Path


def outreach_bounce_sink(state_dir):
    """bounce_sink(email, source) -> True when written to the outreach journal, False while an outreach run holds
    the lock (the mail bot keeps the address queued and retries on the next poll)."""
    kit = str(Path(__file__).resolve().parents[2])
    if kit not in sys.path:
        sys.path.insert(0, kit)
    from outreach.engine import record_bounce
    from outreach.journal import Journal, LockedError

    def sink(email: str, source: str = "dsn") -> bool:
        journal = Journal(Path(state_dir))
        try:
            journal.lock()
        except LockedError:
            return False
        try:
            res = record_bounce(journal, email, dt.datetime.now(dt.timezone.utc), source=source)
            print(f"[mail] bounce {email}: {res}", file=sys.stderr)
            return True
        finally:
            journal.unlock()

    return sink


def _col(letter: str) -> int:
    n = 0
    for ch in letter.strip().upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def sheet_rows_provider(sheet_id: str, tab: str, columns: dict, ttl_s: int = 600, service=None):
    """rows_provider() for the mail bot. Needs the agent runtime's Google API module (KIT_GOOGLE_API_DIR), same as
    kit/bots/ops/followup.py. Missing columns simply stay empty."""
    cache = {"at": 0.0, "rows": []}
    idx = {k: _col(v) for k, v in (columns or {}).items() if k in ("email", "site", "company", "stage") and v}

    def provider():
        if time.time() - cache["at"] < ttl_s:
            return cache["rows"]
        svc = service
        if svc is None:
            sys.path.insert(0, os.environ.get("KIT_GOOGLE_API_DIR", ""))
            import google_api  # provided by the agent runtime's google-workspace skill
            svc = google_api.build_service("sheets", "v4")
        values = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"'{tab}'!A1:AZ20000").execute().get("values", [])
        cache["rows"] = [{k: (r[i] if i < len(r) else "") for k, i in idx.items()} for r in values[1:]]
        cache["at"] = time.time()
        return cache["rows"]

    return provider
