"""KIT_OUTREACH_* env vars -> validated OutreachConfig.

Principle of the kit: a missing value is an error, not a silent default. Only a few
safety-leaning knobs have defaults (documented in README.md).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Mapping

PREFIX = "KIT_OUTREACH_"
TRANSPORTS = ("smtp", "himalaya", "outbox")
REAL_TRANSPORTS = ("smtp", "himalaya")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
ENV_PREFIX_RE = re.compile(r"[A-Z][A-Z0-9_]*")
DEFAULT_HIMALAYA_CMD = "himalaya message send --account {account}"
DEFAULT_COLUMNS = {"email": "email", "first_name": "first_name", "company": "company"}


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Mailbox:
    id: str
    address: str
    env_prefix: str
    sender_name: str
    warmup_from: date | None = None  # mailbox already warmed before this date
    outreach: bool = True            # False: the pack forbids outreach from it (mail.accounts[].outreach)

    @property
    def domain(self) -> str:
        return self.address.rsplit("@", 1)[1]


@dataclass(frozen=True)
class Warmup:
    start: int
    step: int

    def cap(self, daily_cap: int, day_index: int) -> int:
        """Cap for the N-th calendar day since the mailbox's first send (0-based)."""
        return min(daily_cap, self.start + self.step * max(day_index, 0))


@dataclass
class OutreachConfig:
    mailboxes: list[Mailbox]
    transport: str
    templates: list[Path]
    daily_cap: int
    warmup: Warmup | None
    tz: tzinfo
    tz_name: str
    window_from: time
    window_to: time
    days: set[int]                      # 0=Mon .. 6=Sun
    pause_min: float
    pause_max: float
    bounce_gate_percent: float
    bounce_min_sent: int
    bounce_window_days: int
    state_dir: Path
    leads: str | None = None            # csv:<path> | sheet:<tab>
    sheet_id: str | None = None
    columns: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLUMNS))
    max_per_run: int | None = None
    suppression_file: Path | None = None
    himalaya_cmd: str = DEFAULT_HIMALAYA_CMD
    outbox_dir: Path | None = None
    round_robin: bool = True
    off_file: Path | None = None        # runtime switch of the ops bot: {"mail": [mailbox id, ...]}
    deployment_home: Path | None = None

    def switched_off(self) -> set[str]:
        """Mailboxes the operator switched off for outreach in the ops bot (read at every run)."""
        if not self.off_file:
            return set()
        try:
            data = json.loads(Path(self.off_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {str(x) for x in (data.get("mail") or [])} if isinstance(data, dict) else set()


# --------------------------------------------------------------------------- parsers

def parse_hhmm(text: str) -> time:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"{text!r} is not HH:MM")
    return time(int(m.group(1)), int(m.group(2)))


def parse_days(text: str) -> set[int]:
    """'mon-fri', 'mon,wed,fri', 'mon-sun', or ISO digits '1-5' (1=Mon)."""
    out: set[int] = set()

    def one(tok: str) -> int:
        tok = tok.strip().lower()
        if tok.isdigit() and 1 <= int(tok) <= 7:
            return int(tok) - 1
        if tok[:3] in DAY_NAMES:
            return DAY_NAMES.index(tok[:3])
        raise ValueError(f"unknown day {tok!r}")

    for part in text.split(","):
        if not part.strip():
            continue
        if "-" in part:
            a, b = (one(x) for x in part.split("-", 1))
            i = a
            while True:
                out.add(i)
                if i == b:
                    break
                i = (i + 1) % 7
        else:
            out.add(one(part))
    if not out:
        raise ValueError("no days")
    return out


def parse_range(text: str) -> tuple[float, float]:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*", text)
    if not m:
        raise ValueError(f"{text!r} is not MIN-MAX seconds")
    lo, hi = float(m.group(1)), float(m.group(2))
    if hi < lo:
        raise ValueError(f"{text!r}: max < min")
    return lo, hi


def parse_warmup(text: str) -> Warmup | None:
    """'off' or 'start=5,step=5'."""
    if text.strip().lower() == "off":
        return None
    kv = {}
    for part in text.split(","):
        k, sep, v = part.partition("=")
        if not sep:
            raise ValueError(f"{text!r}: expected start=N,step=N or off")
        kv[k.strip()] = v.strip()
    if set(kv) != {"start", "step"}:
        raise ValueError(f"{text!r}: expected start=N,step=N or off")
    start, step = int(kv["start"]), int(kv["step"])
    if start < 1 or step < 0:
        raise ValueError(f"{text!r}: start must be >= 1, step >= 0")
    return Warmup(start, step)


def parse_columns(text: str) -> dict[str, str]:
    """'email=E-mail,first_name=Name,company=Company'; a value 'col:G' means column letter G."""
    cols = dict(DEFAULT_COLUMNS)
    for part in text.split(","):
        if not part.strip():
            continue
        k, sep, v = part.partition("=")
        k = k.strip()
        if not sep or k not in ("email", "first_name", "company", "template") or not v.strip():
            raise ValueError(f"bad column mapping {part!r} (keys: email, first_name, company, template)")
        cols[k] = v.strip()
    return cols


def parse_mailboxes(text: str) -> list[Mailbox]:
    data = json.loads(text)
    if not isinstance(data, list) or not data:
        raise ValueError("must be a non-empty JSON list")
    out, ids, addrs = [], set(), set()
    for i, item in enumerate(data):
        where = f"[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: must be an object")
        for key in ("id", "address", "env_prefix", "sender_name"):
            if not str(item.get(key) or "").strip():
                raise ValueError(f"{where}: {key} is required")
        address = str(item["address"]).strip().lower()
        if not EMAIL_RE.fullmatch(address):
            raise ValueError(f"{where}: {address!r} is not an email address")
        if not ENV_PREFIX_RE.fullmatch(str(item["env_prefix"])):
            raise ValueError(f"{where}: env_prefix must be UPPER_SNAKE (got {item['env_prefix']!r})")
        if item["id"] in ids or address in addrs:
            raise ValueError(f"{where}: duplicate id or address")
        ids.add(item["id"])
        addrs.add(address)
        wf = item.get("warmup_from")
        allowed = item.get("outreach", True)
        if not isinstance(allowed, bool):
            raise ValueError(f"{where}: outreach must be true or false")
        out.append(Mailbox(str(item["id"]), address, str(item["env_prefix"]), str(item["sender_name"]).strip(),
                           date.fromisoformat(wf) if wf else None, allowed))
    return out


def load_tz(name: str) -> tzinfo:
    """IANA name (Europe/Berlin), or 'UTC' / '+03:00' fixed offsets (work without tzdata)."""
    if name.upper() == "UTC":
        return timezone.utc
    m = re.fullmatch(r"([+-])(\d{2}):(\d{2})", name)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return timezone(-delta if m.group(1) == "-" else delta, name)
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown timezone {name!r} (on Windows install the tzdata package)") from e


# --------------------------------------------------------------------------- main entry

def from_env(env: Mapping[str, str]) -> OutreachConfig:
    problems: list[str] = []

    def raw(name: str, required: bool = True) -> str | None:
        value = env.get(PREFIX + name, "").strip()
        if not value:
            if required:
                problems.append(f"{PREFIX}{name} is required")
            return None
        return value

    def parsed(name, fn, required=True, default=None):
        value = raw(name, required)
        if value is None:
            return default
        try:
            return fn(value)
        except (ValueError, json.JSONDecodeError) as e:
            problems.append(f"{PREFIX}{name}: {e}")
            return default

    def positive_int(text: str) -> int:
        n = int(text)
        if n < 1:
            raise ValueError("must be >= 1")
        return n

    def percent(text: str) -> float:
        p = float(text)
        if not 0 < p <= 100:
            raise ValueError("must be in (0, 100]")
        return p

    mailboxes = parsed("ACCOUNTS", parse_mailboxes, default=[])
    transport = raw("TRANSPORT")
    if transport and transport not in TRANSPORTS:
        problems.append(f"{PREFIX}TRANSPORT: {transport!r} is not one of {TRANSPORTS}")
    templates = []
    tpl_raw = raw("TEMPLATES")
    if tpl_raw:
        for p in tpl_raw.split(","):
            path = Path(p.strip())
            if not path.is_file():
                problems.append(f"{PREFIX}TEMPLATES: file not found: {path}")
            templates.append(path)
    daily_cap = parsed("DAILY_CAP", positive_int)
    warmup = parsed("WARMUP", parse_warmup)
    tz_name = raw("WINDOW_TZ")
    tz = None
    if tz_name:
        try:
            tz = load_tz(tz_name)
        except ValueError as e:
            problems.append(f"{PREFIX}WINDOW_TZ: {e}")
    window_from = parsed("WINDOW_FROM", parse_hhmm)
    window_to = parsed("WINDOW_TO", parse_hhmm)
    if window_from is not None and window_from == window_to:
        problems.append(f"{PREFIX}WINDOW_FROM and WINDOW_TO must differ")
    days = parsed("WINDOW_DAYS", parse_days, required=False, default=set(range(5)))
    pause = parsed("PAUSE_SECONDS", parse_range, default=(0.0, 0.0))
    gate = parsed("BOUNCE_GATE_PERCENT", percent)
    min_sent = parsed("BOUNCE_MIN_SENT", positive_int, required=False, default=20)
    window_days = parsed("BOUNCE_WINDOW_DAYS", positive_int, required=False, default=14)
    state_dir = raw("STATE_DIR")
    max_per_run = parsed("MAX_PER_RUN", positive_int, required=False)
    columns = parsed("COLUMNS", parse_columns, required=False, default=dict(DEFAULT_COLUMNS))
    suppression = raw("SUPPRESSION_FILE", required=False)
    outbox = raw("OUTBOX_DIR", required=False)
    rr = (raw("ROUND_ROBIN", required=False) or "1").lower()
    if rr not in ("1", "0", "true", "false"):
        problems.append(f"{PREFIX}ROUND_ROBIN: must be 1/0/true/false")
    if transport == "outbox" and not outbox:
        problems.append(f"{PREFIX}OUTBOX_DIR is required for TRANSPORT=outbox")
    if suppression and not Path(suppression).is_file():
        problems.append(f"{PREFIX}SUPPRESSION_FILE: file not found: {suppression}")
    off_file = raw("OFF_FILE", required=False)
    leads = raw("LEADS", required=False)
    if leads and not (leads.startswith("csv:") or leads.startswith("sheet:")):
        problems.append(f"{PREFIX}LEADS: must be csv:<path> or sheet:<tab name>")

    if problems:
        raise ConfigError(problems)
    return OutreachConfig(
        mailboxes=mailboxes, transport=transport, templates=templates, daily_cap=daily_cap,
        warmup=warmup, tz=tz, tz_name=tz_name, window_from=window_from, window_to=window_to,
        days=days, pause_min=pause[0], pause_max=pause[1], bounce_gate_percent=gate,
        bounce_min_sent=min_sent, bounce_window_days=window_days, state_dir=Path(state_dir),
        leads=leads, sheet_id=env.get("SHEET_ID") or None, columns=columns, max_per_run=max_per_run,
        suppression_file=Path(suppression) if suppression else None,
        himalaya_cmd=raw("HIMALAYA_CMD", required=False) or DEFAULT_HIMALAYA_CMD,
        outbox_dir=Path(outbox) if outbox else None, round_robin=rr in ("1", "true"),
        off_file=Path(off_file) if off_file else None,
        deployment_home=Path(home) if (home := env.get("KIT_HOME") or env.get("HERMES_HOME")) else None,
    )


def secret_problems(cfg: OutreachConfig, env: Mapping[str, str]) -> list[str]:
    """Per-mailbox secrets the chosen transport needs (names derived from env_prefix)."""
    problems = []
    for mb in cfg.mailboxes:
        if not mb.outreach:
            continue  # never sends: its secrets are not needed
        p = mb.env_prefix
        if cfg.transport == "smtp":
            for suffix in ("SMTP_HOST", "SMTP_PASSWORD"):
                if not env.get(f"{p}_{suffix}"):
                    problems.append(f"mailbox {mb.id}: {p}_{suffix} is not set")
            sec = env.get(f"{p}_SMTP_SECURITY", "ssl")
            if sec not in ("ssl", "starttls"):
                problems.append(f"mailbox {mb.id}: {p}_SMTP_SECURITY must be ssl or starttls")
            port = env.get(f"{p}_SMTP_PORT", "")
            if port and not port.isdigit():
                problems.append(f"mailbox {mb.id}: {p}_SMTP_PORT must be a number")
        elif cfg.transport == "himalaya":
            if not env.get(f"{p}_HIMALAYA_ACCOUNT"):
                problems.append(f"mailbox {mb.id}: {p}_HIMALAYA_ACCOUNT is not set")
    return problems
