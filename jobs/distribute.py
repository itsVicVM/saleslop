#!/usr/bin/env python3
"""Hand out new leads from the unsorted tab to team members by the pack's distribution rules.

`plan()` is a pure function: rows + rules + state in, assignments + held rows + new state out.
- Rules are tried in pack order; the first rule whose `when` matches wins. `when` keys: geo, hypothesis, lead_type
  (a string or a list, case-insensitive; geo also matches distribution.geo_aliases). An empty `when` matches all.
- Inside a rule's `to`, leads are split by team.members[].lead_share with smooth weighted round-robin. The cursor
  lives in the state, so the shares hold across runs, not only inside one run.
- No rule matched: distribution.fallback `share` (all members with lead_share > 0) or `hold` (row stays unassigned).
- A row whose site (domain) already has an owner elsewhere in the tab goes to that same owner.
- distribution.daily_cap_per_member: a member at the cap gets nothing more today; if every member of the pool is at
  the cap the row is held.

`apply` writes only the owner cell of each assigned row (single-cell ranges, one batchUpdate), after re-reading the
owner column so a row someone assigned by hand in the meantime is never overwritten, then reads it back.
Rows are not moved between tabs: members work their share by filtering the owner column.

Usage (env from kit_env.py --module jobs):
    python3 -m jobs.distribute plan [--rows-json rows.json]   # dry run, nothing is written
    python3 -m jobs.distribute apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "jobs"

from . import budget, sheets  # noqa: E402
from .config import load_config, state_dir, today  # noqa: E402

MATCH_KEYS = ("geo", "hypothesis", "lead_type")


@dataclass
class Lead:
    row: int
    site: str = ""
    company: str = ""
    geo: str = ""
    hypothesis: str = ""
    lead_type: str = ""
    owner: str = ""


@dataclass
class Plan:
    assign: list = field(default_factory=list)   # [(row, member_id, why)]
    held: list = field(default_factory=list)     # [(row, reason)]
    skipped_owned: int = 0
    state: dict = field(default_factory=dict)

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for _, m, _ in self.assign:
            out[m] = out.get(m, 0) + 1
        return out


def norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def site_key(lead: Lead) -> str:
    s = norm(lead.site)
    s = re.sub(r"^[a-z]+://", "", s)
    s = s.split("/")[0].split("?")[0]
    if s.startswith("www."):
        s = s[4:]
    return s or ("company:" + norm(lead.company) if norm(lead.company) else "")


def _values(v) -> list[str]:
    if v is None:
        return []
    return [norm(x) for x in (v if isinstance(v, list) else [v])]


def rule_matches(rule: dict, lead: Lead, geo_aliases: dict) -> bool:
    for key, wanted in (rule.get("when") or {}).items():
        want = _values(wanted)
        have = norm(getattr(lead, key, ""))
        if key == "geo":
            ok = any(have == w or have in _values(_alias_lookup(geo_aliases, w)) for w in want)
        else:
            ok = have in want
        if not ok:
            return False
    return True


def _alias_lookup(aliases: dict, region_norm: str) -> list:
    for k, v in (aliases or {}).items():
        if norm(k) == region_norm:
            return v if isinstance(v, list) else [v]
    return []


def _pick(pool: list[str], weights: dict, cursor: dict, eligible: list[str]) -> str:
    """Smooth weighted round-robin step over `eligible` (a subset of `pool`, pool order breaks ties)."""
    total = sum(weights[m] for m in eligible)
    for m in eligible:
        cursor[m] = cursor.get(m, 0.0) + weights[m]
    best = max(eligible, key=lambda m: (cursor[m], -pool.index(m)))
    cursor[best] -= total
    return best


def plan(leads: list[Lead], cfg: dict, state: dict | None, day: str) -> Plan:
    dist = cfg["distribution"]
    members = {m["id"]: m for m in cfg["members"]}
    shares = {mid: float(m.get("lead_share") or 0) for mid, m in members.items()}
    owner_field = dist.get("owner_value", "display_name")
    owner_of = {norm(m.get(owner_field) or mid): mid for mid, m in members.items()}
    owner_of.update({norm(mid): mid for mid in members})
    aliases = dist.get("geo_aliases") or {}
    cap = dist.get("daily_cap_per_member")

    st = json.loads(json.dumps(state or {}))
    pools = st.setdefault("pools", {})
    if st.get("day") != day:
        st["day"], st["today"] = day, {}
    today_n = st.setdefault("today", {})
    out = Plan(state=st)

    owned_sites: dict[str, tuple[int, str]] = {}
    for lead in leads:
        if norm(lead.owner) and site_key(lead):
            owned_sites.setdefault(site_key(lead), (lead.row, lead.owner))

    seen_new: dict[str, int] = {}
    for lead in leads:
        if norm(lead.owner):
            out.skipped_owned += 1
            continue
        if not site_key(lead):
            continue   # empty row
        key = site_key(lead)
        if key in owned_sites:
            row0, owner = owned_sites[key]
            mid = owner_of.get(norm(owner))
            if mid:
                out.assign.append((lead.row, mid, f"same site as row {row0}"))
                today_n[mid] = today_n.get(mid, 0) + 1
            else:
                out.held.append((lead.row, f"same site as row {row0}, owner {owner!r} is not a team member"))
            continue
        if key in seen_new:
            first = seen_new[key]
            mid = next((m for r, m, _ in out.assign if r == first), None)
            if mid:
                out.assign.append((lead.row, mid, f"same site as row {first}"))
                today_n[mid] = today_n.get(mid, 0) + 1
            else:
                out.held.append((lead.row, f"same site as held row {first}"))
            continue
        seen_new[key] = lead.row

        rule_i = next((i for i, r in enumerate(dist.get("rules") or []) if rule_matches(r, lead, aliases)), None)
        if rule_i is not None:
            pool = list(dist["rules"][rule_i]["to"])
            why = f"rule {rule_i + 1}"
        elif dist.get("fallback") == "share":
            pool = [m for m in members if shares[m] > 0]
            why = "fallback share"
        else:
            out.held.append((lead.row, "no rule matched (fallback: hold)"))
            continue
        eligible = [m for m in pool if not (cap is not None and today_n.get(m, 0) >= cap)]
        if not eligible:
            out.held.append((lead.row, f"daily cap reached for {', '.join(pool)}"))
            continue
        cursor = pools.setdefault(",".join(pool), {})
        mid = _pick(pool, shares, cursor, eligible)
        today_n[mid] = today_n.get(mid, 0) + 1
        out.assign.append((lead.row, mid, why))
    return out


# ---------------------------------------------------------------- sheet I/O

def leads_from_values(values: list[list[str]], columns: dict, first_row: int = 2) -> list[Lead]:
    idx = {k: sheets.col_index(columns[k]) for k in ("site", "company", "geo", "hypothesis", "lead_type", "owner")
           if columns.get(k)}
    out = []
    for i, raw in enumerate(values):
        cell = lambda k: (raw[idx[k]] if k in idx and idx[k] < len(raw) else "") or ""  # noqa: E731
        out.append(Lead(row=first_row + i, **{k: str(cell(k)).strip() for k in idx}))
    return out


def _source_range(cfg: dict) -> tuple[str, str]:
    cols = cfg["columns"]
    used = [cols[k] for k in ("site", "company", "geo", "hypothesis", "lead_type", "owner") if cols.get(k)]
    last = sheets.col_letter(max(sheets.col_index(c) for c in used))
    tab = sheets.quote_tab(cfg["distribution"]["source_tab"])
    return f"{tab}!A2:{last}", f"{tab}!{cols['owner']}2:{cols['owner']}"


def owner_value(cfg: dict, member_id: str) -> str:
    m = next(x for x in cfg["members"] if x["id"] == member_id)
    field_name = cfg["distribution"].get("owner_value", "display_name")
    return str(m.get(field_name) or member_id)


def apply_plan(p: Plan, cfg: dict, sh: "sheets.Sheets") -> dict:
    """Write owner cells for p.assign. Returns {"written", "conflicts", "mismatches"}."""
    cols = cfg["columns"]
    tab = sheets.quote_tab(cfg["distribution"]["source_tab"])
    _, owner_rng = _source_range(cfg)
    fresh = sh.batch_get([owner_rng])[0]
    current = {2 + i: (r[0] if r else "").strip() for i, r in enumerate(fresh)}
    cells, conflicts = [], []
    for row, mid, _ in p.assign:
        if current.get(row):
            conflicts.append((row, current[row]))
            continue
        cells.append((f"{tab}!{cols['owner']}{row}", owner_value(cfg, mid)))
    written = sh.write_cells(cells)
    back = sh.batch_get([owner_rng])[0] if cells else []
    after = {2 + i: (r[0] if r else "").strip() for i, r in enumerate(back)}
    mismatches = [(int(re.search(r"(\d+)$", rng).group(1)), v) for rng, v in cells
                  if after.get(int(re.search(r"(\d+)$", rng).group(1))) != v]
    return {"written": written, "cells": cells, "conflicts": conflicts, "mismatches": mismatches}


def render(p: Plan, cfg: dict, applied: dict | None, lang: str) -> str:
    counts = p.counts()
    if not p.assign and not p.held:
        return "[SILENT]"
    names = {m["id"]: m.get("display_name") or m["id"] for m in cfg["members"]}
    ru = lang == "ru"
    head = ("Раздача лидов" if ru else "Lead distribution") + ("" if applied else (" (план, ничего не записано)" if ru
                                                                                    else " (plan, nothing written)"))
    lines = [head, ""]
    for mid in [m["id"] for m in cfg["members"]]:
        if counts.get(mid):
            lines.append(f"- {names[mid]}: {counts[mid]}")
    if p.held:
        lines.append(("Оставлено без владельца: " if ru else "Left unassigned: ") + str(len(p.held)))
        reasons: dict[str, int] = {}
        for _, why in p.held:
            reasons[why] = reasons.get(why, 0) + 1
        for why, n in sorted(reasons.items(), key=lambda x: -x[1]):
            lines.append(f"  - {why}: {n}")
    if applied:
        if applied["conflicts"]:
            lines.append(("⚠️ Уже назначены вручную, не тронуты: " if ru else "⚠️ Assigned by hand meanwhile, left as is: ")
                         + ", ".join(str(r) for r, _ in applied["conflicts"]))
        if applied["mismatches"]:
            lines.append(("⚠️ Проверка после записи не совпала в строках: " if ru else "⚠️ Read-back mismatch in rows: ")
                         + ", ".join(str(r) for r, _ in applied["mismatches"]))
    return "\n".join(lines)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Distribute unsorted leads by the pack's rules")
    ap.add_argument("cmd", choices=["plan", "apply"])
    ap.add_argument("--rows-json", help="offline: JSON list of rows (lists of cell values from row 2) instead of Sheets")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    cfg = load_config()
    if not cfg.get("distribution"):
        print("ERROR: the pack has no distribution section", file=sys.stderr)
        return 2
    if args.cmd == "apply" and args.rows_json:
        print("ERROR: apply works on the live sheet only", file=sys.stderr)
        return 2
    lang = cfg.get("language", "ru")
    day = today(cfg).isoformat()
    sdir = state_dir()
    state_path = sdir / "distribution.json"

    sh = None
    if args.rows_json:
        values = json.loads(Path(args.rows_json).read_text(encoding="utf-8"))
    else:
        sh = sheets.Sheets(os.environ["SHEET_ID"], read_quota_per_min=cfg.get("read_quota_per_min", 60),
                           ledger=sheets.ledger_for_reads())
        values = sh.batch_get([_source_range(cfg)[0]])[0]
    leads = leads_from_values(values, cfg["columns"])

    lock = budget._FileLock(sdir / "distribution.lock", timeout=5)
    with lock:
        p = plan(leads, cfg, _load_state(state_path), day)
        applied = None
        if args.cmd == "apply":
            applied = apply_plan(p, cfg, sh)
            done = {r for r, _, _ in p.assign} - {r for r, _ in applied["conflicts"]}
            if done:
                sdir.mkdir(parents=True, exist_ok=True)
                tmp = state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(p.state, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, state_path)
    if args.json:
        print(json.dumps({"assign": p.assign, "held": p.held, "skipped_owned": p.skipped_owned,
                          "applied": applied and {k: applied[k] for k in ("written", "conflicts", "mismatches")}},
                         ensure_ascii=False, indent=1))
    else:
        print(render(p, cfg, applied, lang))
    return 1 if applied and applied["mismatches"] else 0


if __name__ == "__main__":
    sys.exit(main())
