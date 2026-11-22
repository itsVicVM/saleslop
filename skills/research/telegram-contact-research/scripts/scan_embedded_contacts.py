#!/usr/bin/env python3
"""
Scan telegram_contact_finder.py result CSVs for a second, better contact handle
mentioned INSIDE an already-captured channel/group about-text or preview description.

A channel description may list a separate support contact. Scan saved
descriptions for contact and support language around a handle, including
mentions without the word "Telegram". This step uses only the supplied files.

Usage:
    python3 scan_embedded_contacts.py /path/to/results_dir/results_*.csv
    python3 scan_embedded_contacts.py /path/to/single_results.csv --out findings.json

Run this after the contact finder to review additional mentions in its output.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import re
import sys

KEYWORDS_RE = re.compile(
    r'(?:support|contact|help|enquir|report|questions?|reach)[^@\n]{0,40}@([A-Za-z][A-Za-z0-9_]{4,31})',
    re.IGNORECASE,
)


def scan_files(paths: list[str]) -> list[dict]:
    findings = []
    for fp in paths:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_handle = (row.get("telegram_handle") or "").lower()
                for field in ("contact_name_or_title", "evidence_text"):
                    text = row.get(field) or ""
                    for m in KEYWORDS_RE.finditer(text):
                        found_handle = m.group(1)
                        if found_handle.lower() == existing_handle:
                            continue
                        findings.append({
                            "site": row.get("input_site", ""),
                            "company": row.get("company_guess", ""),
                            "existing_handle": row.get("telegram_handle", ""),
                            "found_handle": found_handle,
                            "field": field,
                            "context": text[max(0, m.start() - 30):m.end() + 10],
                            "source_csv": fp,
                        })

    seen = set()
    deduped = []
    for f in findings:
        key = (f["site"], f["found_handle"].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    return deduped


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_globs", nargs="+", help="One or more result CSV paths/globs")
    ap.add_argument("--out", default=None, help="Write JSON findings here (default: stdout)")
    args = ap.parse_args()

    paths = []
    for g in args.csv_globs:
        paths.extend(sorted(glob.glob(g)))
    if not paths:
        print("No CSV files matched.", file=sys.stderr)
        sys.exit(1)

    findings = scan_files(paths)
    payload = json.dumps(findings, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)
        print(f"{len(findings)} finding(s) written to {args.out}")
    else:
        print(payload)
        print(f"\n{len(findings)} finding(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
