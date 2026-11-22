#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

REQUIRED = [
    "company", "website", "telegram_handle", "entity_type", "route_status",
    "person_role_status", "confidence", "evidence_url", "evidence_text",
    "checked_at", "decision", "rejection_reason", "notes",
]
HANDLE_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")
ALLOWED_DECISIONS = {"accepted", "no_public_contact", "reject", "quarantine"}
ALLOWED_ACCEPTED_TYPES = {"direct_user", "public_group"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate evidence-first Telegram contact CSV")
    ap.add_argument("csv_path")
    args = ap.parse_args()
    path = Path(args.csv_path)
    errors: list[str] = []
    warnings: list[str] = []

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        missing = [x for x in REQUIRED if x not in headers]
        if missing:
            errors.append("missing columns: " + ", ".join(missing))
        rows = list(reader)

    counts = Counter()
    accepted_handles: list[str] = []
    for n, row in enumerate(rows, start=2):
        decision = (row.get("decision") or "").strip()
        counts[decision or "blank"] += 1
        if decision not in ALLOWED_DECISIONS:
            errors.append(f"row {n}: invalid decision {decision!r}")
            continue
        handle = (row.get("telegram_handle") or "").strip()
        entity_type = (row.get("entity_type") or "").strip()
        evidence_url = (row.get("evidence_url") or "").strip()
        evidence_text = (row.get("evidence_text") or "").strip()
        rejection = (row.get("rejection_reason") or "").strip()

        if handle and not HANDLE_RE.fullmatch(handle):
            errors.append(f"row {n}: invalid Telegram handle {handle!r}")

        if decision == "accepted":
            accepted_handles.append(handle.lower())
            if not handle:
                errors.append(f"row {n}: accepted row has no handle")
            if entity_type not in ALLOWED_ACCEPTED_TYPES:
                errors.append(f"row {n}: accepted entity_type must be direct_user/public_group")
            if not evidence_url:
                errors.append(f"row {n}: accepted row has no evidence_url")
            if not evidence_text:
                errors.append(f"row {n}: accepted row has no evidence_text")
            if not (row.get("checked_at") or "").strip():
                errors.append(f"row {n}: accepted row has no checked_at")
            if (row.get("confidence") or "").strip().lower() not in {"high", "medium"}:
                errors.append(f"row {n}: accepted confidence must be high/medium")
        elif decision == "no_public_contact":
            if handle:
                errors.append(f"row {n}: no_public_contact row contains a handle")
            if not evidence_text:
                errors.append(f"row {n}: no_public_contact must summarize checked surfaces")
        elif decision in {"reject", "quarantine"} and not rejection:
            errors.append(f"row {n}: {decision} row needs rejection_reason/missing proof")

    duplicate_counts = Counter(accepted_handles)
    for handle, count in duplicate_counts.items():
        if handle and count > 1:
            warnings.append(f"accepted handle appears {count} times: {handle}")

    print(f"rows={len(rows)} decisions={dict(counts)}")
    for warning in warnings:
        print("WARNING:", warning)
    if errors:
        for error in errors:
            print("ERROR:", error, file=sys.stderr)
        print(f"FAIL: {len(errors)} validation error(s)", file=sys.stderr)
        return 1
    print("PASS: structure and accepted-row evidence requirements satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
