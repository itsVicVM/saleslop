#!/usr/bin/env python3
"""Write found Telegram/email contacts back into the operator's tracker sheet.

Match each result by normalized site URL. Rows can move during manual editing.
Only fill empty Telegram and email cells so existing contacts are preserved.

Column letters come from the brand pack (sheets.columns), passed in by the kit
runner as KIT_COL_SITE / KIT_COL_TELEGRAM / KIT_COL_EMAIL, or via --col-* flags.

Usage:
    python3 write_tracker_results.py --sheet-id <id> --tab "<tab name>" \
        --telegram-csv telegram_contacts.csv --email-json email_contacts.json
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
GOOGLE_API_DIR = HERMES_HOME / "skills" / "productivity" / "google-workspace" / "scripts"

COLUMN_KEYS = ("site", "telegram", "email")


def col_to_idx(col: str) -> int:
    """'A' -> 0, 'Z' -> 25, 'AA' -> 26. Raises ValueError on anything else."""
    col = (col or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", col):
        raise ValueError(f"bad column letter: {col!r}")
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def resolve_columns(args) -> dict:
    """Flag > env (KIT_COL_*, set by kit/runner/kit_env.py from sheets.columns).
    No built-in default: a client's tracker layout must be configured explicitly."""
    cols = {}
    for key in COLUMN_KEYS:
        value = getattr(args, f"col_{key}", None) or os.environ.get(f"KIT_COL_{key.upper()}")
        if not value:
            raise SystemExit(f"tracker column for {key!r} is not configured: set sheets.columns.{key} "
                             f"in the brand pack (KIT_COL_{key.upper()}) or pass --col-{key}")
        cols[key] = value.strip().upper()
        col_to_idx(cols[key])  # validate early
    if len(set(cols.values())) != len(cols):
        raise SystemExit(f"tracker columns must be distinct, got {cols}")
    return cols


def normalize_site(raw: str) -> str:
    raw = (raw or "").strip().lower()
    raw = re.sub(r"^https?://", "", raw)
    raw = re.sub(r"^www\.", "", raw)
    return raw.rstrip("/").split("/")[0]


def load_telegram_results(csv_path: Path) -> dict:
    if not csv_path.exists():
        return {}
    out = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            domain = normalize_site(row.get("root_domain") or row.get("input_site") or "")
            handle = (row.get("telegram_url") or "").strip()
            if domain and handle and not out.get(domain):
                out[domain] = handle
    return out


def load_email_results(email_json: Path) -> tuple[dict, dict]:
    """Only mailbox-verified contacts enter the outreach column; other results stay in the research JSON."""
    if not email_json.exists():
        return {}, {}
    try:
        data = json.loads(email_json.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    emails, tiers = {}, {}
    for domain, entry in data.items():
        contacts = entry.get("contacts") or []
        if not contacts:
            continue
        status = (entry.get("reoon") or {}).get("status")
        if status != "valid":
            continue
        d = normalize_site(domain)
        emails[d] = contacts[0]["email"]
        tiers[d] = "green"
    return emails, tiers


def load_color_map(path: Path) -> dict:
    """Read optional colors by domain. Unlisted domains use the default green format."""
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


TIER_RGB = {
    "green": {"red": 0.72, "green": 0.90, "blue": 0.72},
    "yellow": {"red": 1.0, "green": 0.95, "blue": 0.65},
    "red": {"red": 0.96, "green": 0.75, "blue": 0.75},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet-id", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--telegram-csv", required=True)
    ap.add_argument("--email-json", required=True)
    ap.add_argument("--color-map", help="optional JSON file: {domain: 'green'|'yellow'|'red'}")
    ap.add_argument("--col-site", help="site column letter (default: KIT_COL_SITE)")
    ap.add_argument("--col-telegram", help="Telegram column letter (default: KIT_COL_TELEGRAM)")
    ap.add_argument("--col-email", help="email column letter (default: KIT_COL_EMAIL)")
    args = ap.parse_args()
    cols = resolve_columns(args)
    col_site, col_tg, col_email = cols["site"], cols["telegram"], cols["email"]

    tg_results = load_telegram_results(Path(args.telegram_csv))
    email_results, email_tier_map = load_email_results(Path(args.email_json))
    color_map = load_color_map(Path(args.color_map)) if args.color_map else {}
    all_domains = set(tg_results) | set(email_results)

    sys.path.insert(0, str(GOOGLE_API_DIR))
    import google_api  # Hermes google-workspace skill; imported lazily so helpers stay testable

    service = google_api.build_service("sheets", "v4")

    site_col_idx = col_to_idx(col_site)
    tg_col_idx = col_to_idx(col_tg)
    email_col_idx = col_to_idx(col_email)
    last_col = max((col_site, col_tg, col_email), key=col_to_idx)

    tab_ref = "'" + args.tab.replace("'", "''") + "'!"
    # Use the latest row positions when preparing the batch.
    resp = service.spreadsheets().values().get(
        spreadsheetId=args.sheet_id, range=f"{tab_ref}A2:{last_col}30000"
    ).execute()
    rows = resp.get("values", [])

    writes = []  # (range, value, domain, field, row_num, col_idx)
    matched = 0
    for i, row in enumerate(rows):
        row_num = i + 2
        site = row[site_col_idx] if len(row) > site_col_idx else ""
        domain = normalize_site(site)
        if not domain or domain not in all_domains:
            continue
        matched += 1
        current_tg = row[tg_col_idx] if len(row) > tg_col_idx else ""
        current_email = row[email_col_idx] if len(row) > email_col_idx else ""
        if domain in tg_results and not current_tg.strip():
            writes.append((f"{tab_ref}{col_tg}{row_num}", tg_results[domain], domain, "telegram", row_num, tg_col_idx))
        if domain in email_results and not current_email.strip():
            writes.append((f"{tab_ref}{col_email}{row_num}", email_results[domain], domain, "email", row_num, email_col_idx))

    if not writes:
        print(json.dumps({"ok": True, "matched_rows": matched, "written": 0, "note": "nothing to write (already filled or no rows matched)"}))
        return 0

    data = [{"range": r, "values": [[v]]} for r, v, _, _, _, _ in writes]
    body = {"valueInputOption": "RAW", "data": data}
    service.spreadsheets().values().batchUpdate(spreadsheetId=args.sheet_id, body=body).execute()

    time.sleep(1)
    # Re-read the written cells with one batchGet request to limit Sheets API usage.
    mismatches = 0
    written_domains = set()
    written_cells = []  # (row_num, col_idx, domain, field) for cells that verified ok
    changed_rows = sorted({row for _, _, _, _, row, _ in writes})
    all_ranges = [f"{tab_ref}A{row}:{last_col}{row}" for row in changed_rows]
    verify_resp = service.spreadsheets().values().batchGet(
        spreadsheetId=args.sheet_id, ranges=all_ranges
    ).execute()
    value_ranges = verify_resp.get("valueRanges", [])
    if len(value_ranges) != len(changed_rows):
        print(json.dumps({"ok": False, "error": "incomplete readback; inspect the tracker before retrying",
                          "cells_submitted": len(writes), "cells_verified": 0}))
        return 1
    verified_rows = {row: (vr.get("values") or [[]])[0] for row, vr in zip(changed_rows, value_ranges)}
    for cell_range, expected, domain, field, row_num, col_idx in writes:
        row = verified_rows[row_num]
        if (len(row) <= max(site_col_idx, col_idx) or normalize_site(str(row[site_col_idx])) != domain
                or row[col_idx] != expected):
            mismatches += 1
            print(f"Readback mismatch at {cell_range}; review the row before retrying.", file=sys.stderr)
        else:
            written_domains.add(domain)
            written_cells.append((row_num, col_idx, domain, field))

    # Formatting is best effort after the values have been written and read back.
    if color_map or email_tier_map:
        try:
            meta = service.spreadsheets().get(spreadsheetId=args.sheet_id).execute()
            sheet_id_num = next(
                s["properties"]["sheetId"] for s in meta["sheets"]
                if s["properties"]["title"] == args.tab
            )
            color_requests = []
            for row_num, col_idx, domain, field in written_cells:
                tier = email_tier_map.get(domain, "green") if field == "email" else color_map.get(domain, "green")
                rgb = TIER_RGB.get(tier, TIER_RGB["green"])
                color_requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id_num, "startRowIndex": row_num - 1, "endRowIndex": row_num,
                            "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1,
                        },
                        "cell": {"userEnteredFormat": {"backgroundColor": rgb}},
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                })
            if color_requests:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=args.sheet_id, body={"requests": color_requests}
                ).execute()
        except Exception as e:
            print(f"color coding failed (non-fatal, data already written): {e}", file=sys.stderr)

    print(json.dumps({
        "ok": mismatches == 0,
        "matched_rows": matched,
        "cells_written": len(writes) - mismatches,
        "mismatches": mismatches,
        "domains_updated": len(written_domains),
    }, ensure_ascii=False))
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
