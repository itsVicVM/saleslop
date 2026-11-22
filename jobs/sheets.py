"""Google Sheets access for the jobs: one batchGet per read, every read booked against the per-minute read quota.

The Sheets API client comes from the agent runtime's google-workspace module (KIT_GOOGLE_API_DIR), the same way
kit/bots/ops/followup.py and kit/bots/mail/sinks.py get it. Tests pass a fake service instead.
"""
from __future__ import annotations

import os
import sys

from . import budget


def col_index(letter: str) -> int:
    n = 0
    for ch in str(letter).strip().upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def col_letter(index: int) -> str:
    out, n = "", index + 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def quote_tab(tab: str) -> str:
    return "'" + str(tab).replace("'", "''") + "'"


def build_service():
    sys.path.insert(0, os.environ.get("KIT_GOOGLE_API_DIR", ""))
    import google_api  # provided by the agent runtime's google-workspace skill
    return google_api.build_service("sheets", "v4")


class Sheets:
    def __init__(self, sheet_id: str, service=None, read_quota_per_min: int = 60, ledger=None):
        self.sheet_id = sheet_id
        self._service = service
        self.quota = int(read_quota_per_min)
        self.ledger = ledger
        self.reads = 0
        self.writes = 0

    @property
    def service(self):
        if self._service is None:
            self._service = build_service()
        return self._service

    def _book_read(self) -> None:
        if self.ledger is not None:
            self.ledger.take_minute("sheets_read", self.quota)
        self.reads += 1

    def batch_get(self, ranges: list[str], render: str = "FORMATTED_VALUE") -> list[list[list[str]]]:
        """Values of every range in ONE API request (one read against the quota)."""
        self._book_read()
        resp = self.service.spreadsheets().values().batchGet(
            spreadsheetId=self.sheet_id, ranges=list(ranges), valueRenderOption=render).execute()
        out = [vr.get("values", []) for vr in resp.get("valueRanges", [])]
        if len(out) != len(ranges):
            raise ValueError("incomplete Sheets read: expected one response for each requested range")
        return out

    def write_cells(self, cells: list[tuple[str, str]]) -> int:
        """[(range of ONE cell, value)] in one batchUpdate. Single-cell ranges only, so a write can never spill into
        a formula column next to it."""
        if not cells:
            return 0
        for rng, _ in cells:
            if ":" in rng:
                raise ValueError(f"write_cells takes single-cell ranges only, got {rng!r}")
        self.writes += 1
        resp = self.service.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={"valueInputOption": "RAW", "data": [{"range": r, "values": [[v]]} for r, v in cells]}).execute()
        return int(resp.get("totalUpdatedCells", len(cells)))


def ledger_for_reads():
    """Budget ledger for the read quota; None when the host has no ledger configured (reads are then not paced)."""
    path = budget.ledger_path_from_env()
    return budget.Ledger(path, {}) if path else None
