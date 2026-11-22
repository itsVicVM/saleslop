#!/usr/bin/env python3
"""Resumable bulk web-search discovery worker (Keenable / Tavily).

Search adapters in this worker submit one request per query. Keenable returns
a fixed set of results for each query and exposes no pagination here.
- Tavily `POST /search` accepts `max_results` capped at 20, also with no
  offset/page param — one call, one shot, no pagination either.

Consequence: there is no "exhaust pagination" step for either backend.
The only way to grow coverage is a QUERY MATRIX — many different phrasings/
angles/languages run as separate calls — not paging through one query.

This script executes a list of (query, backend) pairs from a run config,
checkpointing after every single call so it is safe to kill and resume.
It never writes to Google Sheets or any tracker — output is a local
raw_results.jsonl for a separate validation/dedup pass (see
validation_worker.py) and human/bot review before anything is written
anywhere permanent.

Usage:
    python discovery_worker.py --run-dir <dir>

<dir> must contain config.json:
    {
      "run_id": "free text label",
      "queries": [
        {"text": "search phrase", "backends": ["keenable", "tavily"]},
        {"text": "another phrase", "backends": ["keenable"]}
      ],
      "delay_seconds": 2
    }

state.json and raw_results.jsonl are created/updated in the same directory.
Re-running the same --run-dir resumes: already-completed (query, backend)
pairs are skipped, never re-fetched, never duplicated.

## Adaptive "keep going while it's still working" loop

This script does NOT invent new queries on its own — that needs actual
judgment (an LLM deciding what phrasing to try next), which belongs to the
operator/bot, not this script. What it DOES do: track, per (query, backend)
pair, how many of that pair's results were domains never seen before
anywhere else in this run ("new_domains"), persisted in state.json and
printed in a yield-trend table after every pass. That number is the signal
for the bot-in-the-loop decision:

    1. Seed config.json with an initial batch of queries (10-15 is a
       reasonable start), run this script.
    2. Read the yield-trend table this script prints. If the last few
       pairs are still finding a meaningful number of new domains, the
       matrix isn't exhausted — the operator/bot writes MORE query
       variations, appends them to config.json's "queries" list, and
       re-runs the SAME --run-dir. Already-done pairs are skipped
       automatically; only the new ones run.
    3. Stop appending once new-domain yield drops near zero for several
       pairs in a row (e.g. the last 3-5 pairs each added 0-1 new
       domains) — that's the plateau signal that this query angle is
       exhausted, not a fixed pair count.

So the "keep making queries while correct domains keep turning up" loop is
real, but it is a bot-writes-config / script-reports-yield loop across
repeated invocations, not a single unattended run that invents its own
queries.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import load_env  # noqa: E402
from _domain import normalize_domain  # noqa: E402
import _budget  # noqa: E402  shared daily API budget (limits.budgets.<backend>_daily)

load_env()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def keenable_search(query: str) -> dict:
    import httpx

    api_key = os.environ.get("KEENABLE_API_KEY", "").strip()
    base = os.environ.get("KEENABLE_API_URL", "https://api.keenable.ai").rstrip("/")
    path = "/v1/search" if api_key else "/v1/search/public"
    headers = {"X-Keenable-Title": "Hermes", "Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    resp = httpx.get(base + path, headers=headers, params={"query": query}, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    results = []
    for r in data.get("results", []) or []:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": " ".join(str(r.get("snippet") or r.get("description") or "").split())[:500],
        })
    return {"ok": True, "results": results}


def tavily_search(query: str) -> dict:
    import httpx

    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return {"ok": False, "error": "TAVILY_API_KEY not set"}
    try:
        _budget.reserve("tavily")   # booked before the request, never refunded
    except _budget.BudgetExhausted as exc:
        return {"ok": False, "error": str(exc), "budget": True}
    base = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/")
    payload = {
        "query": query,
        "api_key": api_key,
        "max_results": 20,
        "include_raw_content": False,
        "include_images": False,
    }
    resp = httpx.post(base + "/search", json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    results = []
    for r in data.get("results", []) or []:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("content", "") or "")[:500],
        })
    return {"ok": True, "results": results}


def serper_search(query: str) -> dict:
    import httpx

    key = os.environ.get("SERPER_API_KEY")
    if not key:
        return {"ok": False, "error": "SERPER_API_KEY not set"}
    try:
        _budget.reserve("serper")
    except _budget.BudgetExhausted as exc:
        return {"ok": False, "error": str(exc), "budget": True}
    payload = {"q": query}
    for param, name in (("gl", "SERPER_COUNTRY"), ("hl", "SERPER_LANGUAGE")):
        if value := os.environ.get(name):
            payload[param] = value
    response = httpx.post("https://google.serper.dev/search", json=payload,
                          headers={"X-API-KEY": key}, timeout=60)
    response.raise_for_status()
    results = [{"title": row.get("title", ""), "url": row["link"], "snippet": (row.get("snippet") or "")[:500]}
               for row in response.json().get("organic", []) if row.get("link")]
    return {"ok": True, "results": results}


BACKENDS = {"keenable": keenable_search, "tavily": tavily_search, "serper": serper_search}


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def pair_key(query_text: str, backend: str) -> str:
    return f"{backend}::{query_text}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, help="Directory containing config.json (state/results live alongside it)")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    config_path = run_dir / "config.json"
    state_path = run_dir / "state.json"
    raw_path = run_dir / "raw_results.jsonl"

    if not config_path.exists():
        print(f"ERROR: {config_path} not found. Create it first (see script docstring).", file=sys.stderr)
        sys.exit(1)

    config = load_json(config_path, {})
    queries = config.get("queries", [])
    delay = float(config.get("delay_seconds", 2))

    state = load_json(state_path, {
        "run_id": config.get("run_id", run_dir.name),
        "created_at": now_iso(),
        "completed_pairs": [],
        "seen_domains": [],
        "yield_history": [],  # [{pair, new_domains, total_domains, order}]
        "status": "in_progress",
    })
    completed = set(state.get("completed_pairs", []))
    seen_domains = set(state.get("seen_domains", []))
    yield_history = state.get("yield_history", [])

    all_pairs = []
    for q in queries:
        text = q["text"]
        for backend in q.get("backends", ["keenable"]):
            all_pairs.append((text, backend))

    total = len(all_pairs)
    todo = [(t, b) for (t, b) in all_pairs if pair_key(t, b) not in completed]
    print(f"Run '{state['run_id']}': {total} total (query, backend) pairs, "
          f"{total - len(todo)} already done, {len(todo)} to go.")

    if not todo:
        state["status"] = "completed"
        save_json(state_path, state)
        print("Nothing to do — already complete.")
        return

    fetched_count = 0
    exhausted: set[str] = set()   # backends whose daily budget ran out: their pairs stay pending for the next run
    for text, backend in todo:
        if backend in exhausted:
            continue
        fn = BACKENDS.get(backend)
        if fn is None:
            print(f"  SKIP unknown backend {backend!r} for query {text!r}")
            completed.add(pair_key(text, backend))
            state["completed_pairs"] = sorted(completed)
            save_json(state_path, state)
            continue

        print(f"  [{backend}] {text!r} ...", end=" ", flush=True)
        result = fn(text)
        if result.get("ok"):
            n = len(result["results"])
            new_domains_this_pair = 0
            with open(raw_path, "a", encoding="utf-8") as f:
                for r in result["results"]:
                    domain = normalize_domain(r["url"])
                    if domain and domain not in seen_domains:
                        seen_domains.add(domain)
                        new_domains_this_pair += 1
                    f.write(json.dumps({
                        "query": text,
                        "backend": backend,
                        "fetched_at": now_iso(),
                        "title": r["title"],
                        "url": r["url"],
                        "snippet": r["snippet"],
                    }, ensure_ascii=False) + "\n")
            fetched_count += n
            yield_history.append({
                "pair": pair_key(text, backend),
                "new_domains": new_domains_this_pair,
                "total_domains": n,
                "order": len(yield_history) + 1,
            })
            print(f"ok, {n} results ({new_domains_this_pair} new domains)")
        else:
            print(f"FAILED: {result.get('error')}")
            if result.get("budget"):
                exhausted.add(backend)
            # Do not mark as completed on failure — next run retries it.
            continue

        completed.add(pair_key(text, backend))
        state["completed_pairs"] = sorted(completed)
        state["seen_domains"] = sorted(seen_domains)
        state["yield_history"] = yield_history
        save_json(state_path, state)
        time.sleep(delay)

    remaining = total - len(completed)
    state["status"] = "completed" if remaining == 0 else ("budget_exhausted" if exhausted else "in_progress")
    save_json(state_path, state)
    print(f"\nDone this pass: {fetched_count} new raw results written to {raw_path}")
    print(f"Pairs completed so far: {len(completed)}/{total} — status: {state['status']}")
    if remaining:
        print("Some pairs failed and were left pending — re-run the same command to retry them.")

    print(f"\nTotal unique domains found across the whole run so far: {len(seen_domains)}")
    print("Yield trend (last 10 pairs run, in order — new domains per pair):")
    for entry in yield_history[-10:]:
        print(f"  #{entry['order']:>3}  +{entry['new_domains']:>2} new  "
              f"({entry['total_domains']} results)  {entry['pair']}")
    recent = yield_history[-5:]
    if len(recent) >= 3 and sum(e["new_domains"] for e in recent) <= len(recent):
        print("\nYield looks flat over the last few pairs (~1 or fewer new domains each) — "
              "this query angle may be exhausted. Consider a different phrasing/language/"
              "sub-niche rather than more queries in the same direction, or stop here.")
    else:
        print("\nStill finding meaningfully new domains — worth adding more query variations "
              "to config.json and re-running this same --run-dir if broader coverage is needed.")
    if exhausted:
        print(f"\nSTOPPED: daily budget used up for {', '.join(sorted(exhausted))}; their pairs stay pending. "
              "Re-run the same --run-dir after the budget resets.")
        sys.exit(_budget.BUDGET_EXIT)


if __name__ == "__main__":
    main()
