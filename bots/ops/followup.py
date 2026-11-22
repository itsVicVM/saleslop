#!/usr/bin/env python3
"""Automatic follow-ups (steps 1-3) to contacts who did not answer, driven by the client's tracker sheet.

  who      tracker rows with status == followup.status_value, stage in followup.step_values, next-touch date <= today
  check    live chat on the sending account: skip if the contact wrote after our last message or our message is missing
  text     pack templates (followup.templates[lang]), language of our last message, first name only if it looks real
  pace     random gap between contacts, daily caps per account and per operator, stop an account on PeerFlood/FloodWait
  after    stage -> next step value (or final_value after the last step), written back with read-back verification

Columns come from the pack (sheets.columns: telegram, stage, status, next_touch, name, company, account, lead_type).
Offline/dry use: --rows-json FILE ({"header": [...], "rows": [[...], ...]} or a list of rows) and --dry-run.

  python3 -m bots.ops.followup [--dry-run] [--rows-json F] [--ignore-window] [--limit N] [--report stdout|operator]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

NOT_A_NAME = re.compile(r"support|manager|admin|team|sales|info|bot|official|help|service|desk|ltd|inc|llc|group|\d|@|https?:|\.", re.I)


def col_index(letter: str) -> int:
    n = 0
    for ch in letter.strip().upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def first_name(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw or NOT_A_NAME.search(raw) or len(raw) > 40:
        return ""
    w = raw.split()[0].strip(",.;:!")
    return w if w[:1].isalpha() and w[:1].isupper() and 2 <= len(w) <= 20 else ""


def norm_handle(h: str) -> str:
    h = (h or "").strip().lower()
    h = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", h).lstrip("@").split("?")[0].strip("/")
    return h if re.fullmatch(r"[a-z0-9_]{4,32}", h) else ""


def parse_day(s: str, today: dt.date):
    """Accepts DD.MM, DD.MM.YY, DD.MM.YYYY and ISO YYYY-MM-DD."""
    s = (s or "").strip()
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        pass
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$", s)
    if not m:
        return None
    y = int(m.group(3)) if m.group(3) else today.year
    try:
        return dt.date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def detect_lang(text: str) -> str:
    t = (text or "").lower()
    return "ru" if t and len(re.findall(r"[а-яё]", t)) > len(t) * 0.2 else "en"


def load_templates(path) -> list:
    """File format: one block per step separated by a line `---`; each block has `named:` and `anonymous:` lines.
    `{name}` in a named line is replaced by the contact's first name."""
    blocks = []
    for block in Path(path).read_text(encoding="utf-8").split("\n---"):
        variants = {}
        for line in block.strip().splitlines():
            key, _, val = line.partition(":")
            if key.strip() in ("named", "anonymous") and val.strip():
                variants[key.strip()] = val.strip()
        if variants:
            blocks.append(variants)
    return blocks


def render(templates: dict, lang: str, step: int, name: str, fallback_lang: str = "en") -> str:
    blocks = templates.get(lang) or templates.get(fallback_lang) or []
    if step < 1 or step > len(blocks):
        raise ValueError(f"no follow-up template for language {lang!r} step {step}")
    b = blocks[step - 1]
    if name and b.get("named"):
        return b["named"].replace("{name}", name)
    return b.get("anonymous") or b["named"].replace("{name}, ", "").replace("{name}", "")


def plan(rows, fcfg: dict, columns: dict, log_rows, accounts, enabled, today: dt.date, outreach_off=(), stopped=()):
    """Pure selection step: which tracker rows get which follow-up from which account. Returns (candidates, skipped).
    `outreach_off`: accounts with outreach switched off (pack or ops bot); a follow-up is outreach, so they are skipped
    even with --ignore-toggle."""
    col = {k: col_index(v) for k, v in columns.items() if v}

    def cell(r, key):
        i = col.get(key)
        return (r[i] if i is not None and i < len(r) else "").strip()

    last_out_acct, lang_of, opener, delivered = {}, {}, {}, {}
    for e in log_rows:
        if e.get("channel", "telegram") != "telegram" or e.get("direction") != "out":
            continue
        p = norm_handle(str(e.get("peer", "")))
        if p:
            kind = e.get("kind", "")
            if kind == "opener":
                opener[p] = str(e.get("message_id") or e.get("ts") or "")
                delivered[p] = set()
            elif kind.startswith("followup"):
                delivered.setdefault(p, set()).add(kind)
            last_out_acct[p] = e.get("account")
            if (e.get("text") or "").strip():
                lang_of[p] = detect_lang(e["text"])
    acct_lc = {a.lower(): a for a in accounts}
    steps = [str(s) for s in fcfg.get("step_values", ["1", "2", "3"])]
    partner_prefixes = [p.lower() for p in fcfg.get("partner_prefixes", [])]
    cands, skipped, seen = [], Counter(), set()
    for i, r in enumerate(rows, start=2):  # row 1 is the header
        stage = cell(r, "stage")
        if stage not in steps or cell(r, "status") != fcfg.get("status_value"):
            continue
        due = parse_day(cell(r, "next_touch"), today)
        if not due or due > today:
            continue
        handles = [norm_handle(x) for x in re.split(r"[\s,;]+", cell(r, "telegram")) if norm_handle(x)]
        if not handles:
            skipped["no telegram handle"] += 1
            continue
        h = handles[0]
        if ("telegram", h) in stopped:
            skipped["company opt-out"] += 1
            continue
        if h in seen:
            skipped["duplicate contact in tracker"] += 1
            continue
        seen.add(h)
        acct = next((acct_lc[t.lower()] for t in re.findall(r"@?([A-Za-z0-9_]{4,40})", cell(r, "account")) if t.lower() in acct_lc),
                    None) or last_out_acct.get(h) or ""
        if not acct:
            skipped["unknown sending account"] += 1
            continue
        if acct not in enabled:
            skipped[f"follow-ups off for {acct}"] += 1
            continue
        if acct in outreach_off:
            skipped[f"outreach off for {acct}"] += 1
            continue
        lead_type = cell(r, "lead_type").lower()
        step = steps.index(stage) + 1
        cands.append({"row": i, "handle": h, "account": acct, "step": step, "stage": stage,
                      "opener": opener.get(h, ""), "already_sent": f"followup{step}" in delivered.get(h, ()),
                      "name": first_name(cell(r, "name")), "lang": lang_of.get(h, fcfg.get("default_language", "en")),
                      "partner": any(lead_type.startswith(p) for p in partner_prefixes) if partner_prefixes else False})
    return cands, skipped


def next_stage(fcfg: dict, step: int) -> str:
    steps = [str(s) for s in fcfg.get("step_values", ["1", "2", "3"])]
    return steps[step] if step < len(steps) else fcfg.get("final_value", "")


def in_window(fcfg: dict, now: dt.datetime) -> bool:
    w = fcfg.get("window") or {}
    days = w.get("days", [1, 2, 3, 4, 5])
    lo, hi = w.get("from", "09:00"), w.get("to", "19:00")
    return now.isoweekday() in days and lo <= now.strftime("%H:%M") < hi


# ------------------------------------------------------------------------------ runner

def _tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return dt.timezone.utc


def _load_rows_json(path):
    """{"header": [...], "rows": [...]} (rows without the header) or a plain list whose first row is the header."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return list(data.get("rows", [])) if isinstance(data, dict) else list(data)[1:]


def _sheets_service():
    sys.path.insert(0, os.environ.get("KIT_GOOGLE_API_DIR", ""))
    import google_api  # provided by the agent runtime's google-workspace skill
    return google_api.build_service("sheets", "v4")


async def run(args) -> int:
    from outreach import company_stop
    from .config import OpsConfig
    from .opsbot import OpsBot
    from .store import Store
    cfg_json = json.loads(os.environ.get("KIT_OPS_CONFIG") or "{}")
    fcfg = cfg_json.get("followup") or {}
    tracker = cfg_json.get("tracker") or {}
    if not fcfg.get("templates") or not tracker.get("columns"):
        print("ERROR: bot.followup and sheets.columns must be set in the pack (KIT_OPS_CONFIG)", file=sys.stderr)
        return 2
    ops_cfg = OpsConfig.from_env()
    tz = _tz(ops_cfg.timezone)
    now = dt.datetime.now(tz)
    if not args.ignore_window and not in_window(fcfg, now):
        print("[followup] outside the sending window")
        return 0
    templates = {lang: load_templates(p) for lang, p in fcfg["templates"].items()}
    partner_templates = {lang: load_templates(p) for lang, p in (fcfg.get("partner_templates") or {}).items()}
    store = Store(ops_cfg.state_dir, ops_cfg.send_log_path)
    enabled = set(store.settings().get("followup_accounts") or fcfg.get("accounts") or [])
    if args.ignore_toggle:
        enabled = set(ops_cfg.accounts)

    svc = None
    if args.rows_json:
        rows = _load_rows_json(args.rows_json)
    else:
        svc = _sheets_service()
        values = svc.spreadsheets().values().get(spreadsheetId=os.environ["SHEET_ID"],
                                                 range=f"'{tracker['tab']}'!A1:AZ20000").execute().get("values", [])
        rows = values[1:]
    outreach_off = set(ops_cfg.outreach_disabled) | store.outreach_off("telegram")
    cands, skipped = plan(rows, fcfg, tracker["columns"], list(store.log_rows()), ops_cfg.accounts, enabled, now.date(),
                          outreach_off=outreach_off, stopped=company_stop.snapshot(ops_cfg.home))
    if args.limit:
        cands = cands[:args.limit]

    per_account, per_operator = int(fcfg.get("per_account_day", 20)), int(fcfg.get("per_operator_day", 45))
    sent_today = Counter()
    for e in store.log_rows():
        try:
            day = dt.datetime.fromisoformat(str(e.get("ts")).replace("Z", "+00:00")).astimezone(tz).date()
        except ValueError:
            continue
        if e.get("direction") == "out" and day == now.date():  # caps count in the company's time zone
            sent_today[e.get("account")] += 1
    op_total = sum(sent_today.values())
    dry = args.dry_run
    clients, blocked, results = {}, {}, []
    for c in cands:
        acct = c["account"]
        tpl = partner_templates if c["partner"] and partner_templates else templates
        c["text"] = render(tpl, c["lang"], c["step"], c["name"], fcfg.get("default_language", "en"))
        if c["already_sent"]:
            if dry or svc is None:
                c["result"] = "already sent: check tracker stage"
            else:
                c["result"] = _update_stage(c, svc, tracker, fcfg)
        elif acct in blocked:
            c["result"] = f"skip: {acct} stopped ({blocked[acct]})"
        elif sent_today[acct] >= per_account or op_total >= per_operator:
            c["result"] = "skip: daily cap"
        elif dry:
            c["result"] = "would send"
            sent_today[acct] += 1
            op_total += 1
        else:
            c["result"] = await _send_one(c, acct, clients, blocked, store, svc, tracker, fcfg)
            if c["result"].startswith("sent"):
                sent_today[acct] += 1
                op_total += 1
                lo, hi = fcfg.get("gap_s", [120, 240])
                await asyncio.sleep(random.uniform(lo, hi))
        results.append(c)
    for cl in clients.values():
        await cl.disconnect()

    head = f"{'🧪 Follow-ups (dry run)' if dry else '🔁 Follow-ups'} {now:%Y-%m-%d}: {len(cands)} candidates"
    lines = [head] + [f"- skipped, {k}: {v}" for k, v in skipped.items()]
    lines += [f"{r['row']} @{r['handle']} [{r['account']}] step {r['step']} {r['lang']} - {r['result']}"
              + (f"\n   {r['text']!r}" if dry else "") for r in results[:60]]
    report = "\n".join(lines)
    store.save("followup_last.json", {"at": now.isoformat(), "dry": dry, "results": results})
    if args.report == "operator" and (results or skipped):
        OpsBot(ops_cfg.ops_bot_token, ops_cfg.ops_chat_id).send(report)
    print(report)
    return 0


def _claim_step(store, candidate, *, release=False):
    """A process crash or lost acknowledgement holds this step for manual review."""
    store.ensure()
    with closing(sqlite3.connect(store.dir / "followup_attempts.sqlite3")) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS attempts "
                   "(peer TEXT, opener TEXT, step INTEGER, PRIMARY KEY(peer, opener, step))")
        key = (candidate["handle"], candidate["opener"], candidate["step"])
        if release:
            db.execute("DELETE FROM attempts WHERE peer=? AND opener=? AND step=?", key)
            return
        return db.execute("INSERT OR IGNORE INTO attempts VALUES (?, ?, ?)", key).rowcount == 1


async def _send_one(c, acct, clients, blocked, store, svc, tracker, fcfg) -> str:
    from telethon.errors import FloodWaitError, PeerFloodError

    from . import sender
    if acct not in clients:
        try:
            cl = sender.get_client(acct, sender.load_config())
            await cl.connect()
            if not await cl.is_user_authorized():
                blocked[acct] = "session not authorized"
                return f"skip: {blocked[acct]}"
            clients[acct] = cl
        except Exception as e:
            blocked[acct] = f"connect failed: {str(e)[:80]}"
            return f"skip: {blocked[acct]}"
    cl = clients[acct]
    try:
        msgs = await cl.get_messages(c["handle"], limit=15)
    except FloodWaitError as e:
        blocked[acct] = f"FloodWait {e.seconds}s"
        return f"skip: {blocked[acct]}"
    except Exception as e:
        return f"skip: chat not available ({str(e)[:60]})"
    outs = [m for m in msgs if m.out]
    if not outs:
        return f"skip: no message of ours in {acct}"
    if any((not m.out) and m.date > outs[0].date for m in msgs):
        return "skip: contact already answered, update the tracker"
    if (outs[0].raw_text or "").strip():
        c["lang"] = detect_lang(outs[0].raw_text)
    if sender.peer_stopped(c["handle"]):
        return "skip: company opt-out"
    if not _claim_step(store, c):
        return "skip: prior send attempt, check conversation and tracker"
    try:
        sent = await cl.send_message(c["handle"], c["text"])
    except (PeerFloodError, FloodWaitError) as e:
        _claim_step(store, c, release=True)
        blocked[acct] = type(e).__name__
        sender.lock_on_flood(acct, e)
        return f"stop: {blocked[acct]}"
    except Exception as e:
        return f"delivery unknown: check conversation ({type(e).__name__})"
    sender.log_send(account=acct, peer="@" + c["handle"], kind=f"followup{c['step']}", hypothesis=None,
                    message_id=sent.id, text=c["text"])
    if svc is None:
        return "sent"
    return "sent, " + _update_stage(c, svc, tracker, fcfg)


def _update_stage(c, svc, tracker, fcfg):
    sheet, tab, cols = os.environ["SHEET_ID"], tracker["tab"], tracker["columns"]
    try:
        api = svc.spreadsheets().values()
        row = api.get(spreadsheetId=sheet, range=f"'{tab}'!{c['row']}:{c['row']}").execute().get("values", [])
        cells = row[0] if row else []
        peer_col, stage_col = col_index(cols["telegram"]), col_index(cols["stage"])
        if len(cells) <= max(peer_col, stage_col):
            return "row moved: check tracker"
        handles = {norm_handle(x) for x in re.split(r"[\s,;]+", str(cells[peer_col]))}
        if c["handle"] not in handles or str(cells[stage_col]).strip() != c["stage"]:
            return "tracker changed: check the row"
        want = next_stage(fcfg, c["step"])
        rng = f"'{tab}'!{cols['stage']}{c['row']}"
        api.update(spreadsheetId=sheet, range=rng, valueInputOption="RAW", body={"values": [[want]]}).execute()
        back = api.get(spreadsheetId=sheet, range=rng).execute().get("values", [])
        return f"stage {want}" if back and back[0] and back[0][0] == want else "check tracker stage"
    except Exception as e:
        return f"tracker writeback pending ({type(e).__name__})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Automatic follow-ups from the tracker")
    ap.add_argument("--dry-run", action="store_true", help="never send")
    ap.add_argument("--rows-json", help="offline tracker rows instead of Google Sheets")
    ap.add_argument("--ignore-toggle", action="store_true", help="treat every watched account as enabled (use with --dry-run)")
    ap.add_argument("--ignore-window", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--report", choices=["stdout", "operator"], default="operator")
    return asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
