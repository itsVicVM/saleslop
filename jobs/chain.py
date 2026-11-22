#!/usr/bin/env python3
"""Night search chain: for each pack hypothesis, discovery -> validation, stopping on the shared API budget.

Steps per hypothesis (run dir: $KIT_HOME/runs/<hypothesis id>, resumable):
  1. config.json from targeting.hypotheses[] (kit_env.discovery_config);
  2. bulk-web-discovery-worker/scripts/discovery_worker.py --run-dir <dir>;
  3. validation_worker.py --run-dir <dir> --verify;
  4. jobs/qualify.py queue: prepare the commercial review packet and pack criteria.
A step that exits with BUDGET_EXIT stops the whole chain (no later hypothesis starts); any other failure stops it
too. Before starting, the chain refuses to run when a budget the steps need is already used up.
Nothing is written to the tracker: candidates.jsonl in each run dir is reviewed separately.

Usage (env from kit_env.py --module jobs, which carries the search env and KIT_BUDGETS):
    python3 -m jobs.chain --pack ../my-sales/pack.json --hypothesis my-segment [--hypothesis ...] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "jobs"

from . import budget  # noqa: E402
from .config import home  # noqa: E402

KIT = Path(__file__).resolve().parents[1]
SCRIPTS = KIT / "skills" / "research" / "bulk-web-discovery-worker" / "scripts"
PAID_BACKENDS = {"tavily", "serper"}


def _kit_env():
    sys.path.insert(0, str(KIT / "runner"))
    import kit_env  # noqa: E402
    return kit_env


def needed_providers(cfg: dict) -> set:
    return {b for q in cfg.get("queries", []) for b in q.get("backends", [])} & PAID_BACKENDS


def _logged_call(cmd: list, log_path: Path) -> int:
    """Run one step with its output in a log file: the chain's stdout is what the cron job relays to the chat."""
    with open(log_path, "w", encoding="utf-8") as fh:
        return subprocess.call(cmd, stdout=fh, stderr=subprocess.STDOUT)


def run_chain(pack_path: Path, hypotheses: list[str], runner=None, ledger=None, dry_run=False, out=print) -> int:
    """runner(cmd, log_path) -> exit code (default: subprocess with output in <run dir>/<step>.log)."""
    runner = runner or _logged_call
    kit_env = _kit_env()
    pack, _ = kit_env.load_pack(pack_path)
    runs = home() / "runs"
    if not dry_run and ledger is None:
        ledger = budget.from_env()
    for hyp in hypotheses:
        cfg = kit_env.discovery_config(pack, hyp, 2.0)
        run_dir = runs / hyp
        if not dry_run:
            left = ledger.status()["providers"]
            empty = sorted(p for p in needed_providers(cfg) if (left.get(p) or {}).get("left") in (0, None))
            if empty:
                out(f"STOPPED before {hyp}: no budget left today for {', '.join(empty)}")
                return budget.BUDGET_EXIT
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        steps = [[sys.executable, str(SCRIPTS / "discovery_worker.py"), "--run-dir", str(run_dir)],
                 [sys.executable, str(SCRIPTS / "validation_worker.py"), "--run-dir", str(run_dir), "--verify"],
                 [sys.executable, str(KIT / "jobs/qualify.py"), "queue", "--run-dir", str(run_dir),
                  "--pack", str(pack_path)]]
        for cmd in steps:
            name = Path(cmd[1]).stem
            if dry_run:
                out(f"[dry-run] {hyp}: {' '.join(cmd[1:])}")
                continue
            log_path = run_dir / f"{name}.log"
            rc = runner(cmd, log_path)
            if rc == budget.BUDGET_EXIT:
                out(f"STOPPED at {hyp}/{name}: daily API budget used up or not configured, see {log_path}; "
                    "the run dir resumes next time")
                return budget.BUDGET_EXIT
            if rc != 0:
                out(f"FAILED at {hyp}/{name} (exit {rc}, see {log_path}); chain stopped")
                return rc
        out(f"{'planned' if dry_run else 'done'} {hyp}: review packet {run_dir / 'qualification.json'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Search chain over pack hypotheses with the shared API budget")
    ap.add_argument("--pack", required=True, type=Path)
    ap.add_argument("--hypothesis", action="append", required=True)
    ap.add_argument("--dry-run", action="store_true", help="print the steps, run nothing")
    args = ap.parse_args(argv)
    try:
        return run_chain(args.pack, args.hypothesis, dry_run=args.dry_run)
    except budget.BudgetNotConfigured as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
