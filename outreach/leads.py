"""Lead sources: a CSV file or one tab of the tracker spreadsheet.

The tab is read once per run (Sheets quota: 60 reads/min per project). Preferred path is
the Hermes google-workspace skill (`google_api`, imported lazily, same as
write_tracker_results.py); fallback is the CSV export of a link-shared sheet.
Physical row numbers (header = row 1) are kept for reporting.
"""
from __future__ import annotations

import csv
import io
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import EMAIL_RE

COLUMN_LETTER_RE = re.compile(r"[A-Z]{1,3}")


@dataclass
class Lead:
    row: int
    email: str          # normalized (lowercase, stripped); "" if absent
    first_name: str
    company: str
    template: str = ""
    raw_email: str = ""

    @property
    def valid_email(self) -> bool:
        return bool(EMAIL_RE.fullmatch(self.email))


class LeadSourceError(Exception):
    pass


def normalize_email(value: str) -> str:
    value = (value or "").strip().strip("<>").strip()
    if value.lower().startswith("mailto:"):
        value = value[7:]
    return value.split("?", 1)[0].strip().lower()


def col_to_idx(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def rows_to_leads(rows: list[list[str]], columns: dict[str, str]) -> list[Lead]:
    """rows[0] is the header. Column mapping values are header names or 'col:<LETTER>'."""
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    lower = [h.lower() for h in header]

    def index(key: str, required: bool) -> int | None:
        spec = columns.get(key)
        if not spec:
            return None
        if spec.startswith("col:"):
            letter = spec[4:].strip().upper()
            if not COLUMN_LETTER_RE.fullmatch(letter):
                raise LeadSourceError(f"column {key}: {spec!r} is not a column letter")
            return col_to_idx(letter)
        if spec.lower() in lower:
            return lower.index(spec.lower())
        if required:
            raise LeadSourceError(f"column {key}: header {spec!r} not found (have: {header})")
        return None

    idx = {k: index(k, required=(k == "email")) for k in ("email", "first_name", "company", "template")}

    def cell(row: list[str], key: str) -> str:
        i = idx[key]
        return row[i].strip() if i is not None and i < len(row) else ""

    leads = []
    for n, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        raw = cell(row, "email")
        leads.append(Lead(row=n, email=normalize_email(raw), first_name=cell(row, "first_name"),
                          company=cell(row, "company"), template=cell(row, "template"), raw_email=raw))
    return leads


def read_csv(path: Path) -> list[list[str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as e:
        raise LeadSourceError(f"leads file not found: {path}") from e
    return list(csv.reader(io.StringIO(text)))


def _fetch_url(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (fixed https host)
        return resp.read().decode("utf-8-sig")


def read_sheet_tab(sheet_id: str, tab: str, fetch: Callable[[str], str] | None = None) -> list[list[str]]:
    """One read of the whole tab. `fetch` is injectable for tests."""
    if fetch is None:
        try:
            import google_api  # type: ignore  # Hermes google-workspace skill
        except ImportError:
            google_api = None
        if google_api is not None:
            service = google_api.build_service("sheets", "v4")
            resp = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"'{tab}'").execute()
            return [[str(c) for c in r] for r in resp.get("values", [])]
        fetch = _fetch_url
    url = (f"https://docs.google.com/spreadsheets/d/{urllib.parse.quote(sheet_id)}/gviz/tq"
           f"?tqx=out:csv&sheet={urllib.parse.quote(tab)}")
    try:
        text = fetch(url)
    except OSError as e:
        raise LeadSourceError(f"cannot read tab {tab!r}: {e} (private sheet? configure google-workspace "
                              f"OAuth or export the tab to CSV)") from e
    return list(csv.reader(io.StringIO(text)))


def load_leads(spec: str, columns: dict[str, str], sheet_id: str | None = None,
               fetch: Callable[[str], str] | None = None) -> list[Lead]:
    """spec: 'csv:<path>' or 'sheet:<tab name>' (spreadsheet id from SHEET_ID)."""
    kind, _, value = spec.partition(":")
    if kind == "csv":
        return rows_to_leads(read_csv(Path(value)), columns)
    if kind == "sheet":
        if not sheet_id:
            raise LeadSourceError("sheet source needs SHEET_ID (set by kit_env from sheets.spreadsheet_id)")
        return rows_to_leads(read_sheet_tab(sheet_id, value, fetch), columns)
    raise LeadSourceError(f"unknown leads source {spec!r} (csv:<path> | sheet:<tab>)")


def load_suppression(path: Path | None) -> set[str]:
    """One address per line (or first CSV cell); '#' comments allowed. Opt-outs, do-not-contact."""
    if not path:
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(normalize_email(line.split(",", 1)[0]))
    return out
