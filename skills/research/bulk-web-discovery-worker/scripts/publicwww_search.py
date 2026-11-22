#!/usr/bin/env python3
"""Search PublicWWW for page-source phrases and append candidates to a discovery run.

Choose quoted phrases from sites that match the current hypothesis. PublicWWW
orders matches by domain popularity. Narrow a broad result set before spending
more budget on pages. Check phrases from different writing systems separately;
an unexpectedly large result set can indicate that a term had no effect.
The depth:all option can return several pages from one domain.

Preview the total with a phrase from the current search:
    python publicwww_search.py --check --query '"phrase from a buyer site"'

To append to raw_results.jsonl, pass --run-dir and omit --check. Run
validation_worker.py --verify on the same directory to score the new domains.
Each query, including --check, reserves a lookup from the configured budget.
The script spaces requests to the service.
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
import _budget  # noqa: E402  shared daily API budget (limits.budgets.publicwww_daily)

load_env()

API_BASE = "https://api.publicwww.com"
_MIN_INTERVAL_SECS = 16.0  # keeps well under "2 requests per 30 seconds"
_last_call_at = 0.0


def _pace() -> None:
    global _last_call_at
    elapsed = time.time() - _last_call_at
    if elapsed < _MIN_INTERVAL_SECS:
        time.sleep(_MIN_INTERVAL_SECS - elapsed)
    _last_call_at = time.time()


def publicwww_search(query: str, per_page: int) -> dict:
    import httpx

    api_key = os.environ.get("PUBLICWWW_API_KEY", "").strip()
    if not api_key:
        return {"ok": False, "error": "PUBLICWWW_API_KEY is required"}

    try:
        _budget.reserve("publicwww")   # booked before the request: a failed call may still cost a search
    except _budget.BudgetExhausted as exc:
        return {"ok": False, "error": str(exc), "budget": True}
    _pace()
    try:
        resp = httpx.get(
            f"{API_BASE}/v1/search",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"query": query, "per_page": per_page},
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    if resp.status_code == 429:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        retry_after = (body.get("error") or {}).get("retry_after", 30)
        return {"ok": False, "error": f"rate limited, retry_after={retry_after}s — re-run in a moment"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}

    data = resp.json()
    return {
        "ok": True,
        "total": data.get("total"),
        "returned": data.get("returned"),
        "truncated": data.get("truncated"),
        "results": data.get("results", []),
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True, help="PublicWWW query containing the page-source phrases to match")
    ap.add_argument("--per-page", type=int, default=100, help="Rows to fetch (same 1-search cost regardless — see script docstring)")
    ap.add_argument("--check", action="store_true", help="Print total/returned only, do not append anywhere. Use this first.")
    ap.add_argument("--run-dir", default=None, help="Required unless --check. Appends into <dir>/raw_results.jsonl")
    args = ap.parse_args()

    if not args.check and not args.run_dir:
        print("ERROR: --run-dir is required unless --check is passed.", file=sys.stderr)
        sys.exit(1)

    result = publicwww_search(args.query, args.per_page)
    if not result["ok"]:
        print(f"FAILED: {result['error']}", file=sys.stderr)
        sys.exit(_budget.BUDGET_EXIT if result.get("budget") else 1)

    total = result["total"]
    returned = result["returned"]
    print(f"Query: {args.query!r}")
    print(f"Total matches: {total}  |  Returned this call: {returned}  |  Truncated: {result['truncated']}")

    if total and total > 500:
        print(
            f"\nWARNING: {total} pages match this query. PublicWWW ranks by domain popularity. "
            "Add a more specific required phrase to surface smaller sites.",
            file=sys.stderr,
        )

    if args.check:
        print("\n--check mode: nothing fetched further, nothing written. Re-run without --check "
              "once `total` looks reasonably small (tens to a few hundred).")
        return

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_path = run_dir / "raw_results.jsonl"

    seen_before = set()
    if raw_path.exists():
        with open(raw_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    seen_before.add(normalize_domain(json.loads(line)["url"]))

    new_count = 0
    with open(raw_path, "a", encoding="utf-8") as f:
        for r in result["results"]:
            url = r.get("url") or f"https://{r.get('domain', '')}/"
            domain = normalize_domain(url)
            if not domain or domain in seen_before:
                continue
            seen_before.add(domain)
            new_count += 1
            f.write(json.dumps({
                "query": args.query,
                "backend": "publicwww",
                "fetched_at": now_iso(),
                "title": "",
                "url": url,
                "snippet": "",
            }, ensure_ascii=False) + "\n")

    print(f"\nAppended {new_count} new domains to {raw_path} (skipped {len(result['results']) - new_count} already-seen).")
    print(f"Next: python validation_worker.py --run-dir {run_dir} --verify")


if __name__ == "__main__":
    main()
