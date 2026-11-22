#!/usr/bin/env python3
"""Daily tracker report: send facts from the logs + tracker state, computed by code, delivered as is.

Numbers come only from a fresh computation for the requested day, never from a model's memory:
- facts: Telegram sends/replies from $KIT_HOME/outreach/send_log.jsonl (kit/bots/ops/sender.py), email outreach
  attempts and bounces from the kit/outreach journal (KIT_OUTREACH_STATE_DIR/journal.jsonl);
- tracker: every member's tracker_tab read in ONE values.batchGet (Sheets quota: reads per minute), columns only by
  the pack's sheets.columns letters.
Rules the output keeps (tested):
- a count of warm leads is followed by all of them, or by "shown N of M";
- "no discrepancies" is written only when overdue next steps and log/tracker mismatches are both zero;
- empty send logs while the tracker shows touches for the day is a warning, never "0 sent";
- the source of every block is named, proxy metrics are labeled as proxies.

Usage (env from kit_env.py --module jobs):
    python3 -m jobs.report [--day YYYY-MM-DD] [--rows-json tabs.json] [--json]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "jobs"

from . import sheets  # noqa: E402
from .config import home, load_config, zone, today  # noqa: E402

EVENT_FIELDS = ("first_touch", "reply_on", "meeting_on", "accepted_on", "won_on", "lost_on")
TRACKER_KEYS = ("company", "site", "telegram", "email", "status", "stage", "lead_type", "hypothesis",
                "next_touch", *EVENT_FIELDS)

T = {
    "ru": {
        "title": "Отчёт за {day}", "facts": "По факту отправок ({src})", "src_logs": "журналы отправок",
        "tg_new": "Новых исходящих в Telegram: {n}", "tg_fu": "Фоллоуапов в Telegram: {n}",
        "tg_in": "Входящих ответов в Telegram: {n}", "mail_new": "Писем рассылки: {n}", "mail_bounce": "Недоставок: {n}",
        "by_hyp": "По гипотезам (Telegram, новые / фоллоуапы):", "no_hyp": "без гипотезы",
        "no_facts": "За день исходящих по журналам не зафиксировано",
        "warn_empty": "⚠️ Журналы отправок пусты, а в трекере есть первые касания за день: {n}. Счёт ниже — по трекеру",
        "warn_nolog": "⚠️ Журнал Telegram ({path}) не найден",
        "warn_nojournal": "ℹ️ Журнал почтовой рассылки не подключён (KIT_OUTREACH_STATE_DIR): письма не учтены",
        "tracker": "По трекеру", "touched_day": "Первых касаний за день: {n}",
        "rows": "Строк: {n} (лиды {lead}, партнёры {partner}, без типа {unknown})",
        "status": "Статусы: в работе {active}, тёплые {won}, закрыто {closed}, без статуса {unlabeled}, другое {other}",
        "conv": "Конверсия (прокси: тёплые / тронутые): {won}/{touched} = {pct}%",
        "backlog": "Без Telegram и почты (остаток на поиск): {n}",
        "warm": "Тёплые: {n}", "shown": "показаны {k} из {n}",
        "control": "Контроль", "overdue": "Просроченные следующие шаги: {n}",
        "mismatch_log": "Отправки из журнала без строки в трекере: {n}",
        "mismatch_tr": "Первые касания в трекере без записи в журналах: {n}",
        "clean": "Расхождений и просрочек нет", "closed_reasons": "Причины закрытия: {items}", "none": "(не указана)",
    },
    "en": {
        "title": "Report for {day}", "facts": "Sent, by fact ({src})", "src_logs": "send logs",
        "tg_new": "New Telegram openers: {n}", "tg_fu": "Telegram follow-ups: {n}",
        "tg_in": "Telegram replies received: {n}", "mail_new": "Outreach emails: {n}", "mail_bounce": "Bounces: {n}",
        "by_hyp": "By hypothesis (Telegram, new / follow-ups):", "no_hyp": "no hypothesis",
        "no_facts": "No outbound activity in the send logs for the day",
        "warn_empty": "⚠️ The send logs are empty but the tracker has first touches for the day: {n}. Counted from the tracker",
        "warn_nolog": "⚠️ Telegram send log ({path}) not found",
        "warn_nojournal": "ℹ️ The outreach journal is not configured (KIT_OUTREACH_STATE_DIR): emails not counted",
        "tracker": "Tracker", "touched_day": "First touches on the day: {n}",
        "rows": "Rows: {n} (leads {lead}, partners {partner}, untyped {unknown})",
        "status": "Statuses: in progress {active}, warm {won}, closed {closed}, empty {unlabeled}, other {other}",
        "conv": "Conversion (proxy: warm / touched): {won}/{touched} = {pct}%",
        "backlog": "No Telegram and no email (search backlog): {n}",
        "warm": "Warm: {n}", "shown": "showing {k} of {n}",
        "control": "Checks", "overdue": "Overdue next steps: {n}",
        "mismatch_log": "Logged sends with no tracker row: {n}",
        "mismatch_tr": "Tracker first touches with no log entry: {n}",
        "clean": "No discrepancies, nothing overdue", "closed_reasons": "Close reasons: {items}", "none": "(not set)",
    },
}


# ---------------------------------------------------------------- parsing helpers

SERIAL_EPOCH = date(1899, 12, 30)   # Google Sheets date serial numbers (UNFORMATTED_VALUE)


def parse_day(value, ref: date) -> date | None:
    """A Sheets serial number (the report reads UNFORMATTED_VALUE, so dates come as numbers whatever the sheet's
    locale), else ISO text, else day-first text (dd.mm[.yyyy], dd/mm/yyyy)."""
    s = str(value or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{5}(\.\d+)?", s):
        return SERIAL_EPOCH + timedelta(days=int(float(s)))
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?$", s)
    if m:
        y = int(m[3]) if m[3] else ref.year
        if y < 100:
            y += 2000
        try:
            d = date(y, int(m[2]), int(m[1]))
        except ValueError:
            return None
        if not m[3] and d > ref + timedelta(days=180):   # "dd.mm" without a year: the nearest such date
            d = d.replace(year=d.year - 1)
        return d
    return None


def handle(value) -> str:
    s = str(value or "").strip().lower()
    s = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/", "", s)
    return s.lstrip("@").split("/")[0].split("?")[0]


def read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def local_day(ts: str, tz) -> date | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date()


# ---------------------------------------------------------------- compute (pure)

def rows_from_values(values: list[list[str]], columns: dict) -> list[dict]:
    idx = {k: sheets.col_index(columns[k]) for k in TRACKER_KEYS if columns.get(k)}
    out = []
    for raw in values:
        rec = {k: str(raw[i]).strip() if i < len(raw) and raw[i] is not None else "" for k, i in idx.items()}
        if rec.get("company") or rec.get("site"):
            out.append(rec)
    return out


def weekly_activity(rows, columns, end, ref):
    """Count dated tracker events, independently of the date of the first touch."""
    start = end - timedelta(days=6)
    groups = {}
    for row in rows:
        dates = {key: parse_day(row.get(key), ref) for key in EVENT_FIELDS if columns.get(key)}
        occurred = [key for key, stamp in dates.items() if stamp and start <= stamp <= end]
        if not occurred:
            continue
        counts = groups.setdefault(row.get("hypothesis") or "", {
            key: 0 if key in dates else None for key in EVENT_FIELDS
        })
        for key in occurred:
            counts[key] += 1
        tracks_outcome = "won_on" in dates and "lost_on" in dates
        counts.setdefault("outcome_unknown", 0 if tracks_outcome else None)
        if tracks_outcome and "first_touch" in occurred:
            if not any(dates[key] and dates[key] <= end for key in ("won_on", "lost_on")):
                counts["outcome_unknown"] += 1
    return {"start": start.isoformat(), "end": end.isoformat(), "by_hypothesis": groups}


def compute(tabs: dict, cfg: dict, day: date, ref_today: date, send_log: list | None, journal: list | None) -> dict:
    """tabs: {member display name: [row dicts]}. send_log/journal: parsed JSONL rows, None = file missing."""
    rep = cfg.get("reports") or {}
    st = rep.get("statuses") or {}
    active, won, closed = (set(map(str.casefold, st.get(k) or [])) for k in ("active", "won", "closed"))
    lt = rep.get("lead_types") or {}
    lead_vals, partner_vals = (set(map(str.casefold, lt.get(k) or [])) for k in ("lead", "partner"))
    tz = zone(cfg)

    members, all_rows = [], []
    for name, rows in tabs.items():
        c = Counter()
        reasons, hyp = Counter(), {}
        warm = []
        for r in rows:
            all_rows.append(r)
            s = r.get("status", "").casefold()
            c["rows"] += 1
            t = r.get("lead_type", "").casefold()
            c["lead" if t in lead_vals else "partner" if t in partner_vals else "unknown"] += 1
            if not s:
                c["unlabeled"] += 1
            elif s in closed:
                c["closed"] += 1
                reasons[r.get("stage") or ""] += 1
            elif s in active or s in won:
                c["active"] += 1
            else:
                c["other"] += 1
            if s in won:
                c["won"] += 1
                warm.append(r.get("company") or r.get("site"))
            ft = parse_day(r.get("first_touch"), ref_today)
            touched = bool(r.get("first_touch"))
            if touched:
                c["touched"] += 1
            if ft == day:
                c["touched_day"] += 1
            nt = parse_day(r.get("next_touch"), ref_today)
            if nt and nt < ref_today and s not in closed:
                c["overdue"] += 1
            if not r.get("telegram") and not r.get("email"):
                c["backlog"] += 1
            h = hyp.setdefault(r.get("hypothesis") or "", Counter())
            h["touched"] += touched
            h["won"] += s in won
        members.append({"name": name, "counts": dict(c), "closed_reasons": dict(reasons), "warm": warm,
                        "by_hypothesis": {k: dict(v) for k, v in hyp.items()}})

    facts = {"tg_new": 0, "tg_fu": 0, "tg_in": 0, "mail_new": 0, "mail_bounce": 0, "by_hypothesis": {}}
    openers_day, mail_day = set(), set()
    for e in send_log or []:
        if local_day(e.get("ts", ""), tz) != day:
            continue
        kind = str(e.get("kind", ""))
        if e.get("direction") == "in":
            facts["tg_in"] += 1
            continue
        hy = facts["by_hypothesis"].setdefault(e.get("hypothesis") or "", {"new": 0, "fu": 0})
        if kind == "opener":
            facts["tg_new"] += 1
            hy["new"] += 1
            openers_day.add(handle(e.get("peer")))
        elif kind.startswith("followup"):
            facts["tg_fu"] += 1
            hy["fu"] += 1
    for e in journal or []:
        ev = e.get("event")
        if ev == "attempt" and (e.get("day") == day.isoformat() or local_day(e.get("ts", ""), tz) == day):
            facts["mail_new"] += 1
            mail_day.add(str(e.get("email", "")).lower())
        elif ev == "bounce" and local_day(e.get("ts", ""), tz) == day:
            facts["mail_bounce"] += 1

    has_facts = any(facts[k] for k in ("tg_new", "tg_fu", "tg_in", "mail_new", "mail_bounce"))
    touched_day = sum(m["counts"].get("touched_day", 0) for m in members)
    known_handles = {handle(r.get("telegram")) for r in all_rows if r.get("telegram")}
    log_not_in_tracker = sorted(h for h in openers_day if h and h not in known_handles)
    # every first touch on the day must have a log entry; with empty logs all of them are unmatched.
    # Without the outreach journal (journal is None) e-mail-only touches cannot be checked and are left out.
    tracker_not_in_log = []
    for r in all_rows:
        if parse_day(r.get("first_touch"), ref_today) != day:
            continue
        if handle(r.get("telegram")) in openers_day or str(r.get("email", "")).lower() in mail_day:
            continue
        if journal is None and not r.get("telegram"):
            continue
        tracker_not_in_log.append(r.get("company") or r.get("site"))
    return {
        "day": day.isoformat(), "members": members, "facts": facts, "has_facts": has_facts,
        "weekly_activity": weekly_activity(all_rows, cfg["columns"], day, ref_today),
        "send_log_missing": send_log is None, "journal_missing": journal is None, "touched_day": touched_day,
        "empty_logs_with_activity": (not has_facts) and touched_day > 0,
        "overdue": sum(m["counts"].get("overdue", 0) for m in members),
        "log_not_in_tracker": log_not_in_tracker, "tracker_not_in_log": tracker_not_in_log,
    }


# ---------------------------------------------------------------- render

def render(data: dict, cfg: dict, lang: str = "ru") -> str:
    t = T.get(lang, T["en"])
    limit = int((cfg.get("reports") or {}).get("list_limit", 10))
    f = data["facts"]
    day = date.fromisoformat(data["day"])
    out = [t["title"].format(day=day.strftime("%d.%m")), ""]
    out.append(t["facts"].format(src=t["src_logs"]))
    if data["send_log_missing"]:
        out.append(t["warn_nolog"].format(path="outreach/send_log.jsonl"))
    if data.get("journal_missing"):
        out.append(t["warn_nojournal"])
    if data["has_facts"]:
        out += ["- " + t["tg_new"].format(n=f["tg_new"]), "- " + t["tg_fu"].format(n=f["tg_fu"]),
                "- " + t["tg_in"].format(n=f["tg_in"]), "- " + t["mail_new"].format(n=f["mail_new"])]
        if f["mail_bounce"]:
            out.append("- " + t["mail_bounce"].format(n=f["mail_bounce"]))
        hyps = {h: v for h, v in f["by_hypothesis"].items() if v["new"] or v["fu"]}
        if hyps:
            out.append(t["by_hyp"])
            for h, v in sorted(hyps.items()):
                out.append(f"  - {h or t['no_hyp']}: {v['new']} / {v['fu']}")
    elif data["empty_logs_with_activity"]:
        out.append(t["warn_empty"].format(n=data["touched_day"]))
    else:
        out.append("- " + t["no_facts"])

    for m in data["members"]:
        c = m["counts"]
        g = lambda k: c.get(k, 0)  # noqa: E731
        out += ["", f"{t['tracker']}: {m['name']}",
                "- " + t["touched_day"].format(n=g("touched_day")),
                "- " + t["rows"].format(n=g("rows"), lead=g("lead"), partner=g("partner"), unknown=g("unknown")),
                "- " + t["status"].format(active=g("active"), won=g("won"), closed=g("closed"),
                                          unlabeled=g("unlabeled"), other=g("other"))]
        if g("touched"):
            pct = round(100.0 * g("won") / g("touched"), 1)
            out.append("- " + t["conv"].format(won=g("won"), touched=g("touched"), pct=f"{pct:g}"))
        if m["closed_reasons"]:
            items = ", ".join(f"{k or t['none']} {v}" for k, v in sorted(m["closed_reasons"].items(), key=lambda x: -x[1]))
            out.append("- " + t["closed_reasons"].format(items=items))
        out.append("- " + t["backlog"].format(n=g("backlog")))
        warm = m["warm"]
        if warm:
            shown = warm[:limit]
            tail = "" if len(shown) == len(warm) else f" ({t['shown'].format(k=len(shown), n=len(warm))})"
            out.append("- " + t["warm"].format(n=len(warm)) + tail + ": " + ", ".join(shown))

    out += ["", t["control"]]
    problems = [(data["overdue"], t["overdue"]), (len(data["log_not_in_tracker"]), t["mismatch_log"]),
                (len(data["tracker_not_in_log"]), t["mismatch_tr"])]
    if all(n == 0 for n, _ in problems):
        out.append("- " + t["clean"])
    else:
        for n, text in problems:
            if n:
                out.append("- " + text.format(n=n))
    if week := data.get("weekly_activity"):
        ru = lang == "ru"
        heading = "За 7 дней по датам в трекере" if ru else "7 days of dated tracker events"
        out += ["", f"{heading} ({week['start']} — {week['end']})"]
        labels = (("первые касания", "ответы", "встречи", "принято клиентом", "выиграно", "проиграно",
                   "исход новых неизвестен") if ru else
                  ("first touches", "replies", "meetings", "accepted by client", "won", "lost",
                   "new with unknown outcome"))
        for hyp, counts in sorted(week["by_hypothesis"].items()):
            metrics = [f"{label} {counts[key] if counts[key] is not None else '—'}"
                       for label, key in zip(labels, (*EVENT_FIELDS, "outcome_unknown"))]
            out.append(f"- {hyp or t['no_hyp']}: " + ", ".join(metrics))
        if not week["by_hypothesis"]:
            out.append("За период нет записанных дат событий." if ru else "No tracker events dated in this period.")
    return "\n".join(out)


# ---------------------------------------------------------------- I/O

def tracker_ranges(cfg: dict) -> list[tuple[str, str]]:
    cols = cfg["columns"]
    used = [cols[k] for k in TRACKER_KEYS if cols.get(k)]
    last = sheets.col_letter(max(sheets.col_index(c) for c in used))
    members = [m for m in cfg["members"] if m.get("tracker_tab")]
    wanted = (cfg.get("reports") or {}).get("members")
    if wanted:
        members = [m for m in members if m["id"] in wanted]
    return [(m.get("display_name") or m["id"], f"{sheets.quote_tab(m['tracker_tab'])}!A2:{last}") for m in members]


def archive_report(directory: Path, data: dict) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / (data["day"] + ".json")
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temp:
        tmp = Path(temp.name)
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def read_tracker_csv(path: Path, cfg: dict):
    members = {m["id"]: m.get("display_name") or m["id"] for m in cfg["members"]
               if not (cfg.get("reports") or {}).get("members") or m["id"] in cfg["reports"]["members"]}
    tabs = {label: [] for label in members.values()}
    with path.open(encoding="utf-8-sig", newline="") as source:
        header = source.readline()
        source.seek(0)
        delimiter = max((",", ";", "\t"), key=header.count)
        reader = csv.DictReader(source, delimiter=delimiter)
        fields = set(reader.fieldnames or ())
        if len(fields) != len(reader.fieldnames or ()):
            raise ValueError("duplicate CSV column name")
        if "member" not in fields or not fields.intersection(("company", "site")) or "first_touch" not in fields:
            raise ValueError("CSV needs member, first_touch and company or site columns")
        for row in reader:
            if None in row or None in row.values():
                raise ValueError(f"CSV row {reader.line_num}: incorrect column count")
            member = row["member"].strip()
            if member not in members:
                if any(m["id"] == member for m in cfg["members"]):
                    continue
                raise ValueError(f"CSV row {reader.line_num}: unknown member")
            if row.get("company", "").strip() or row.get("site", "").strip():
                tabs[members[member]].append({k: row[k].strip() for k in fields.intersection(TRACKER_KEYS)})
    return tabs, {key: True for key in fields.intersection(TRACKER_KEYS)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Daily tracker report")
    ap.add_argument("--day", help="YYYY-MM-DD, default: yesterday in the pack timezone")
    ap.add_argument("--rows-json", default=os.environ.get("KIT_REPORT_ROWS_JSON"),
                    help="offline JSON keyed by each member's display_name (or id if omitted); use [] for an empty tracker")
    ap.add_argument("--rows-csv", type=Path, default=os.environ.get("KIT_REPORT_ROWS_CSV") or None,
                    help="read a UTF-8 tracker export with named columns")
    ap.add_argument("--archive", type=Path, default=os.environ.get("KIT_REPORT_ARCHIVE_DIR") or None,
                    help="also save the snapshot as YYYY-MM-DD.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config()
    ref = today(cfg)
    day = date.fromisoformat(args.day) if args.day else ref - timedelta(days=1)
    if args.rows_csv and args.rows_json:
        ap.error("choose one source: CSV or JSON")
    ranges = tracker_ranges(cfg) if not args.rows_csv else []
    if args.rows_csv:
        tabs, columns = read_tracker_csv(args.rows_csv, cfg)
        cfg = {**cfg, "columns": columns}
    elif args.rows_json:
        raw = json.loads(Path(args.rows_json).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            ap.error("rows JSON must be an object keyed by member display name")
        missing = [label for label, _ in ranges if label not in raw]
        if missing:
            ap.error("rows JSON is missing member display name(s): " + ", ".join(missing)
                     + ". Use an empty array for a member with no rows.")
        values = [raw[label] for label, _ in ranges]
        if any(not isinstance(rows, list) or any(not isinstance(row, list) for row in rows) for rows in values):
            ap.error("each member must have an array of rows, each row an array of cells")
    else:
        sh = sheets.Sheets(os.environ["SHEET_ID"], read_quota_per_min=cfg.get("read_quota_per_min", 60),
                           ledger=sheets.ledger_for_reads())
        values = sh.batch_get([r for _, r in ranges], render="UNFORMATTED_VALUE")
    if not args.rows_csv:
        tabs = {label: rows_from_values(v, cfg["columns"]) for (label, _), v in zip(ranges, values)}
    log_path = home() / "outreach" / "send_log.jsonl"
    send_log = read_jsonl(log_path) if log_path.exists() else None
    jdir = os.environ.get("KIT_OUTREACH_STATE_DIR", "").strip()
    journal_path = Path(jdir) / "journal.jsonl" if jdir else None
    journal = read_jsonl(journal_path) if journal_path and journal_path.is_file() else None
    data = compute(tabs, cfg, day, ref, send_log, journal)
    if args.archive:
        data["collected_at"] = datetime.now(timezone.utc).isoformat()
        data["tracker_source"] = "csv" if args.rows_csv else ("rows-json" if args.rows_json else "sheets")
        archive_report(args.archive, data)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print(render(data, cfg, cfg.get("language", "ru")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
