"""Prepare provider input or preview the first email for one approved company."""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

from runner import kit_env
from . import cli
from .config import EMAIL_RE


def accepted_company(path: Path) -> dict:
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(record, dict):
        raise ValueError("expected a company record")
    if record.get("decision") != "accepted":
        raise ValueError("company has not been accepted for contact research")
    for field in ("company", "domain", "hypothesis", "source_url", "evidence", "role"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"missing company field: {field}")
        record[field] = record[field].strip()
    record["domain"] = record["domain"].lower()
    if not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z0-9-]+", record["domain"]):
        raise ValueError("expected a bare company domain")
    source = urlsplit(record["source_url"])
    if source.scheme not in ("https", "http") or not source.hostname:
        raise ValueError("expected a source page URL")
    return record


def prepare(record: dict, target: Path) -> None:
    contents = record["domain"] + "\n"
    try:
        with target.open("x", encoding="utf-8") as stream:
            stream.write(contents)
    except FileExistsError:
        if target.read_text(encoding="utf-8") != contents:
            raise ValueError("existing domain list differs; choose a new file")
    print(json.dumps({"company": record["company"], "role": record["role"],
                      "lookup_args": [str(target), "--roles", record["role"], "--no-apollo"]}, ensure_ascii=False))


def preview(record: dict, args) -> int:
    contacts = json.loads(args.contacts.read_text(encoding="utf-8-sig"))
    if not isinstance(contacts, dict):
        raise ValueError("expected contact results keyed by domain")
    entry = contacts.get(record["domain"], {})
    if not isinstance(entry, dict):
        raise ValueError("invalid contact result for the approved domain")
    candidates = entry.get("contacts") or []
    verification = entry.get("reoon") or {}
    if not isinstance(verification, dict) or not isinstance(candidates, list):
        raise ValueError("invalid contact list or verification result")
    status = verification.get("status") or "unverified"
    print(f"{record['company']} | {record['hypothesis']} | {record['role']}\n"
          f"{record['source_url']} | {record['evidence']}")
    if not candidates:
        print("Needs contact research: no address found.")
        return 0
    if not isinstance(candidates[0], dict):
        raise ValueError("invalid first contact in provider output")
    email = str(candidates[0].get("email") or "").strip().lower()
    if status != "valid" or not EMAIL_RE.fullmatch(email):
        print(f"Needs mailbox verification: {status} ({email}).")
        return 0

    pack, base = kit_env.load_pack(args.pack)
    env = {} if args.standalone else dict(os.environ)
    options, _, problems = kit_env.build_outreach_env(pack, env, base)
    if problems:
        raise ValueError(", ".join(problems))
    env.update(options)
    if not args.first_name:
        template = pack.get("mail", {}).get("unnamed_template")
        if not template:
            print("Needs a template for an unnamed contact, or a confirmed first name.")
            return 0
        env["KIT_OUTREACH_TEMPLATES"] = str(base / template)
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "lead.csv"
        if args.standalone and env.get("KIT_OUTREACH_TRANSPORT") == "outbox":
            env["KIT_OUTREACH_OUTBOX_DIR"] = str(Path(work) / "outbox")
        env["KIT_OUTREACH_COLUMNS"] = "company=company,email=email,first_name=first_name"
        if not env.get("KIT_OUTREACH_STATE_DIR"):
            if env.get("KIT_HOME") or env.get("HERMES_HOME"):
                raise ValueError("set KIT_OUTREACH_STATE_DIR to the campaign journal")
            env["KIT_OUTREACH_STATE_DIR"] = str(Path(work) / "state")
            print("Standalone preview with an empty campaign journal.")
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["company", "email", "first_name"])
            writer.writerow([record["company"], email, args.first_name])
        command = ["run", "--dry-run", "--preview", "1", "--leads", f"csv:{path}"]
        if args.now:
            command += ["--now", args.now]
        return cli.main(command, env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    lookup = commands.add_parser("prepare")
    lookup.add_argument("accepted", type=Path)
    lookup.add_argument("--domains", required=True, type=Path)
    draft = commands.add_parser("preview")
    draft.add_argument("accepted", type=Path)
    draft.add_argument("--contacts", type=Path, required=True)
    draft.add_argument("--pack", type=Path, required=True)
    draft.add_argument("--first-name", default="", help="Operator-confirmed recipient name")
    draft.add_argument("--now", help="ISO timestamp for a reproducible dry run")
    draft.add_argument("--standalone", action="store_true", help="Offline demo with isolated campaign state")
    args = parser.parse_args()
    try:
        record = accepted_company(args.accepted)
        if args.command == "prepare":
            prepare(record, args.domains)
            return 0
        return preview(record, args)
    except (OSError, ValueError, kit_env.PackError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
