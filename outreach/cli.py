#!/usr/bin/env python3
"""Outreach CLI. Config comes from KIT_OUTREACH_* env vars (README.md).

    python3 -m outreach.cli check
    python3 -m outreach.cli run                      # dry-run (default): prints the plan, writes nothing
    python3 -m outreach.cli run --preview 2          # ... plus the first 2 rendered emails
    python3 -m outreach.cli run --send               # real sending via KIT_OUTREACH_TRANSPORT
    python3 -m outreach.cli status
    python3 -m outreach.cli bounce lead@x.example [--source dsn]   # or --file bounces.txt
    python3 -m outreach.cli resume primary

Exit codes: 0 ok, 1 config invalid, 2 bad input (leads/templates/args), 3 every mailbox is
paused by the bounce gate, 4 another run holds the lock.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "outreach"

from outreach import config as cfgmod  # noqa: E402
from outreach import engine  # noqa: E402
from outreach.journal import Journal, LockedError, bounce_rate  # noqa: E402
from outreach.leads import LeadSourceError, load_leads, load_suppression, normalize_email  # noqa: E402
from outreach.templates import TemplateError, load_templates  # noqa: E402
from outreach.transport import make_transport  # noqa: E402


def _parse_now(text: str | None) -> datetime | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(cfg, dt: datetime) -> str:
    return dt.astimezone(cfg.tz).strftime("%Y-%m-%d %H:%M %Z")


def print_plan(cfg, plan: engine.Plan, preview: int, out) -> None:
    w = "OPEN" if plan.window_open else f"CLOSED, plan starts at next opening {_fmt(cfg, plan.opens_at)}"
    print(f"DRY RUN - nothing is sent, nothing is written. transport={cfg.transport}", file=out)
    print(f"now {_fmt(cfg, plan.now)}; window {cfg.window_from:%H:%M}-{cfg.window_to:%H:%M} {cfg.tz_name}: {w}; "
          f"closes {_fmt(cfg, plan.closes_at)}", file=out)
    print("\nmailboxes:", file=out)
    for ms in plan.mailboxes:
        state = ms.stop_reason or "active"
        print(f"  {ms.mailbox.id:<12} {ms.mailbox.address:<32} cap today {ms.cap:>3}  planned {ms.used:>3}  {state}",
              file=out)
    print(f"\nplanned sends: {len(plan.sends)}", file=out)
    for s in plan.sends:
        print(f"  {s.eta.astimezone(cfg.tz):%H:%M}  row {s.lead.row:<4} {s.lead.email:<36} via {s.mailbox.id:<10} "
              f"[{s.template}] {s.subject}", file=out)
    if plan.deferred:
        print(f"\ndeferred to a later run: {len(plan.deferred)}", file=out)
        for lead, why in plan.deferred:
            print(f"  row {lead.row:<4} {lead.email:<36} {why}", file=out)
    if plan.skips:
        print(f"\nskipped: {len(plan.skips)}", file=out)
        for s in plan.skips:
            print(f"  row {s.lead.row:<4} {(s.lead.email or s.lead.raw_email)!s:<36} {s.reason}", file=out)
    for s in plan.sends[:preview]:
        print(f"\n----- preview: {s.lead.email} via {s.mailbox.address}\nSubject: {s.subject}\n\n{s.body}", file=out)


def main(argv: list[str] | None = None, env: dict | None = None, fetch=None, transport=None, out=None) -> int:
    env = dict(os.environ) if env is None else env
    out = out or sys.stdout
    err = sys.stderr
    ap = argparse.ArgumentParser(prog="outreach", description="Scripted email outreach (dry-run by default)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate config, templates and (with --send-ready) mailbox secrets") \
        .add_argument("--send-ready", action="store_true")
    r = sub.add_parser("run", help="plan (default) or send")
    r.add_argument("--leads", help="csv:<path> or sheet:<tab> (overrides KIT_OUTREACH_LEADS)")
    r.add_argument("--send", action="store_true", help="really send via KIT_OUTREACH_TRANSPORT")
    r.add_argument("--dry-run", action="store_true", help="explicit dry-run (the default)")
    r.add_argument("--preview", type=int, default=0, help="print N rendered emails in dry-run")
    r.add_argument("--now", help="simulated time (ISO 8601); dry-run or outbox transport only")
    r.add_argument("--seed", type=int, help="seed for random pauses")
    st = sub.add_parser("status", help="per-mailbox counters, gate, pauses")
    st.add_argument("--now")
    b = sub.add_parser("bounce", help="record bounced addresses (async DSN, operator)")
    b.add_argument("emails", nargs="*")
    b.add_argument("--file", type=Path, help="one address per line")
    b.add_argument("--source", default="manual")
    b.add_argument("--mailbox", help="mailbox id if the address is not in the journal")
    b.add_argument("--now")
    rs = sub.add_parser("resume", help="lift a bounce-gate pause")
    rs.add_argument("mailbox")
    rs.add_argument("--reason", default="operator")
    rs.add_argument("--now")
    args = ap.parse_args(argv)

    try:
        cfg = cfgmod.from_env(env)
        templates = load_templates(cfg.templates)
    except cfgmod.ConfigError as e:
        print("Outreach config is invalid:", file=err)
        for p in e.problems:
            print(f"  - {p}", file=err)
        return 1
    except TemplateError as e:
        print(f"ERROR: template: {e}", file=err)
        return 1

    if args.cmd == "check":
        problems = cfgmod.secret_problems(cfg, env) if args.send_ready else []
        if problems:
            print("Not ready to send:", file=err)
            for p in problems:
                print(f"  - {p}", file=err)
            return 1
        print(f"OK: {len(cfg.mailboxes)} mailboxes, {len(templates)} templates, transport {cfg.transport}", file=out)
        return 0

    journal = Journal(cfg.state_dir)
    now_arg = _parse_now(getattr(args, "now", None))

    if args.cmd == "status":
        now = now_arg or datetime.now(timezone.utc)
        v = journal.view
        print(f"journal: {journal.path} (exists: {journal.path.exists()}, torn lines: {v.bad_lines})", file=out)
        print(f"contacted {len(v.contacted)}, bounced {len(v.bounced)}, uncertain {len(v.uncertain)}", file=out)
        alloc = engine.Allocator(cfg, v, now)
        for ms in alloc.states:
            bnc, snt = bounce_rate(v.mb(ms.mailbox.id), now, cfg.bounce_window_days)
            print(f"  {ms.mailbox.id:<12} today {ms.used}/{ms.cap}  bounces {bnc}/{snt} "
                  f"({cfg.bounce_window_days}d)  {ms.stop_reason or 'active'}", file=out)
        return 0

    if args.cmd in ("bounce", "resume"):
        try:
            journal.lock()
        except LockedError as e:
            print(f"ERROR: {e}", file=err)
            return 4
        try:
            now = now_arg or datetime.now(timezone.utc)
            if args.cmd == "resume":
                if args.mailbox not in {m.id for m in cfg.mailboxes}:
                    print(f"ERROR: unknown mailbox {args.mailbox!r}", file=err)
                    return 2
                journal.append({"event": "resumed", "ts": now.isoformat(), "mailbox": args.mailbox,
                                "reason": args.reason})
                print(f"resumed {args.mailbox}; the gate now counts only sends/bounces after {now.isoformat()}",
                      file=out)
            else:
                emails = list(args.emails)
                if args.file:
                    emails += [l.strip() for l in args.file.read_text(encoding="utf-8-sig").splitlines() if l.strip()]
                if not emails:
                    print("ERROR: no addresses", file=err)
                    return 2
                for e in emails:
                    status = engine.record_bounce(journal, e, now, args.source, args.mailbox)
                    print(f"{normalize_email(e)}: {status}", file=out)
            journal.write_snapshot()
        finally:
            journal.unlock()
        return 0

    # ---- run
    if args.send and args.dry_run:
        print("ERROR: --send and --dry-run are mutually exclusive", file=err)
        return 2
    spec = args.leads or cfg.leads
    if not spec:
        print("ERROR: no leads source (--leads or KIT_OUTREACH_LEADS)", file=err)
        return 2
    try:
        leads = load_leads(spec, cfg.columns, cfg.sheet_id, fetch)
        suppression = load_suppression(cfg.suppression_file)
    except (LeadSourceError, OSError) as e:
        print(f"ERROR: leads: {e}", file=err)
        return 2

    if not args.send:
        now = now_arg or datetime.now(timezone.utc)
        try:
            plan = engine.build_plan(cfg, leads, templates, journal.view, suppression, now)
        except ValueError as e:
            print(f"ERROR: {e}", file=err)
            return 1
        print_plan(cfg, plan, args.preview, out)
        problems = cfgmod.secret_problems(cfg, env)
        if problems:
            print("\nnote: --send would refuse until these are set:", file=out)
            for p in problems:
                print(f"  - {p}", file=out)
        return 0

    tr = transport or make_transport(cfg, env)
    if getattr(tr, "real", True):
        if now_arg:
            print("ERROR: --now is only allowed in dry-run or with TRANSPORT=outbox", file=err)
            return 2
        problems = cfgmod.secret_problems(cfg, env)
        if problems:
            print("Not ready to send:", file=err)
            for p in problems:
                print(f"  - {p}", file=err)
            return 1
        print(f"REAL SEND via {tr.name}: {len(cfg.mailboxes)} mailboxes, {len(leads)} leads in source", file=out)
    clock = engine.SimClock(now_arg) if now_arg else engine.SystemClock()
    try:
        journal.lock()
    except LockedError as e:
        print(f"ERROR: {e}", file=err)
        return 4
    try:
        res = engine.run(cfg, leads, templates, journal, suppression, tr, clock,
                         random.Random(args.seed), log=lambda m: print(m, file=out))
    finally:
        journal.unlock()
    print(f"done: sent {res.sent}, bounced {res.bounced}, failed {res.failed}, uncertain {res.uncertain}; "
          f"stop: {res.stop_reason}", file=out)
    return 3 if res.all_gated else 0


if __name__ == "__main__":
    sys.exit(main())
