"""Persist operator opt-outs shared by email and Telegram outreach."""
from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3


def database(home: Path) -> Path:
    return home / "ops_state" / "company_stops.sqlite3"


def normalize_peer(peer: str) -> str:
    return re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/", "", str(peer).strip().lower()).lstrip("@").rstrip("/")


def record(home: Path, domains, emails, peers) -> None:
    rules = []
    for kind, values, pattern in (
        ("domain", domains, r"(?:[a-z0-9-]+\.)+[a-z0-9-]+"),
        ("email", emails, r"[^@\s]+@[^@\s]+\.[^@\s]+"),
        ("telegram", peers, r"[a-z0-9_]{4,32}|-?[0-9]+"),
    ):
        for value in values:
            key = normalize_peer(value) if kind == "telegram" else value.strip().lower()
            if not re.fullmatch(pattern, key):
                raise ValueError(f"invalid {kind}: {value!r}")
            rules.append((kind, key))
    if not rules:
        raise ValueError("supply a company domain, email or Telegram contact")
    path = database(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS stops (kind TEXT, value TEXT, PRIMARY KEY(kind, value))")
        conn.executemany("INSERT OR IGNORE INTO stops VALUES (?, ?)", rules)


def snapshot(home: Path | None) -> set[tuple[str, str]]:
    if home is None:
        return set()
    path = database(home)
    try:
        with path.open("rb"):
            pass
    except FileNotFoundError:
        return set()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        return set(conn.execute("SELECT kind, value FROM stops"))


def blocks_email(rules, email: str) -> bool:
    email = email.strip().lower()
    return ("email", email) in rules or ("domain", email.rpartition("@")[2]) in rules


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", action="append", default=[])
    parser.add_argument("--email", action="append", default=[])
    parser.add_argument("--telegram", action="append", default=[])
    args = parser.parse_args()
    home = os.getenv("KIT_HOME") or os.getenv("HERMES_HOME")
    if not home:
        parser.error("set KIT_HOME to the deployment directory")
    try:
        record(Path(home), args.domain, args.email, args.telegram)
    except ValueError as exc:
        parser.error(str(exc))
    print("Opt-out recorded.")


if __name__ == "__main__":
    main()
