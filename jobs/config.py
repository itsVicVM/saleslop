"""KIT_JOBS_CONFIG (built by kit/runner/kit_env.py --module jobs) and host paths for the jobs."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path


class ConfigError(Exception):
    pass


def load_config(env=None) -> dict:
    env = os.environ if env is None else env
    raw = env.get("KIT_JOBS_CONFIG")
    if not raw:
        raise SystemExit("ERROR: KIT_JOBS_CONFIG is not set: run through kit/runner/kit_env.py --module jobs")
    return json.loads(raw)


def home(env=None) -> Path:
    env = os.environ if env is None else env
    value = (env.get("KIT_HOME") or env.get("HERMES_HOME") or "").strip()
    if not value:
        raise SystemExit("ERROR: KIT_HOME is not set (deployment directory of this host)")
    return Path(value)


def state_dir(env=None) -> Path:
    return home(env) / "jobs_state"


def zone(cfg: dict):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(cfg.get("timezone") or "UTC")
    except Exception:
        return timezone.utc


def now(cfg: dict, clock=None) -> datetime:
    return (clock() if clock else datetime.now(timezone.utc)).astimezone(zone(cfg))


def today(cfg: dict, clock=None) -> date:
    return now(cfg, clock).date()
