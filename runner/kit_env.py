#!/usr/bin/env python3
"""Turn a client's brand pack into the environment variables the kit's skills read.

The search skills (kit/skills/research/*) never read the brand pack themselves - they
read a small set of env vars. This runner is the single place that maps pack fields to
those vars, validates the pack, and resolves `env:NAME` references against the process
environment and the client's `.env` file.

Usage:
    python3 kit_env.py --pack ../my-sales/pack.json check
    python3 kit_env.py --pack ../my-sales/pack.json discovery-config --hypothesis my-segment --out run/config.json
    python3 kit_env.py --pack ../my-sales/pack.json print            # values of secret refs are masked
    eval "$(python3 kit_env.py --pack ../my-sales/pack.json export)"
    python3 kit_env.py --pack ../my-sales/pack.json --env-file ../my-sales/client.env \
        run -- python3 -m jobs.qualify queue --pack ../my-sales/pack.json --run-dir ../my-sales/run
    python3 kit_env.py --pack ../my-sales/pack.json --module ops --env-file .env run -- python3 -m bots.ops.telegram_listener
    python3 kit_env.py --pack ../my-sales/pack.json --module outreach --env-file .env run -- python3 outreach/cli.py run --send

`--module ops` builds the ops bot env instead (KIT_OPS_CONFIG JSON + KIT_OPS_BOT_TOKEN/KIT_OPS_CHAT_ID + sender
limits, see kit/bots/ops/config.py; the mail cards of kit/bots/mail ride in KIT_OPS_CONFIG["mail"]);
`--module outreach` builds KIT_OUTREACH_* for kit/outreach (mail.outreach); `--module jobs` builds KIT_JOBS_CONFIG for
kit/jobs plus the search env (the search chain needs it); `--module all` builds all of them. Every module that runs
the search skills gets KIT_BUDGETS (limits.budgets, the shared daily API budget of kit/jobs/budget.py).
    python3 kit_env.py --pack ../my-sales/pack.json --env-file .env cron [--format commands|json] [--server-tz Europe/Berlin]
prints the pack's jobs.list[] as Hermes cron jobs (nothing is installed).
Sections bot.*, team.*, offer.*, crm.*, mail.*, distribution, reports, jobs, limits.budgets are validated
structurally on every command when present.

`--pack` accepts a directory (containing pack.yaml or pack.json) or a file path.
YAML needs PyYAML (installed by the kit installer); JSON works with the stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

KNOWN_EMAIL_PROVIDERS = {"apollo", "findymail"}
KNOWN_VERIFIERS = {"reoon", "findymail", "mx"}
KNOWN_BACKENDS = {"keenable", "tavily", "serper"}
DEFAULT_NEGATIVE_TERMS = ["directory", "top 10", "comparison", "рейтинг", "топ-10"]
# Pack fields that point at files inside the pack directory.
FILE_FIELDS = [
    "company.description_file", "offer.facts_files", "targeting.verdict_prompt_file",
    "targeting.fit_rubric_file", "bot.concierge.persona_file", "bot.concierge.gate.rules_file",
    "mail.templates", "mail.first_messages",
    "bot.followup.templates", "bot.followup.partner_templates",
    "operations.texts",
]
COLUMN_RE = re.compile(r"[A-Z]{1,3}")
OPS_MODES = ("off", "draft", "auto")
ROUTE_ACTIONS = ("auto", "draft", "handoff")
KNOWN_CRM = ("none", "amocrm", "hubspot", "bitrix24")
TRACKER_KEYS = ("telegram", "stage", "status", "next_touch", "name", "company", "account", "lead_type")
MAIL_PROVIDERS = ("himalaya", "gmail_api", "ms_graph")
OUTREACH_TRANSPORTS = ("smtp", "himalaya", "outbox")   # outbox: demo/e2e only, nothing leaves the host
OUTREACH_COLUMN_KEYS = ("email", "first_name", "company", "template")
MAIL_TRACKER_KEYS = ("email", "site", "company", "stage")
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
BUDGET_PROVIDERS = ("serper", "findymail", "publicwww", "tavily", "apollo")   # kit/jobs/budget.py
JOB_TEMPLATES = ("distribute_leads", "tracker_report", "budget_report", "search_chain", "followup_check")
JOB_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
CRON_FIELD_RE = re.compile(r"(\*|\d+(-\d+)?)(/\d+)?(,(\*|\d+(-\d+)?)(/\d+)?)*")
MATCH_KEYS = ("geo", "hypothesis", "lead_type")
DIST_COLUMNS = ("site", "owner")            # sheets.columns needed by distribution
REPORT_COLUMNS = ("status", "first_touch")  # sheets.columns needed by the tracker report
MAILBOX_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")
ENV_PREFIX_RE = re.compile(r"[A-Z][A-Z0-9_]*")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
HHMM_RE = re.compile(r"([01]?\d|2[0-3]):[0-5]\d")


class PackError(Exception):
    pass


# --------------------------------------------------------------------------- loading

def load_pack(path: Path) -> tuple[dict, Path]:
    """Returns (pack data, pack directory)."""
    if path.is_dir():
        for name in ("pack.yaml", "pack.yml", "pack.json"):
            if (path / name).exists():
                path = path / name
                break
        else:
            raise PackError(f"no pack.yaml/pack.json in {path}")
    if not path.exists():
        raise PackError(f"pack file not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as e:
            raise PackError("PyYAML is not installed; use pack.json or `pip install pyyaml`") from e
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            raise PackError(f"Invalid YAML in {path.name}{location}") from exc
    if not isinstance(data, dict):
        raise PackError("pack root must be a mapping")
    return data, path.parent


def load_env_file(path: Path | None) -> dict:
    """Minimal .env parser: KEY=VALUE lines, optional quotes, # comments, optional `export`."""
    out: dict[str, str] = {}
    if not path or not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


# --------------------------------------------------------------------------- helpers

def get(pack: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = pack
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def resolve(value: Any, env: dict, field: str, problems: list) -> tuple[str | None, bool]:
    """Returns (resolved string or None, came_from_env_ref)."""
    if value is None:
        return None, False
    if isinstance(value, str) and value.startswith("env:"):
        name = value[4:]
        if name not in env or env[name] == "":
            problems.append(f"{field}: env var {name} is not set (process env or --env-file)")
            return None, True
        return env[name], True
    return str(value), False


def tab_name(tab: Any) -> Any:
    return tab.get("name") if isinstance(tab, dict) else tab


# --------------------------------------------------------------------------- mapping

def build_env(pack: dict, env: dict) -> tuple[dict, set, list]:
    """Map pack -> kit env vars. Returns (vars, names_from_env_refs, problems)."""
    problems: list[str] = []
    out: dict[str, str] = {}
    from_ref: set[str] = set()

    def put(var: str, value: Any, field: str, required: bool = False) -> None:
        resolved, is_ref = resolve(value, env, field, problems)
        if resolved is None:
            if required and value is None:
                problems.append(f"{field}: required")
            return
        out[var] = resolved
        if is_ref:
            from_ref.add(var)

    # sheets
    put("SHEET_ID", get(pack, "sheets.spreadsheet_id"), "sheets.spreadsheet_id", required=True)
    put("UNSORTED_TAB", tab_name(get(pack, "sheets.tabs.unsorted")), "sheets.tabs.unsorted", required=True)
    columns = get(pack, "sheets.columns") or {}
    if not isinstance(columns, dict):
        problems.append("sheets.columns: must be a mapping {site, telegram, email}")
        columns = {}
    for key in ("site", "telegram", "email"):
        col = columns.get(key)
        if col is None:
            problems.append(f"sheets.columns.{key}: required (column letter, e.g. F)")
            continue
        col = str(col).strip().upper()
        if not COLUMN_RE.fullmatch(col):
            problems.append(f"sheets.columns.{key}: {col!r} is not a column letter")
            continue
        out[f"KIT_COL_{key.upper()}"] = col
    used = [out.get(f"KIT_COL_{k.upper()}") for k in ("site", "telegram", "email")]
    if all(used) and len(set(used)) != 3:
        problems.append(f"sheets.columns: site/telegram/email must be different columns, got {used}")

    # targeting
    kw = get(pack, "targeting.contact_keywords", [])
    if not isinstance(kw, list) or not all(isinstance(k, str) and k.strip() for k in kw):
        problems.append("targeting.contact_keywords: must be a list of non-empty strings")
    elif any("," in k for k in kw):
        problems.append("targeting.contact_keywords: keywords must not contain commas")
    else:
        out["KIT_CONTACT_KEYWORDS"] = ",".join(k.strip() for k in kw)

    # search
    providers = get(pack, "search.email_providers", sorted(KNOWN_EMAIL_PROVIDERS))
    if not isinstance(providers, list) or not providers:
        problems.append("search.email_providers: must be a non-empty list")
    else:
        unknown = set(map(str, providers)) - KNOWN_EMAIL_PROVIDERS
        if unknown:
            problems.append(f"search.email_providers: unknown {sorted(unknown)} (known: {sorted(KNOWN_EMAIL_PROVIDERS)})")
        else:
            out["KIT_EMAIL_PROVIDERS"] = ",".join(providers)
    verifiers = get(pack, "search.verifiers", [])
    if verifiers and (not isinstance(verifiers, list) or set(map(str, verifiers)) - KNOWN_VERIFIERS):
        problems.append(f"search.verifiers: must be a subset of {sorted(KNOWN_VERIFIERS)}")
    phone = get(pack, "search.phone_lookup", False)
    if not isinstance(phone, bool):
        problems.append("search.phone_lookup: must be true or false")
    elif phone:
        out["KIT_PHONE_LOOKUP"] = "1"
    put("KIT_BOT_USER_AGENT", get(pack, "search.bot_user_agent"), "search.bot_user_agent")

    # limits.budgets: one daily ledger shared by every chain on the host (kit/jobs/budget.py)
    limits, b_problems = budget_limits(pack)
    problems += b_problems
    if limits is not None and not b_problems:
        out["KIT_BUDGETS"] = ",".join(f"{p}={n}" for p, n in sorted(limits.items()))
        out["KIT_BUDGET_TZ"] = str(get(pack, "company.timezone") or "UTC")
        home = (env.get("KIT_HOME") or env.get("HERMES_HOME") or "").strip()
        if home:
            out["KIT_BUDGET_LEDGER"] = str(Path(home) / "budget" / "ledger.json")

    return out, from_ref, problems


def enabled_paid_providers(pack: dict) -> dict:
    """Paid providers the pack explicitly switches on -> the field that does it."""
    found: dict[str, str] = {}
    for p in get(pack, "targeting.search_providers", []) or []:
        if p in BUDGET_PROVIDERS:
            found.setdefault(p, "targeting.search_providers")
    for h in get(pack, "targeting.hypotheses", []) or []:
        for q in (h.get("queries") or []) if isinstance(h, dict) else []:
            for b in (q.get("backends") or []) if isinstance(q, dict) else []:
                if b in BUDGET_PROVIDERS:
                    found.setdefault(b, "targeting.hypotheses[].queries[].backends")
    for p in get(pack, "search.email_providers", []) or []:
        if p in BUDGET_PROVIDERS:
            found.setdefault(p, "search.email_providers")
    if "findymail" in (get(pack, "search.verifiers", []) or []):
        found.setdefault("findymail", "search.verifiers")
    return found


def budget_limits(pack: dict) -> tuple[dict | None, list]:
    """limits.budgets.<provider>_daily -> {provider: n}. Fail closed: a paid provider the pack enables needs a budget."""
    problems = []
    raw = get(pack, "limits.budgets")
    limits: dict[str, int] = {}
    if raw is not None:
        if not isinstance(raw, dict):
            return None, ["limits.budgets: must be a mapping {<provider>_daily: N}"]
        for key, value in raw.items():
            name = str(key)[:-len("_daily")] if str(key).endswith("_daily") else None
            if name not in BUDGET_PROVIDERS:
                problems.append(f"limits.budgets.{key}: unknown (known: "
                                f"{', '.join(p + '_daily' for p in BUDGET_PROVIDERS)})")
            elif not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
                problems.append(f"limits.budgets.{key}: must be an integer >= 0 (0 = provider off)")
            else:
                limits[name] = value
    for provider, field in sorted(enabled_paid_providers(pack).items()):
        if provider not in limits and f"{provider}_daily" not in (raw or {}):
            problems.append(f"limits.budgets.{provider}_daily: required, {field} enables {provider} "
                            "(paid calls without a budget are refused)")
    return (limits if raw is not None else None), problems


def check_files(pack: dict, base: Path) -> list:
    """Every file the pack references must exist inside the pack directory."""
    problems = []
    for field in FILE_FIELDS:
        value = get(pack, field)
        if value is None:
            continue
        values = list(value.values()) if isinstance(value, dict) else value if isinstance(value, list) else [value]
        for rel in values:
            p = (base / str(rel)).resolve()
            if base.resolve() not in p.parents:
                problems.append(f"{field}: {rel!r} points outside the pack directory")
            elif not p.is_file():
                problems.append(f"{field}: file {rel!r} not found in the pack")
    return problems


def check_operations(pack: dict) -> list:
    """Validate the optional operator text bundle independently of credentials."""
    section = pack.get("operations")
    if section is None:
        return []
    if not isinstance(section, dict) or not isinstance(section.get("texts"), dict):
        return ["operations.texts: required mapping"]
    texts = section["texts"]
    problems = []
    for key in ("dialog", "onboarding", "handoff"):
        if not isinstance(texts.get(key), str) or not texts[key].strip():
            problems.append(f"operations.texts.{key}: required file path")
    if set(texts) - {"dialog", "onboarding", "handoff"}:
        problems.append("operations.texts: unknown text key")
    return problems


def ops_context(pack: dict, base: Path) -> dict:
    """Expose an allowlisted, offline context; never resolve credential references."""
    problems = check_operations(pack) + check_files(pack, base)
    if pack.get("operations") is None:
        problems.append("operations: required for ops-context")
    if problems:
        raise PackError("; ".join(problems))
    return {
        "company": {k: get(pack, "company." + k) for k in ("name", "language", "timezone")},
        "texts": {k: (base / rel).read_text(encoding="utf-8")
                  for k, rel in pack["operations"]["texts"].items()},
        "crm_type": get(pack, "crm.type", "none"),
        "mode": get(pack, "bot.ops.mode"),
        "stages": get(pack, "bot.concierge.stages", []),
        "routes": get(pack, "bot.concierge.routes", []),
        "columns": get(pack, "sheets.columns", {}),
        "protected_columns": get(pack, "sheets.protected_columns", []),
    }


def check_hypotheses(pack: dict) -> list:
    problems = []
    hyps = get(pack, "targeting.hypotheses", []) or []
    if not isinstance(hyps, list):
        return ["targeting.hypotheses: must be a list"]
    seen = set()
    for i, h in enumerate(hyps):
        where = f"targeting.hypotheses[{i}]"
        if not isinstance(h, dict) or not h.get("id"):
            problems.append(f"{where}: needs an id")
            continue
        if h["id"] in seen:
            problems.append(f"{where}: duplicate id {h['id']!r}")
        seen.add(h["id"])
        for j, q in enumerate(h.get("queries") or []):
            if not isinstance(q, dict) or not str(q.get("text", "")).strip():
                problems.append(f"{where}.queries[{j}]: needs text")
                continue
            bad = set(q.get("backends") or []) - KNOWN_BACKENDS
            if not q.get("backends") or bad:
                problems.append(f"{where}.queries[{j}]: backends must be a non-empty subset of {sorted(KNOWN_BACKENDS)}")
    return problems


# --------------------------------------------------------------------------- ops bot (bot.*, team.*, offer.*, crm.*)

def _num(v, minimum=0.0) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= minimum


def team_accounts(pack: dict) -> list:
    out = []
    for m in get(pack, "team.members", []) or []:
        if isinstance(m, dict):
            out += list(((m.get("telegram") or {}).get("accounts")) or [])
    return out


def ops_accounts(pack: dict) -> list:
    """bot.ops.accounts, else the accounts of bot.ops.member, else every team account."""
    explicit = get(pack, "bot.ops.accounts")
    if explicit:
        return list(explicit)
    member = get(pack, "bot.ops.member")
    if member:
        for m in get(pack, "team.members", []) or []:
            if isinstance(m, dict) and m.get("id") == member:
                return list(((m.get("telegram") or {}).get("accounts")) or [])
    return team_accounts(pack)


def check_ops_sections(pack: dict) -> list:
    """Structural validation of bot.*, team.*, offer.*, crm.* (runs whenever these sections are present)."""
    problems = []
    members = get(pack, "team.members", []) or []
    if not isinstance(members, list):
        problems.append("team.members: must be a list")
        members = []
    ids = [m.get("id") for m in members if isinstance(m, dict)]
    if len(ids) != len(members) or not all(ids):
        problems.append("team.members: every member needs an id")
    if len(set(ids)) != len(ids):
        problems.append("team.members: duplicate ids")
    for i, m in enumerate(members):
        accts = ((m or {}).get("telegram") or {}).get("accounts", [])
        if not isinstance(accts, list) or not all(isinstance(a, str) and a for a in accts):
            problems.append(f"team.members[{i}].telegram.accounts: must be a list of session names")
    markers = get(pack, "team.internal_markers", [])
    if not isinstance(markers, list) or not all(isinstance(x, str) and x.strip() for x in markers):
        problems.append("team.internal_markers: must be a list of non-empty strings")
    all_accts = set(team_accounts(pack))
    off = get(pack, "bot.sender.outreach_disabled", []) or []
    if not isinstance(off, list) or not all(isinstance(a, str) for a in off):
        problems.append("bot.sender.outreach_disabled: must be a list of Telegram account names")
    elif set(off) - all_accts:
        problems.append(f"bot.sender.outreach_disabled: {sorted(set(off) - all_accts)} are not accounts of team.members")

    tariffs = get(pack, "offer.tariffs", []) or []
    if not isinstance(tariffs, list):
        problems.append("offer.tariffs: must be a list")
    else:
        t_ids = []
        for i, t in enumerate(tariffs):
            if not isinstance(t, dict) or not t.get("id") or not t.get("name"):
                problems.append(f"offer.tariffs[{i}]: needs id and name")
                continue
            t_ids.append(t["id"])
            if t.get("fee_percent") is not None and not _num(t["fee_percent"]):
                problems.append(f"offer.tariffs[{i}].fee_percent: must be a non-negative number or null")
        if len(set(t_ids)) != len(t_ids):
            problems.append("offer.tariffs: duplicate ids")

    ops = get(pack, "bot.ops") or {}
    if ops:
        if ops.get("mode", "draft") not in OPS_MODES:
            problems.append(f"bot.ops.mode: must be one of {list(OPS_MODES)}")
        for acct, mode in (ops.get("account_modes") or {}).items():
            if mode not in OPS_MODES:
                problems.append(f"bot.ops.account_modes.{acct}: must be one of {list(OPS_MODES)}")
            if acct not in all_accts:
                problems.append(f"bot.ops.account_modes.{acct}: not an account of team.members")
        unknown = set(ops.get("accounts") or []) - all_accts
        if unknown:
            problems.append(f"bot.ops.accounts: {sorted(unknown)} are not accounts of team.members")
        if ops.get("member") and ops["member"] not in ids:
            problems.append(f"bot.ops.member: {ops['member']!r} is not a team member id")
        if "batching_seconds" in ops and not _num(ops["batching_seconds"]):
            problems.append("bot.ops.batching_seconds: must be a number >= 0")
        cats = ops.get("reply_categories", [])
        if not isinstance(cats, list) or not all(isinstance(c, str) and c.strip() for c in cats):
            problems.append("bot.ops.reply_categories: must be a list of non-empty strings")

    conc = get(pack, "bot.concierge") or {}
    if conc:
        if "daily_limit" in conc and not (isinstance(conc["daily_limit"], int) and conc["daily_limit"] >= 0):
            problems.append("bot.concierge.daily_limit: must be an integer >= 0 (0 = no auto replies)")
        for k, v in (conc.get("typing_delay") or {}).items():
            if not _num(v):
                problems.append(f"bot.concierge.typing_delay.{k}: must be a number >= 0")
        if "takeover_hours" in conc and not _num(conc["takeover_hours"]):
            problems.append("bot.concierge.takeover_hours: must be a number >= 0")
        gate = conc.get("gate") or {}
        if not isinstance(gate.get("enabled", True), bool):
            problems.append("bot.concierge.gate.enabled: must be true or false")
        elif gate.get("enabled", True) and not gate.get("rules_file"):
            problems.append("bot.concierge.gate.rules_file: required when the gate is enabled")
        st_ids = [s.get("id") for s in conc.get("stages") or [] if isinstance(s, dict)]
        if len(st_ids) != len(conc.get("stages") or []) or not all(st_ids) or len(set(st_ids)) != len(st_ids):
            problems.append("bot.concierge.stages: every stage needs a unique id")
        r_ids = []
        for i, r in enumerate(conc.get("routes") or []):
            where = f"bot.concierge.routes[{i}]"
            if not isinstance(r, dict) or not r.get("id"):
                problems.append(f"{where}: needs an id")
                continue
            r_ids.append(r["id"])
            if r.get("action") not in ROUTE_ACTIONS:
                problems.append(f"{where}: action must be one of {list(ROUTE_ACTIONS)}")
            match = r.get("match") or {}
            if not (match.get("categories") or match.get("keywords")):
                problems.append(f"{where}: match needs categories and/or keywords")
            bad = set(match.get("categories") or []) - set(ops.get("reply_categories") or []) - {"cold_inbound", "first_reply"}
            if bad:
                problems.append(f"{where}: unknown categories {sorted(bad)} (bot.ops.reply_categories, cold_inbound, first_reply)")
        if len(set(r_ids)) != len(r_ids):
            problems.append("bot.concierge.routes: duplicate ids")

    fu = get(pack, "bot.followup") or {}
    if fu:
        if not isinstance(fu.get("templates"), dict) or not fu["templates"]:
            problems.append("bot.followup.templates: must map a language to a template file")
        if not fu.get("status_value"):
            problems.append("bot.followup.status_value: required (tracker status of rows due for follow-up)")
        steps = fu.get("step_values", ["1", "2", "3"])
        if not isinstance(steps, list) or not steps:
            problems.append("bot.followup.step_values: must be a non-empty list")
        unknown = set(fu.get("accounts") or []) - all_accts
        if unknown:
            problems.append(f"bot.followup.accounts: {sorted(unknown)} are not accounts of team.members")
        cols = get(pack, "sheets.columns") or {}
        for key in ("telegram", "stage", "status", "next_touch"):
            if not cols.get(key):
                problems.append(f"sheets.columns.{key}: required by bot.followup")

    crm = get(pack, "crm") or {}
    if crm:
        kind = crm.get("type", "none")
        if kind not in KNOWN_CRM:
            problems.append(f"crm.type: {kind!r} unknown (known: {list(KNOWN_CRM)})")
        if kind in ("hubspot", "bitrix24"):
            key = "token" if kind == "hubspot" else "webhook"
            if not re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", str(get(pack, f"crm.{kind}.{key}") or "")):
                problems.append(f"crm.{kind}.{key}: must be an env:NAME reference")
            if crm.get("advance_on_reply") or crm.get("task_on_reply"):
                problems.append(f"crm.{kind} supports lookup and notes only; disable advance_on_reply and task_on_reply")
        if kind == "amocrm":
            amo = crm.get("amocrm") or {}
            for k in ("base_url", "token"):
                if not str(amo.get(k, "")).startswith("env:"):
                    problems.append(f"crm.amocrm.{k}: must be an env:NAME reference (no secrets in the pack)")
            if not isinstance(amo.get("pipelines", {}), dict):
                problems.append("crm.amocrm.pipelines: must be a mapping")
    return problems


def _abs(base: Path, rel) -> str | None:
    return str((base / str(rel)).resolve()) if rel else None


# --------------------------------------------------------------------------- mail (mail.accounts, mail.bot, mail.outreach)

def mail_accounts(pack: dict) -> list:
    accts = get(pack, "mail.accounts", []) or []
    return [a for a in accts if isinstance(a, dict)] if isinstance(accts, list) else []


def mailbox_sender_name(pack: dict, acct: dict) -> str:
    """mail.accounts[].sender_name, else display_name of the team member whose mail_account is this address."""
    if str(acct.get("sender_name") or "").strip():
        return str(acct["sender_name"]).strip()
    addr = str(acct.get("address", "")).lower()
    for m in get(pack, "team.members", []) or []:
        if isinstance(m, dict) and str(m.get("mail_account", "")).lower() == addr and m.get("display_name"):
            return str(m["display_name"])
    return ""


def mailbox_provider(pack: dict, acct: dict) -> str:
    return acct.get("provider") or get(pack, "mail.provider") or "himalaya"


def check_mail_sections(pack: dict) -> list:
    """Structural validation of mail.accounts and mail.bot (mail.outreach is checked by build_outreach_env)."""
    problems = []
    mail = get(pack, "mail")
    if mail is None:
        return problems
    if not isinstance(mail, dict):
        return ["mail: must be a mapping"]
    if mail.get("provider") is not None and mail["provider"] not in MAIL_PROVIDERS:
        problems.append(f"mail.provider: must be one of {list(MAIL_PROVIDERS)}")
    raw = mail.get("accounts", [])
    if not isinstance(raw, list):
        return problems + ["mail.accounts: must be a list"]
    ids, addrs = [], []
    for i, a in enumerate(raw):
        where = f"mail.accounts[{i}]"
        if not isinstance(a, dict):
            problems.append(f"{where}: must be a mapping {{id, address, env_prefix}}")
            continue
        if not MAILBOX_ID_RE.fullmatch(str(a.get("id", ""))):
            problems.append(f"{where}.id: lowercase letters/digits/-/_, up to 24 chars")
        if not EMAIL_RE.fullmatch(str(a.get("address", ""))):
            problems.append(f"{where}.address: not an email address")
        if not ENV_PREFIX_RE.fullmatch(str(a.get("env_prefix", ""))):
            problems.append(f"{where}.env_prefix: required, UPPER_SNAKE (secrets are <env_prefix>_* in .env)")
        if a.get("provider") is not None and a["provider"] not in MAIL_PROVIDERS:
            problems.append(f"{where}.provider: must be one of {list(MAIL_PROVIDERS)}")
        if "outreach" in a and not isinstance(a["outreach"], bool):
            problems.append(f"{where}.outreach: must be true or false")
        bot = a.get("bot") or {}
        if not isinstance(bot, dict):
            problems.append(f"{where}.bot: must be a mapping {{notify, reply}}")
        else:
            if "notify" in bot and not isinstance(bot["notify"], bool):
                problems.append(f"{where}.bot.notify: must be true or false")
            if bot.get("reply", "off") not in ("off", "draft"):
                problems.append(f"{where}.bot.reply: must be off or draft (mail has no auto mode)")
        ids.append(a.get("id"))
        addrs.append(str(a.get("address", "")).lower())
    if len(set(ids)) != len(ids) or len(set(addrs)) != len(addrs):
        problems.append("mail.accounts: duplicate id or address")

    bot = mail.get("bot")
    if bot is not None:
        if not isinstance(bot, dict):
            return problems + ["mail.bot: must be a mapping"]
        if not isinstance(bot.get("enabled", False), bool):
            problems.append("mail.bot.enabled: must be true or false")
        if "poll_seconds" in bot and not (isinstance(bot["poll_seconds"], int) and bot["poll_seconds"] >= 30):
            problems.append("mail.bot.poll_seconds: must be an integer >= 30")
        unknown = set(bot.get("accounts") or []) - set(ids)
        if unknown:
            problems.append(f"mail.bot.accounts: {sorted(unknown)} are not ids of mail.accounts")
        secret = bot.get("gmail_client_secret")
        if secret is not None and not str(secret).startswith("env:"):
            problems.append("mail.bot.gmail_client_secret: must be an env:NAME reference (a path on the host, not in the pack)")
        chosen = [a for a in raw if isinstance(a, dict) and (not bot.get("accounts") or a.get("id") in bot["accounts"])]
        if bot.get("enabled") and secret is None and any(mailbox_provider(pack, a) == "gmail_api" for a in chosen):
            problems.append("mail.bot.gmail_client_secret: required when a mailbox of the bot uses gmail_api")
        presets = (bot.get("connect") or {}).get("presets", [])
        if not isinstance(presets, list):
            problems.append("mail.bot.connect.presets: must be a list")
            presets = []
        p_ids = []
        for j, p in enumerate(presets):
            where = f"mail.bot.connect.presets[{j}]"
            if not isinstance(p, dict) or not p.get("id"):
                problems.append(f"{where}: needs an id")
                continue
            p_ids.append(p["id"])
            if not re.match(r"imaps?://[^:/]+(:\d+)?$", str(p.get("imap", ""))):
                problems.append(f"{where}.imap: expected imaps://host:port")
            if not re.match(r"smtps?://[^:/]+(:\d+)?$", str(p.get("smtp", ""))):
                problems.append(f"{where}.smtp: expected smtp://host:port or smtps://host:port")
            mx = p.get("mx_contains", [])
            if not isinstance(mx, list) or not all(isinstance(x, str) and x for x in mx):
                problems.append(f"{where}.mx_contains: must be a list of MX host substrings")
        if len(set(p_ids)) != len(p_ids):
            problems.append("mail.bot.connect.presets: duplicate ids")
    return problems


def build_mail_bot_config(pack: dict, env: dict, problems: list, out: dict, from_ref: set) -> dict | None:
    """KIT_OPS_CONFIG["mail"] for kit/bots/mail (only when mail.bot.enabled) + KIT_MAIL_GMAIL_CLIENT_SECRET."""
    bot = get(pack, "mail.bot") or {}
    if not bot.get("enabled"):
        return None
    chosen = [a for a in mail_accounts(pack) if not bot.get("accounts") or a.get("id") in bot["accounts"]]
    needs_gmail = any(mailbox_provider(pack, a) == "gmail_api" for a in chosen)
    ref = bot.get("gmail_client_secret")
    # without Gmail mailboxes the OAuth client is optional (only "connect Gmail" needs it): an unset env var is fine
    if ref and (needs_gmail or env.get(str(ref)[4:])):
        resolved, is_ref = resolve(ref, env, "mail.bot.gmail_client_secret", problems)
        if resolved:
            out["KIT_MAIL_GMAIL_CLIENT_SECRET"] = resolved
            if is_ref:
                from_ref.add("KIT_MAIL_GMAIL_CLIENT_SECRET")
    columns = get(pack, "sheets.columns") or {}
    member = get(pack, "bot.ops.member")
    tab = next((m.get("tracker_tab") for m in get(pack, "team.members", []) or []
                if isinstance(m, dict) and m.get("id") == member), None) or tab_name(get(pack, "sheets.tabs.tracker"))
    return {
        "enabled": True,
        "poll_seconds": int(bot.get("poll_seconds", 180)),
        "mailboxes": [{"id": a["id"], "address": str(a["address"]).lower(), "provider": mailbox_provider(pack, a),
                       "env_prefix": a.get("env_prefix", ""), "sender_name": mailbox_sender_name(pack, a),
                       "outreach": bool(a.get("outreach", False)),
                       "notify": bool((a.get("bot") or {}).get("notify", True)),
                       "reply": (a.get("bot") or {}).get("reply", "off")} for a in chosen],
        "connect": {"presets": list((bot.get("connect") or {}).get("presets") or [])},
        "tracker": {"tab": tab, "columns": {k: v for k, v in columns.items() if k in MAIL_TRACKER_KEYS}},
    }


def _day_list(days) -> str | None:
    out = []
    for d in days:
        if isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 7:
            out.append(DAY_NAMES[d - 1])
        elif isinstance(d, str) and d.strip().lower()[:3] in DAY_NAMES:
            out.append(d.strip().lower()[:3])
        else:
            return None
    return ",".join(out) if out else None


def _pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def build_outreach_env(pack: dict, env: dict, base: Path) -> tuple[dict, set, list]:
    """mail.* -> KIT_OUTREACH_* for kit/outreach (mapping: kit/outreach/PACK_FIELDS.md). Runs only with mail.outreach."""
    problems: list[str] = []
    out: dict[str, str] = {}
    o = get(pack, "mail.outreach")
    if not isinstance(o, dict):
        return out, set(), ["mail.outreach: required for --module outreach"]
    P = "mail.outreach"

    # mailboxes: every one of them carries the pack's outreach flag (no default: the pack must say it)
    boxes = []
    for i, a in enumerate(mail_accounts(pack)):
        where = f"mail.accounts[{i}]"
        if not isinstance(a.get("outreach"), bool):
            problems.append(f"{where}.outreach: required with mail.outreach (true = may send outreach, false = never)")
            continue
        if a["outreach"] and mailbox_provider(pack, a) in ("gmail_api", "ms_graph"):
            problems.append(f"{where}: {mailbox_provider(pack, a)} mailboxes cannot send outreach (kit/outreach sends over smtp/himalaya); "
                            "set outreach: false")
            continue
        name = mailbox_sender_name(pack, a)
        if a["outreach"] and not name:
            problems.append(f"{where}.sender_name: required ({{{{sender_name}}}} in templates); or team.members[].display_name "
                            "of the member with this mail_account")
        box = {"id": a.get("id"), "address": str(a.get("address", "")).lower(), "env_prefix": a.get("env_prefix"),
               "sender_name": name or str(a.get("id")), "outreach": a["outreach"]}
        if a.get("warmup_from"):
            try:
                import datetime as _dt
                _dt.date.fromisoformat(str(a["warmup_from"]))
                box["warmup_from"] = str(a["warmup_from"])
            except ValueError:
                problems.append(f"{where}.warmup_from: expected YYYY-MM-DD")
        boxes.append(box)
    if not any(b["outreach"] for b in boxes) and not problems:
        problems.append("mail.accounts: no mailbox has outreach: true, kit/outreach would have nothing to send from")
    out["KIT_OUTREACH_ACCOUNTS"] = json.dumps(boxes, ensure_ascii=False)

    transport = o.get("transport")
    if transport is None:
        provider = get(pack, "mail.provider")
        if provider == "gmail_api":
            problems.append(f"{P}.transport: mail.provider is gmail_api, which kit/outreach cannot send through; "
                            f"set {P}.transport: smtp or himalaya")
        transport = provider or None
    if transport is None:
        problems.append(f"{P}.transport: required (smtp | himalaya), or mail.provider: himalaya")
    elif transport not in OUTREACH_TRANSPORTS:
        problems.append(f"{P}.transport: must be one of {list(OUTREACH_TRANSPORTS)}")
    else:
        out["KIT_OUTREACH_TRANSPORT"] = transport

    tpls = get(pack, "mail.templates") or []
    if not isinstance(tpls, list) or not tpls:
        problems.append("mail.templates: required (list of email template files)")
    else:
        kit_dir = str(Path(__file__).resolve().parents[1])
        if kit_dir not in sys.path:
            sys.path.insert(0, kit_dir)
        from outreach.templates import TemplateError, parse_template
        paths = []
        for rel in tpls:
            p = (base / str(rel)).resolve()
            paths.append(str(p))
            if p.is_file():
                try:
                    parse_template(p)
                except TemplateError as e:
                    problems.append(f"mail.templates: {e}")
        out["KIT_OUTREACH_TEMPLATES"] = ",".join(paths)

    if not _pos_int(o.get("daily_cap_per_mailbox")):
        problems.append(f"{P}.daily_cap_per_mailbox: required, integer >= 1")
    else:
        out["KIT_OUTREACH_DAILY_CAP"] = str(o["daily_cap_per_mailbox"])

    w = o.get("warmup")
    if w is False or (isinstance(w, str) and w.strip().lower() == "off"):
        out["KIT_OUTREACH_WARMUP"] = "off"
    elif isinstance(w, dict) and _pos_int(w.get("start")) and isinstance(w.get("step"), int) and w["step"] >= 0:
        out["KIT_OUTREACH_WARMUP"] = f"start={w['start']},step={w['step']}"
    else:
        problems.append(f"{P}.warmup: required, {{start: N>=1, step: N>=0}} or off (warm-up is switched off only explicitly)")

    ps = o.get("pause_seconds")
    if isinstance(ps, dict) and all(isinstance(ps.get(k), (int, float)) and not isinstance(ps.get(k), bool)
                                    for k in ("min", "max")) and 0 <= ps["min"] <= ps["max"]:
        out["KIT_OUTREACH_PAUSE_SECONDS"] = f"{ps['min']:g}-{ps['max']:g}"
    else:
        problems.append(f"{P}.pause_seconds: required, {{min, max}} seconds with 0 <= min <= max")

    sw = o.get("send_window") or {}
    if not sw.get("tz"):
        problems.append(f"{P}.send_window.tz: required (IANA name, e.g. Europe/Berlin)")
    else:
        out["KIT_OUTREACH_WINDOW_TZ"] = str(sw["tz"])
    for key, var in (("from", "KIT_OUTREACH_WINDOW_FROM"), ("to", "KIT_OUTREACH_WINDOW_TO")):
        if not HHMM_RE.fullmatch(str(sw.get(key, ""))):
            problems.append(f"{P}.send_window.{key}: required, HH:MM")
        else:
            out[var] = str(sw[key])
    if sw.get("days") is not None:
        days = _day_list(sw["days"]) if isinstance(sw["days"], list) else None
        if days is None:
            problems.append(f"{P}.send_window.days: list of mon..sun (or 1..7)")
        else:
            out["KIT_OUTREACH_WINDOW_DAYS"] = days

    g = o.get("bounce_gate_percent")
    if not (isinstance(g, (int, float)) and not isinstance(g, bool) and 0 < g <= 100):
        problems.append(f"{P}.bounce_gate_percent: required, number in (0, 100]")
    else:
        out["KIT_OUTREACH_BOUNCE_GATE_PERCENT"] = f"{g:g}"
    for key, var in (("bounce_gate_min_sent", "KIT_OUTREACH_BOUNCE_MIN_SENT"),
                     ("bounce_window_days", "KIT_OUTREACH_BOUNCE_WINDOW_DAYS"), ("max_per_run", "KIT_OUTREACH_MAX_PER_RUN")):
        if key in o:
            if not _pos_int(o[key]):
                problems.append(f"{P}.{key}: integer >= 1")
            else:
                out[var] = str(o[key])
    if "round_robin" in o:
        if not isinstance(o["round_robin"], bool):
            problems.append(f"{P}.round_robin: must be true or false")
        else:
            out["KIT_OUTREACH_ROUND_ROBIN"] = "1" if o["round_robin"] else "0"

    leads = o.get("leads") or {}
    if leads.get("tab"):
        tab = tab_name(get(pack, f"sheets.tabs.{leads['tab']}"))
        if not tab:
            problems.append(f"{P}.leads.tab: {leads['tab']!r} is not a key of sheets.tabs")
        else:
            out["KIT_OUTREACH_LEADS"] = f"sheet:{tab}"
    cols = o.get("columns") or {}
    if not isinstance(cols, dict) or set(cols) - set(OUTREACH_COLUMN_KEYS):
        problems.append(f"{P}.columns: mapping with keys {list(OUTREACH_COLUMN_KEYS)} -> header of the column")
        cols = {}
    sheet_cols = get(pack, "sheets.columns") or {}
    mapping = {k: str(v) for k, v in cols.items()}
    for key in ("email", "company"):   # same tracker column the search modules write
        if key not in mapping and sheet_cols.get(key):
            mapping[key] = f"col:{str(sheet_cols[key]).upper()}"
    if any("," in v or "=" in v for v in mapping.values()):
        problems.append(f"{P}.columns: headers must not contain ',' or '='")
    elif mapping:
        out["KIT_OUTREACH_COLUMNS"] = ",".join(f"{k}={v}" for k, v in mapping.items())

    home = (env.get("KIT_HOME") or env.get("HERMES_HOME") or "").strip()
    if home:   # the operator's runtime switch from the ops bot (kit/bots/ops/store.py)
        out["KIT_OUTREACH_OFF_FILE"] = str(Path(home) / "ops_state" / "outreach_off.json")
    return out, set(), problems


def build_ops_env(pack: dict, env: dict, base: Path) -> tuple[dict, set, list]:
    """Map the pack to the ops bot env: KIT_OPS_CONFIG (JSON, non-secret) + secret/env-ref vars."""
    problems: list[str] = []
    out: dict[str, str] = {}
    from_ref: set[str] = set()

    def put(var, value, field, required=False):
        resolved, is_ref = resolve(value, env, field, problems)
        if resolved is None:
            if required and value is None:
                problems.append(f"{field}: required for the ops bot")
            return
        out[var] = resolved
        if is_ref:
            from_ref.add(var)

    if not get(pack, "bot.ops"):
        return out, from_ref, ["bot.ops: required for --module ops"]
    put("KIT_OPS_BOT_TOKEN", get(pack, "bot.ops.bot_token"), "bot.ops.bot_token", required=True)
    put("KIT_OPS_CHAT_ID", get(pack, "bot.ops.owner_chat"), "bot.ops.owner_chat", required=True)
    kind = get(pack, "crm.type")
    if kind in ("hubspot", "bitrix24"):
        key, var = ("token", "KIT_CRM_TOKEN") if kind == "hubspot" else ("webhook", "KIT_CRM_WEBHOOK")
        put(var, get(pack, f"crm.{kind}.{key}"), f"crm.{kind}.{key}", required=True)
    if get(pack, "crm.type") == "amocrm":
        put("KIT_CRM_BASE_URL", get(pack, "crm.amocrm.base_url"), "crm.amocrm.base_url", required=True)
        put("KIT_CRM_TOKEN", get(pack, "crm.amocrm.token"), "crm.amocrm.token", required=True)

    conc = dict(get(pack, "bot.concierge") or {})
    if conc.get("persona_file"):
        conc["persona_file"] = _abs(base, conc["persona_file"])
    if conc.get("gate"):
        conc["gate"] = dict(conc["gate"], rules_file=_abs(base, conc["gate"].get("rules_file")))
    fu = dict(get(pack, "bot.followup") or {})
    for key in ("templates", "partner_templates"):
        if fu.get(key):
            fu[key] = {lang: _abs(base, p) for lang, p in fu[key].items()}
    member = get(pack, "bot.ops.member")
    tab = next((m.get("tracker_tab") for m in get(pack, "team.members", []) or []
                if isinstance(m, dict) and m.get("id") == member), None) or tab_name(get(pack, "sheets.tabs.tracker"))
    crm = {k: v for k, v in (get(pack, "crm") or {"type": "none"}).items() if k != "amocrm"}
    if get(pack, "crm.type") == "amocrm":
        crm["amocrm"] = {"pipelines": get(pack, "crm.amocrm.pipelines") or {}}
    ops = dict(get(pack, "bot.ops"))
    ops.pop("bot_token", None)
    ops.pop("owner_chat", None)
    ops["accounts"] = ops_accounts(pack)
    cfg = {
        "company": {"name": get(pack, "company.name"), "description": get(pack, "company.tagline", ""),
                    "language": get(pack, "company.language", "en"), "timezone": get(pack, "company.timezone", "UTC")},
        "ops": ops,
        "concierge": conc,
        "internal_markers": get(pack, "team.internal_markers", []) or [],
        "internal_ids": get(pack, "team.internal_ids", []) or [],
        "facts_files": [_abs(base, p) for p in get(pack, "offer.facts_files", []) or []],
        "tariffs": get(pack, "offer.tariffs", []) or [],
        "llm": get(pack, "bot.llm", {}) or {},
        "crm": crm,
        "followup": fu,
        "tracker": {"tab": tab, "columns": {k: v for k, v in (get(pack, "sheets.columns") or {}).items() if k in TRACKER_KEYS}},
        "outreach_disabled": list(get(pack, "bot.sender.outreach_disabled", []) or []),
    }
    mail_cfg = build_mail_bot_config(pack, env, problems, out, from_ref)
    if mail_cfg:
        cfg["mail"] = mail_cfg
    if not cfg["company"]["name"]:
        problems.append("company.name: required for the ops bot")
    out["KIT_OPS_CONFIG"] = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    out["KIT_COMPANY_NAME"] = str(cfg["company"]["name"] or "")
    out["KIT_TIMEZONE"] = cfg["company"]["timezone"]
    sender = get(pack, "bot.sender") or {}
    if "daily_cap_per_account" in sender:
        out["KIT_TG_DAILY_CAP"] = str(int(sender["daily_cap_per_account"]))
    if sender.get("min_gap_s"):
        lo, hi = (list(sender["min_gap_s"]) * 2)[:2]
        out["KIT_TG_MIN_GAP_S"] = f"{lo},{hi}"
    if sender.get("text_guard_ignore"):
        out["KIT_TG_TEXT_GUARD_IGNORE"] = ",".join(sender["text_guard_ignore"])
    if sender.get("outreach_disabled"):
        out["KIT_TG_OUTREACH_DISABLED"] = ",".join(sender["outreach_disabled"])
    return out, from_ref, problems


# --------------------------------------------------------------------------- jobs (distribution, reports, jobs.list)

def _str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)


def cron_ok(expr) -> bool:
    fields = str(expr or "").split()
    return len(fields) == 5 and all(CRON_FIELD_RE.fullmatch(f) for f in fields)


def job_channels(pack: dict) -> dict:
    """Delivery channels: the built-in ops_chat (bot.ops.owner_chat) + jobs.channels."""
    out = {}
    if get(pack, "bot.ops.owner_chat"):
        out["ops_chat"] = {"platform": "telegram", "chat_id": get(pack, "bot.ops.owner_chat"), "chat_name": "ops"}
    for name, ch in (get(pack, "jobs.channels") or {}).items():
        out[name] = ch
    return out


def check_jobs_sections(pack: dict) -> list:
    """Structural validation of distribution, reports, jobs and the extra sheets.columns (whenever present)."""
    problems = []
    cols = get(pack, "sheets.columns") or {}
    if isinstance(cols, dict):
        for key, col in cols.items():
            if not COLUMN_RE.fullmatch(str(col).strip().upper()):
                problems.append(f"sheets.columns.{key}: {col!r} is not a column letter")
    members = {m.get("id"): m for m in get(pack, "team.members", []) or [] if isinstance(m, dict)}

    dist = get(pack, "distribution")
    if dist is not None:
        if not isinstance(dist, dict):
            return problems + ["distribution: must be a mapping"]
        for key in DIST_COLUMNS:
            if not cols.get(key):
                problems.append(f"sheets.columns.{key}: required by distribution")
        src = dist.get("source_tab", "unsorted")
        if not tab_name(get(pack, f"sheets.tabs.{src}")):
            problems.append(f"distribution.source_tab: {src!r} is not a key of sheets.tabs")
        if dist.get("fallback") not in ("share", "hold"):
            problems.append("distribution.fallback: required, share (all members by lead_share) or hold (leave unassigned)")
        if dist.get("owner_value", "display_name") not in ("display_name", "id"):
            problems.append("distribution.owner_value: display_name or id (what is written into the owner column)")
        cap = dist.get("daily_cap_per_member")
        if cap is not None and not _pos_int(cap):
            problems.append("distribution.daily_cap_per_member: integer >= 1")
        aliases = dist.get("geo_aliases") or {}
        if not isinstance(aliases, dict) or not all(_str_list(v) for v in aliases.values()):
            problems.append("distribution.geo_aliases: mapping region -> list of geo cell values")
        sharing = [mid for mid, m in members.items() if _num(m.get("lead_share")) and m["lead_share"] > 0]
        for mid, m in members.items():
            if m.get("lead_share") is not None and not _num(m.get("lead_share")):
                problems.append(f"team.members[{mid}].lead_share: must be a number >= 0")
        if dist.get("fallback") == "share" and not sharing:
            problems.append("distribution.fallback share: no team member has lead_share > 0")
        rules = dist.get("rules") or []
        if not isinstance(rules, list):
            problems.append("distribution.rules: must be a list")
            rules = []
        for i, r in enumerate(rules):
            where = f"distribution.rules[{i}]"
            if not isinstance(r, dict):
                problems.append(f"{where}: must be a mapping {{when, to}}")
                continue
            when = r.get("when") or {}
            if not isinstance(when, dict) or set(when) - set(MATCH_KEYS):
                problems.append(f"{where}.when: keys from {list(MATCH_KEYS)}")
            else:
                for key in when:
                    if not cols.get(key):
                        problems.append(f"{where}.when.{key}: needs sheets.columns.{key}")
            to = r.get("to")
            if not _str_list(to) or not to:
                problems.append(f"{where}.to: non-empty list of team member ids")
                continue
            for mid in to:
                if mid not in members:
                    problems.append(f"{where}.to: {mid!r} is not a team member id")
                elif mid not in sharing:
                    problems.append(f"{where}.to: {mid!r} needs team.members[].lead_share > 0")

    rep = get(pack, "reports")
    if rep is not None:
        if not isinstance(rep, dict):
            return problems + ["reports: must be a mapping"]
        for legacy in ("schedule", "channels"):
            if legacy in rep:
                problems.append(f"reports.{legacy}: moved to jobs.list[] (template: tracker_report, schedule, deliver)")
        st = rep.get("statuses") or {}
        if not isinstance(st, dict) or not all(_str_list(st.get(k)) for k in ("active", "closed")):
            problems.append("reports.statuses: required, {active: [...], won: [...], closed: [...]} tracker status values")
        elif st.get("won") is not None and not _str_list(st["won"]):
            problems.append("reports.statuses.won: list of status values")
        lt = rep.get("lead_types")
        if lt is not None and (not isinstance(lt, dict) or set(lt) - {"lead", "partner"}
                               or not all(_str_list(v) for v in lt.values())):
            problems.append("reports.lead_types: {lead: [...], partner: [...]} values of the lead_type column")
        for key in REPORT_COLUMNS:
            if not cols.get(key):
                problems.append(f"sheets.columns.{key}: required by reports")
        if not any(isinstance(m, dict) and m.get("tracker_tab") for m in members.values()):
            problems.append("reports: no team member has a tracker_tab to report on")
        if "list_limit" in rep and not _pos_int(rep["list_limit"]):
            problems.append("reports.list_limit: integer >= 1")
        if rep.get("language") is not None and rep["language"] not in ("ru", "en"):
            problems.append("reports.language: ru or en")

    jobs = get(pack, "jobs")
    if jobs is not None:
        if not isinstance(jobs, dict):
            return problems + ["jobs: must be a mapping {channels, list}"]
        channels = job_channels(pack)
        for name, ch in (jobs.get("channels") or {}).items():
            if not isinstance(ch, dict) or ch.get("platform") != "telegram" or not str(ch.get("chat_id", "")).startswith("env:"):
                problems.append(f"jobs.channels.{name}: {{platform: telegram, chat_id: env:NAME}}")
        seen = set()
        hyps = {h.get("id"): h for h in get(pack, "targeting.hypotheses", []) or [] if isinstance(h, dict)}
        for i, j in enumerate(jobs.get("list") or []):
            where = f"jobs.list[{i}]"
            if not isinstance(j, dict) or not JOB_ID_RE.fullmatch(str(j.get("id", ""))):
                problems.append(f"{where}.id: lowercase letters/digits/-, up to 40 chars")
                continue
            if j["id"] in seen:
                problems.append(f"{where}.id: duplicate {j['id']!r}")
            seen.add(j["id"])
            t = j.get("template")
            if t not in JOB_TEMPLATES:
                problems.append(f"{where}.template: one of {list(JOB_TEMPLATES)}")
            if not cron_ok(j.get("schedule")):
                problems.append(f"{where}.schedule: 5-field cron expression (minute hour day month weekday)")
            deliver = j.get("deliver", "local")
            if deliver != "local" and deliver not in channels:
                problems.append(f"{where}.deliver: {deliver!r} is not local, ops_chat (bot.ops.owner_chat) or a jobs.channels key")
            p = j.get("params") or {}
            if "enabled" in j and not isinstance(j["enabled"], bool):
                problems.append(f"{where}.enabled: must be true or false")
            if t == "distribute_leads" and dist is None:
                problems.append(f"{where}: template distribute_leads needs a distribution section")
            if t == "tracker_report" and rep is None:
                problems.append(f"{where}: template tracker_report needs a reports section")
            if t == "budget_report" and get(pack, "limits.budgets") is None:
                problems.append(f"{where}: template budget_report needs limits.budgets")
            if t == "followup_check" and not get(pack, "bot.followup"):
                problems.append(f"{where}: template followup_check needs bot.followup")
            if t == "search_chain":
                hs = p.get("hypotheses")
                if not _str_list(hs) or not hs:
                    problems.append(f"{where}.params.hypotheses: required, list of targeting.hypotheses ids")
                else:
                    for h in hs:
                        if h not in hyps or not hyps[h].get("queries"):
                            problems.append(f"{where}.params.hypotheses: {h!r} is not a hypothesis with queries")
            if t == "budget_report" and "warn_percent" in p and not (_num(p["warn_percent"]) and p["warn_percent"] <= 100):
                problems.append(f"{where}.params.warn_percent: number in [0, 100]")
    return problems


def jobs_language(pack: dict) -> str:
    lang = get(pack, "reports.language") or get(pack, "bot.ops.operator_language") or get(pack, "company.language")
    return lang if lang in ("ru", "en") else "en"


def build_jobs_env(pack: dict, env: dict, base: Path) -> tuple[dict, set, list]:
    """KIT_JOBS_CONFIG for kit/jobs (distribution, report, budget, chain)."""
    problems: list[str] = []
    if not any(get(pack, k) is not None for k in ("distribution", "reports", "jobs", "limits.budgets")):
        return {}, set(), ["distribution / reports / jobs / limits.budgets: none present, nothing for --module jobs"]
    dist = dict(get(pack, "distribution") or {})
    if dist:
        dist["source_tab"] = tab_name(get(pack, f"sheets.tabs.{dist.get('source_tab', 'unsorted')}"))
    members = [{"id": m.get("id"), "display_name": m.get("display_name") or m.get("id"),
                "tracker_tab": m.get("tracker_tab"), "lead_share": m.get("lead_share")}
               for m in get(pack, "team.members", []) or [] if isinstance(m, dict)]
    cfg = {
        "timezone": get(pack, "company.timezone") or "UTC",
        "language": jobs_language(pack),
        "read_quota_per_min": int(get(pack, "sheets.read_quota_per_min") or 60),
        "columns": {k: str(v).upper() for k, v in (get(pack, "sheets.columns") or {}).items()},
        "members": members,
        "distribution": dist or None,
        "reports": get(pack, "reports"),
    }
    # KIT_HOME (state, budget ledger) is a host setting from .env: the jobs refuse to start without it
    out = {"KIT_JOBS_CONFIG": json.dumps(cfg, ensure_ascii=False, sort_keys=True), "KIT_REPORT_LANG": cfg["language"]}
    return out, set(), problems


def build_cron(pack: dict, env: dict, paths: dict, fmt: str) -> tuple[str, list]:
    """Hermes cron output for jobs.list[] (see kit/jobs/templates.py). Returns (text, problems)."""
    kit_dir = str(Path(__file__).resolve().parents[1])
    if kit_dir not in sys.path:
        sys.path.insert(0, kit_dir)
    from jobs import templates
    problems: list[str] = []
    jobs = [j for j in get(pack, "jobs.list", []) or []]
    if not jobs:
        return "", ["jobs.list: no jobs in the pack"]
    channels = job_channels(pack)
    origins = {}
    for j in jobs:
        ch = channels.get(j.get("deliver", "local"))
        if ch:
            chat, _ = resolve(ch.get("chat_id"), env, f"jobs.list[{j['id']}].deliver", problems)
            if chat:
                origins[j["id"]] = {"platform": ch.get("platform", "telegram"), "chat_id": chat,
                                    "chat_name": ch.get("chat_name") or j.get("deliver")}
    if problems:
        return "", problems
    tz = get(pack, "company.timezone") or "UTC"
    if fmt == "json":
        spec = {"jobs": [templates.hermes_job(j, paths, origins.get(j["id"])) for j in jobs], "updated_at": None}
        return json.dumps(spec, ensure_ascii=False, indent=2) + "\n", []
    lines = [f"# Hermes cron jobs from the brand pack. Cron expressions are read in the SERVER's timezone:",
             f"# this pack expects {tz} (check with `timedatectl`). Jobs with --deliver origin go to the chat",
             f"# the command is run from; check delivery after the first run."]
    lines += templates.to_commands(jobs, paths, origins)
    return "\n".join(lines) + "\n", []


def discovery_config(pack: dict, hypothesis_id: str, delay: float) -> dict:
    """config.json for bulk-web-discovery-worker built from one pack hypothesis."""
    hyp = next((h for h in get(pack, "targeting.hypotheses", []) or [] if h.get("id") == hypothesis_id), None)
    if hyp is None:
        ids = [h.get("id") for h in get(pack, "targeting.hypotheses", []) or []]
        raise PackError(f"hypothesis {hypothesis_id!r} not found (have: {ids})")
    if not hyp.get("queries"):
        raise PackError(f"hypothesis {hypothesis_id!r} has no queries")
    if not hyp.get("relevance_terms"):
        raise PackError(f"hypothesis {hypothesis_id!r} has no relevance_terms (validation scoring needs them)")
    return {
        # run dir is named after the target, not the date (see the skill's run-dir pitfall)
        "run_id": hypothesis_id,
        "hypothesis": hyp.get("text", ""),
        "delay_seconds": delay,
        "relevance_terms": list(hyp["relevance_terms"]),
        "negative_terms": list(get(pack, "targeting.negative_terms") or DEFAULT_NEGATIVE_TERMS),
        "queries": [{"text": q["text"], "backends": list(q["backends"])} for q in hyp["queries"]],
    }


# --------------------------------------------------------------------------- commands

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Brand pack -> kit env vars")
    ap.add_argument("--pack", required=True, type=Path, help="pack directory or pack.yaml/pack.json")
    ap.add_argument("--env-file", type=Path, help="client .env used to resolve env:NAME refs")
    ap.add_argument("--module", choices=["search", "ops", "outreach", "jobs", "all"], default="search",
                    help="which modules' env to build: search skills (default), the ops bot (+ mail cards), "
                         "email outreach, scheduled jobs (distribution, reports, budget, search chain), or all of them")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate the pack and exit")
    sub.add_parser("ops-context", help="offline operator texts and public pack settings; no credentials needed")
    sub.add_parser("print", help="print KEY=VALUE (values from env: refs masked)")
    sub.add_parser("export", help="print shell export lines for eval")
    dc = sub.add_parser("discovery-config", help="write a discovery_worker config.json from a pack hypothesis")
    dc.add_argument("--hypothesis", required=True)
    dc.add_argument("--out", type=Path, help="output path (default: stdout)")
    dc.add_argument("--delay", type=float, default=2.0)
    run = sub.add_parser("run", help="run a command with the kit env applied")
    run.add_argument("command", nargs=argparse.REMAINDER, help="-- command [args...]")
    cron = sub.add_parser("cron", help="Hermes cron jobs from jobs.list[] (review, then create them in Hermes)")
    cron.add_argument("--format", choices=["commands", "json"], default="commands")
    cron.add_argument("--server-tz", help="the server's timezone; refused if it differs from company.timezone")
    cron.add_argument("--kit-dir", type=Path, help="kit directory on the server (default: this kit)")
    args = ap.parse_args(argv)

    try:
        pack, base = load_pack(args.pack)
    except (PackError, ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if args.cmd == "ops-context":
        try:
            print(json.dumps(ops_context(pack, base), ensure_ascii=False, indent=2))
        except (PackError, OSError, UnicodeError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        return 0
    file_env = load_env_file(args.env_file)
    lookup_env = {**file_env, **os.environ}
    kit_vars, from_ref, problems = build_env(pack, lookup_env)
    problems += (check_files(pack, base) + check_hypotheses(pack) + check_ops_sections(pack) + check_mail_sections(pack)
                 + check_jobs_sections(pack) + check_operations(pack))
    if args.module in ("ops", "outreach"):
        kit_vars = {k: v for k, v in kit_vars.items() if k.startswith("KIT_COL_") or k == "SHEET_ID"}
        from_ref &= set(kit_vars)
    if args.module in ("jobs", "all"):
        # jobs keep the search env too: the search chain runs the search skills with the same budgets
        j_vars, j_ref, j_problems = build_jobs_env(pack, lookup_env, base)
        problems += j_problems
        kit_vars.update(j_vars)
        from_ref |= j_ref
    if args.module in ("ops", "all"):
        ops_vars, ops_ref, ops_problems = build_ops_env(pack, lookup_env, base)
        problems += ops_problems
        kit_vars.update(ops_vars)
        from_ref |= ops_ref
    if get(pack, "mail.outreach") is not None or args.module == "outreach":
        # mail.outreach is validated on every command when present; its vars are exported only for outreach/all
        o_vars, o_ref, o_problems = build_outreach_env(pack, lookup_env, base)
        problems += o_problems
        if args.module in ("outreach", "all"):
            kit_vars.update(o_vars)
            from_ref |= o_ref

    if problems:
        print("Brand pack is invalid:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    if args.cmd == "check":
        print(f"OK: {len(kit_vars)} vars ({', '.join(sorted(kit_vars))})")
        return 0
    if args.cmd == "print":
        for k in sorted(kit_vars):
            print(f"{k}={'<from env>' if k in from_ref else kit_vars[k]}")
        return 0
    if args.cmd == "discovery-config":
        try:
            cfg = discovery_config(pack, args.hypothesis, args.delay)
        except PackError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding="utf-8")
            print(f"wrote {args.out} ({len(cfg['queries'])} queries)")
        else:
            sys.stdout.write(text)
        return 0
    if args.cmd == "export":
        for k in sorted(kit_vars):
            print(f"export {k}={shlex.quote(kit_vars[k])}")
        return 0
    if args.cmd == "cron":
        tz = get(pack, "company.timezone") or "UTC"
        if args.server_tz and args.server_tz != tz:
            print(f"ERROR: server timezone {args.server_tz} != company.timezone {tz}: cron times would be off "
                  "(set the server timezone or the pack's)", file=sys.stderr)
            return 1
        if not args.env_file:
            print("ERROR: cron needs --env-file (the jobs run kit_env.py with it)", file=sys.stderr)
            return 2
        paths = {"kit": (args.kit_dir or Path(__file__).resolve().parents[1]).as_posix(),
                 "pack": args.pack.as_posix(), "env_file": args.env_file.as_posix()}
        text, cron_problems = build_cron(pack, lookup_env, paths, args.format)
        if cron_problems:
            print("Cannot build cron jobs:", file=sys.stderr)
            for p in cron_problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        sys.stdout.write(text)
        return 0

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("ERROR: run needs a command after --", file=sys.stderr)
        return 2
    child_env = {**file_env, **os.environ, **kit_vars}
    return subprocess.call(command, env=child_env)


if __name__ == "__main__":
    sys.exit(main())
