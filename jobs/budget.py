#!/usr/bin/env python3
"""Shared daily budget for paid lookup APIs, one ledger for every chain on the host.

Every paid call reserves its unit BEFORE the request goes out: check-and-increment happens atomically under a
file lock, so parallel chains (cron jobs, background batches, an operator's manual run) can never overspend the
pack's limits.budgets together. A reservation is never refunded, not even on an HTTP error: the provider may have
charged the credit anyway. When a provider's budget is used up the caller stops (exit code BUDGET_EXIT) instead of
skipping item after item.

Configuration comes from env vars that kit/runner/kit_env.py builds from the pack:
    KIT_BUDGETS        "tavily=200,findymail=100,..."   (limits.budgets.<provider>_daily)
    KIT_BUDGET_TZ      IANA zone of the day boundary     (company.timezone, default UTC)
    KIT_BUDGET_LEDGER  ledger path, else $KIT_HOME/budget/ledger.json, else $HERMES_HOME/budget/ledger.json
No KIT_BUDGETS, no ledger path, or a provider missing from KIT_BUDGETS -> the call is refused (fail closed).

The ledger is per host. A client who shares one API key between several hosts has to split the provider's daily
limit between their packs.

This file is the canonical copy. kit/skills/research/*/scripts/_budget.py are byte-identical copies, so the
skill scripts work where they are installed (kit/tests/test_jobs_budget.py checks that the copies match).

CLI:
    python3 budget.py status [--json]
    python3 budget.py report [--warn-percent 80] [--always]   # [SILENT] when nothing to report
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUDGET_EXIT = 75          # EX_TEMPFAIL: "stopped, try again tomorrow"; chains treat it as a stop signal
KNOWN_PROVIDERS = ("serper", "findymail", "publicwww", "tavily", "apollo")
KEEP_DAYS = 14
LOCK_TIMEOUT_S = 30.0


class BudgetExhausted(Exception):
    def __init__(self, provider: str, used: int, limit: int, reason: str = ""):
        self.provider, self.used, self.limit = provider, used, limit
        super().__init__(reason or f"daily budget for {provider} is used up ({used}/{limit}); stopping instead of "
                                   f"spending more credits")


class BudgetNotConfigured(BudgetExhausted):
    def __init__(self, reason: str):
        super().__init__("", 0, 0, reason)


def parse_limits(text: str) -> dict:
    out = {}
    for part in (text or "").split(","):
        if not part.strip():
            continue
        name, _, value = part.partition("=")
        out[name.strip().lower()] = int(value.strip())
    return out


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name or "UTC")
    except Exception:
        return timezone.utc


class _FileLock:
    """Blocking exclusive lock on a side file; released by the OS if the process dies."""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_S):
        self.path, self.timeout, self.fh = path, timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    self.fh.close()
                    raise BudgetNotConfigured(f"budget ledger lock {self.path} is held for over {self.timeout:.0f}s")
                time.sleep(0.02)

    def __exit__(self, *exc):
        try:
            if sys.platform == "win32":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()


class Ledger:
    def __init__(self, path, limits: dict, tz: str = "UTC", clock=None):
        self.path = Path(path)
        self.limits = {k.lower(): int(v) for k, v in (limits or {}).items()}
        self.tz = _zone(tz)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ---------------------------------------------------------------- storage
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise BudgetNotConfigured("cannot read budget ledger; restore it before spending") from exc
        if not isinstance(data, dict) or not ({"days", "minute"} & data.keys()):
            raise BudgetNotConfigured("unexpected budget ledger format")
        days = data.get("days", {})
        minute = data.get("minute", {})
        if (not isinstance(days, dict) or not isinstance(minute, dict)
                or any(not isinstance(counts, dict) or
                       any(type(n) is not int or n < 0 for n in counts.values()) for counts in days.values())
                or any(not isinstance(stamps, list) or
                       any(type(t) not in (int, float) for t in stamps) for stamps in minute.values())):
            raise BudgetNotConfigured("invalid counters in budget ledger")
        return data

    def _save(self, data: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def _lock(self):
        return _FileLock(self.path.with_name(self.path.name + ".lock"))

    def day(self) -> str:
        return self.clock().astimezone(self.tz).date().isoformat()

    # ---------------------------------------------------------------- daily budgets
    def reserve(self, provider: str, n: int = 1) -> int:
        """Book n units for today or raise BudgetExhausted. Returns units left after the booking."""
        if type(n) is not int or n < 1:
            raise ValueError("reservation must be a positive integer")
        provider = provider.lower()
        if provider not in self.limits:
            raise BudgetExhausted(provider, 0, 0, f"no daily budget for {provider} (limits.budgets.{provider}_daily "
                                                  f"in the brand pack): paid calls refused")
        limit = self.limits[provider]
        with self._lock():
            data = self._load()
            days = data.setdefault("days", {})
            today = self.day()
            used = int(days.get(today, {}).get(provider, 0))
            if used + n > limit:
                raise BudgetExhausted(provider, used, limit)
            days.setdefault(today, {})[provider] = used + n
            for old in sorted(days)[:-KEEP_DAYS]:
                days.pop(old, None)
            self._save(data)
        return limit - used - n

    def status(self) -> dict:
        today = self.day()
        used = self._load().get("days", {}).get(today, {})
        names = sorted(set(self.limits) | set(used))
        return {"day": today, "providers": {p: {"used": int(used.get(p, 0)), "limit": self.limits.get(p),
                                                "left": (None if p not in self.limits
                                                         else max(0, self.limits[p] - int(used.get(p, 0))))}
                                            for p in names}}

    # ---------------------------------------------------------------- per-minute quota (Google Sheets reads)
    def take_minute(self, name: str, limit: int, n: int = 1, max_wait_s: float = 70.0, sleep=time.sleep) -> None:
        """Rolling 60 s window: waits for room instead of failing (a read quota, not money)."""
        waited = 0.0
        while True:
            with self._lock():
                data = self._load()
                now = self.clock().timestamp()
                stamps = [t for t in data.setdefault("minute", {}).get(name, []) if now - t < 60.0]
                if len(stamps) + n <= limit:
                    data["minute"][name] = stamps + [now] * n
                    self._save(data)
                    return
                wait = 60.0 - (now - min(stamps)) + 0.05 if stamps else 1.0
            if waited + wait > max_wait_s:
                raise BudgetExhausted(name, len(stamps), limit, f"{name}: {limit}/min quota still full after "
                                                                f"{waited:.0f}s")
            sleep(wait)
            waited += wait


def ledger_path_from_env(env=None) -> Path | None:
    env = os.environ if env is None else env
    if env.get("KIT_BUDGET_LEDGER", "").strip():
        return Path(env["KIT_BUDGET_LEDGER"].strip())
    for var in ("KIT_HOME", "HERMES_HOME"):
        if env.get(var, "").strip():
            return Path(env[var].strip()) / "budget" / "ledger.json"
    return None


def from_env(env=None, clock=None) -> Ledger:
    env = os.environ if env is None else env
    raw = env.get("KIT_BUDGETS")
    if raw is None:
        raise BudgetNotConfigured("KIT_BUDGETS is not set: run the script through kit/runner/kit_env.py so the pack's "
                                  "limits.budgets apply; paid calls refused")
    try:
        limits = parse_limits(raw)
    except ValueError:
        raise BudgetNotConfigured(f"KIT_BUDGETS is malformed ({raw!r}): paid calls refused")
    path = ledger_path_from_env(env)
    if path is None:
        raise BudgetNotConfigured("no budget ledger path (KIT_BUDGET_LEDGER or KIT_HOME/HERMES_HOME): paid calls refused")
    return Ledger(path, limits, env.get("KIT_BUDGET_TZ", "UTC"), clock=clock)


_DEFAULT: Ledger | None = None


def reserve(provider: str, n: int = 1) -> int:
    """Module-level helper for the skill scripts: env config, cached per process."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = from_env()
    return _DEFAULT.reserve(provider, n)


# ---------------------------------------------------------------- CLI

def render_report(status: dict, warn_percent: float, always: bool, lang: str = "ru") -> str:
    rows, alert = [], False
    for p, s in status["providers"].items():
        if s["limit"] is None:
            continue
        pct = 100.0 * s["used"] / s["limit"] if s["limit"] else 100.0
        flag = ""
        if s["left"] == 0:
            flag, alert = (" ⛔ исчерпан" if lang == "ru" else " ⛔ used up"), True
        elif pct >= warn_percent:
            flag, alert = " ⚠️", True
        rows.append(f"- {p}: {s['used']}/{s['limit']}{flag}")
    if not alert and not always:
        return "[SILENT]"
    head = f"Бюджет API за {status['day']}" if lang == "ru" else f"API budget for {status['day']}"
    return "\n".join([head, ""] + rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Shared daily API budget (limits.budgets)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    rp = sub.add_parser("report")
    rp.add_argument("--warn-percent", type=float, default=80.0)
    rp.add_argument("--always", action="store_true", help="report even when every provider is below the warning level")
    rp.add_argument("--lang", default=os.environ.get("KIT_REPORT_LANG", "ru"))
    args = ap.parse_args(argv)
    try:
        ledger = from_env()
    except BudgetNotConfigured as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    status = ledger.status()
    if args.cmd == "status":
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=1))
        else:
            for p, s in status["providers"].items():
                print(f"{p}: {s['used']}/{s['limit']} (left {s['left']})")
        return 0
    print(render_report(status, args.warn_percent, args.always, args.lang))
    return 0


if __name__ == "__main__":
    sys.exit(main())
