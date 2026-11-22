#!/usr/bin/env python3
"""Dedup + tiered validation/scoring over a discovery_worker.py raw run.

Reads raw_results.jsonl (produced by discovery_worker.py in the same
--run-dir), collapses results to one row per normalized domain, then
(with --verify) runs each domain through a tiered relevance check:

  1. robots.txt-respecting direct HTTP fetch of the homepage (small
     per-run cache, min delay between requests).
  2. Lightweight extraction: title, meta description, headings, visible text.
  3. If the fetch fails outright or the extracted text is suspiciously thin
     (likely a JS-rendered shell) and --browser-fallback is passed, retry
     once through the local headless-browser tool.
  4. If direct HTTP is blocked (403/Cloudflare/timeout) and neither browser
     fallback is enabled nor succeeds, fall back to Keenable/Tavily's own
     fetch/extract endpoints as a last resort content source.
  5. Score the resulting text against --relevance-terms / --negative-terms
     (from config.json) and bucket into verified / needs_manual_check /
     rejected. A 403/Cloudflare/timeout with no fallback content is left as
     needs_manual_check, never silently marked rejected/dead.

Writes candidates.jsonl — a clean, scored list ready for HUMAN/BOT REVIEW.

This script never writes to Google Sheets or any tracker. That write is a
separate, explicit step the operator (or the bot, on the operator's
instruction) takes after reviewing candidates.jsonl — matching the
provenance rules already in place for outreach sends (a discovery hit is a
candidate, not a confirmed row, until a human/bot decision adds it).

Usage:
    python validation_worker.py --run-dir <dir>                        # dedup only
    python validation_worker.py --run-dir <dir> --verify                # + scored validation
    python validation_worker.py --run-dir <dir> --verify --browser-fallback
    python validation_worker.py --run-dir <dir> --verify --known-domains known.txt
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import load_env  # noqa: E402
from _domain import normalize_domain  # noqa: E402
import _budget  # noqa: E402  shared daily API budget (limits.budgets.tavily_daily)

load_env()

DEFAULT_UA = os.environ.get("KIT_BOT_USER_AGENT", "Mozilla/5.0 (compatible; LeadgenBot/1.0)")


# ---------------------------------------------------------------------------
# Extract HTML text and metadata with the standard library.
# ---------------------------------------------------------------------------
class _PageExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.meta_description = ""
        self.headings = []
        self.text_chunks = []
        self._in_title = False
        self._in_heading = False
        self._in_script_or_style = False

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag in ("h1", "h2", "h3"):
            self._in_heading = True
        elif tag in ("script", "style", "noscript"):
            self._in_script_or_style = True
        elif tag == "meta" and attrs_d.get("name", "").lower() == "description":
            self.meta_description = attrs_d.get("content", "")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("h1", "h2", "h3"):
            self._in_heading = False
        elif tag in ("script", "style", "noscript"):
            self._in_script_or_style = False

    def handle_data(self, data):
        if self._in_script_or_style:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title.append(text)
        elif self._in_heading:
            self.headings.append(text)
        else:
            self.text_chunks.append(text)

    def result(self) -> dict:
        return {
            "title": " ".join(self.title).strip(),
            "meta_description": (self.meta_description or "").strip(),
            "headings": self.headings[:20],
            "text": " ".join(self.text_chunks)[:8000],
        }


def extract_page(html: str) -> dict:
    parser = _PageExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — malformed HTML, keep whatever we got
        pass
    return parser.result()


# ---------------------------------------------------------------------------
# robots.txt-respecting fetch with a per-run cache + politeness delay.
# ---------------------------------------------------------------------------
class Fetcher:
    def __init__(self, delay_seconds: float = 2.0, timeout: float = 15.0):
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_request = 0.0

    def _get_robots(self, scheme_host: str) -> urllib.robotparser.RobotFileParser:
        if scheme_host in self._robots_cache:
            return self._robots_cache[scheme_host]
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(scheme_host.rstrip("/") + "/robots.txt")
        try:
            import httpx

            resp = httpx.get(
                scheme_host.rstrip("/") + "/robots.txt",
                headers={"User-Agent": DEFAULT_UA},
                timeout=self.timeout,
                follow_redirects=True,
            )
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp.parse([])  # no robots.txt -> allow-all
        except Exception:  # noqa: BLE001 — treat unreachable robots.txt as allow-all
            rp.parse([])
        self._robots_cache[scheme_host] = rp
        return rp

    def _pace(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)
        self._last_request = time.time()

    def fetch(self, domain: str) -> dict:
        """Return {ok, status, html, final_url, error, blocked_by_robots}."""
        import httpx

        scheme_host = f"https://{domain}"
        rp = self._get_robots(scheme_host)
        if not rp.can_fetch(DEFAULT_UA, scheme_host + "/"):
            return {"ok": False, "status": None, "html": "", "final_url": scheme_host,
                     "error": "disallowed by robots.txt", "blocked_by_robots": True}

        self._pace()
        try:
            resp = httpx.get(
                scheme_host,
                headers={"User-Agent": DEFAULT_UA},
                timeout=self.timeout,
                follow_redirects=True,
            )
            return {
                "ok": resp.status_code < 400,
                "status": resp.status_code,
                "html": resp.text if resp.status_code < 400 else "",
                "final_url": str(resp.url),
                "error": None if resp.status_code < 400 else f"HTTP {resp.status_code}",
                "blocked_by_robots": False,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": None, "html": "", "final_url": scheme_host,
                     "error": str(exc), "blocked_by_robots": False}


# Reuse one browser session for all domains to bound Chromium memory use.
_BROWSER_FALLBACK_TASK_ID = "bulk_web_discovery_validation"


def browser_fallback_fetch(domain: str) -> dict:
    """Return a Fetcher.fetch()-shaped result, including errors from the local browser."""
    try:
        agent_dir = os.environ.get("HERMES_AGENT_DIR", "")
        if agent_dir and agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)
        from tools.browser_tool import browser_navigate, browser_snapshot  # type: ignore

        task_id = _BROWSER_FALLBACK_TASK_ID
        nav = browser_navigate(f"https://{domain}", task_id=task_id)
        nav_d = json.loads(nav) if isinstance(nav, str) else nav
        if not nav_d.get("success"):
            return {"ok": False, "status": None, "html": "", "final_url": f"https://{domain}",
                     "error": nav_d.get("error", "browser navigate failed"), "blocked_by_robots": False}
        snap = browser_snapshot(task_id=task_id)
        snap_d = json.loads(snap) if isinstance(snap, str) else snap
        text = snap_d.get("snapshot", "")
        # Wrap the accessibility-tree text as a pseudo-HTML body so the same
        # extract_page() scoring path can consume it uniformly.
        pseudo_html = f"<title>{nav_d.get('title', '')}</title><body>{text}</body>"
        return {"ok": True, "status": 200, "html": pseudo_html,
                 "final_url": nav_d.get("url", f"https://{domain}"), "error": None,
                 "blocked_by_robots": False}
    except Exception as exc:  # noqa: BLE001 — browser tool not usable here
        return {"ok": False, "status": None, "html": "", "final_url": f"https://{domain}",
                 "error": f"browser fallback unavailable: {exc}", "blocked_by_robots": False}


def keenable_tavily_fetch(domain: str) -> dict:
    """Last-resort content source via Keenable/Tavily's own fetch/extract."""
    import httpx

    url = f"https://{domain}"
    keenable_key = os.environ.get("KEENABLE_API_KEY", "").strip()
    base = os.environ.get("KEENABLE_API_URL", "https://api.keenable.ai").rstrip("/")
    path = "/v1/fetch" if keenable_key else "/v1/fetch/public"
    headers = {"X-Keenable-Title": "Hermes"}
    if keenable_key:
        headers["X-API-Key"] = keenable_key
    try:
        resp = httpx.get(base + path, headers=headers, params={"url": url}, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        content = payload.get("content") or ""
        title = payload.get("title") or ""
        if content or title:
            pseudo_html = f"<title>{title}</title><body>{content}</body>"
            return {"ok": True, "status": 200, "html": pseudo_html, "final_url": url,
                     "error": None, "blocked_by_robots": False}
    except Exception as exc:  # noqa: BLE001
        keenable_error = str(exc)
    else:
        keenable_error = "empty response"

    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        try:
            _budget.reserve("tavily")   # booked before the request, never refunded
        except _budget.BudgetExhausted as exc:
            return {"ok": False, "status": None, "html": "", "final_url": url, "error": str(exc),
                    "blocked_by_robots": False, "budget": True}
        try:
            tbase = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/")
            resp = httpx.post(
                tbase + "/extract",
                json={"urls": [url], "api_key": tavily_key, "include_images": False},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or []
            if results:
                raw_content = results[0].get("raw_content", "") or ""
                pseudo_html = f"<body>{raw_content}</body>"
                return {"ok": True, "status": 200, "html": pseudo_html, "final_url": url,
                         "error": None, "blocked_by_robots": False}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": None, "html": "", "final_url": url,
                     "error": f"keenable: {keenable_error}; tavily: {exc}", "blocked_by_robots": False}

    return {"ok": False, "status": None, "html": "", "final_url": url,
             "error": f"keenable: {keenable_error}; tavily not configured", "blocked_by_robots": False}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
DEFAULT_NEGATIVE_TERMS = [
    "directory", "top 10", "top10", "best 10", "comparison", "vs.", "review of",
    "рейтинг", "каталог сайтов", "топ-10", "сравнение",
]

MIN_TEXT_LEN_FOR_JS_SUSPECT = 300


def score_page(extracted: dict, relevance_terms: list[str], negative_terms: list[str]) -> tuple[int, int, list[str]]:
    """Return (score, term_hits, reasons). Classification checks topical hits separately."""
    combined = " ".join([
        extracted.get("title", ""),
        extracted.get("meta_description", ""),
        " ".join(extracted.get("headings", [])),
        extracted.get("text", ""),
    ]).lower()

    score = 0
    reasons = []

    term_hits = sum(1 for t in relevance_terms if t.lower() in combined)
    if term_hits:
        score += term_hits
        reasons.append(f"+{term_hits} relevance term(s) present ({term_hits} distinct)")

    # Minor supporting signal only — having a pricing/services page says
    # almost nothing about topical relevance (nearly every commercial
    # website has one), so it must never be able to push a low-term-hit
    # site over the verified line by itself. See classify()'s hard gate.
    if any(k in combined for k in (
        "pricing", "package", "our services", "solutions", "products",
        "цена", "услуги", "тариф", "прайс",
    )):
        score += 1
        reasons.append("+1 service/pricing page signal (minor, supporting only)")

    # Common commercial pages may mention a directory keyword in passing.
    # Multiple distinct negative terms carry a stronger penalty.
    neg_hits = [t for t in negative_terms if t.lower() in combined]
    if len(neg_hits) >= 2:
        score -= 3
        reasons.append(f"-3 negative/directory signal(s): {', '.join(neg_hits[:3])}")
    elif len(neg_hits) == 1:
        score -= 1
        reasons.append(f"-1 single negative signal (weak on its own): {neg_hits[0]}")

    if len(extracted.get("text", "")) < 80 and not extracted.get("title"):
        score -= 2
        reasons.append("-2 effectively empty page")

    return score, term_hits, reasons


MIN_TERM_HITS_FOR_VERIFIED = 3


def classify(score: int, term_hits: int, fetch_result: dict, known_domains: set[str], domain: str) -> str:
    """Require both a sufficient score and independent topical matches for verification.

    Common site features such as pricing and contact pages add supporting evidence.
    The topical threshold prevents those features alone from verifying a candidate.
    """
    if domain in known_domains:
        return "duplicate"
    if fetch_result.get("blocked_by_robots"):
        return "needs_manual_check"
    if not fetch_result.get("ok"):
        status = fetch_result.get("status")
        if status in (403,) or "cloudflare" in (fetch_result.get("error") or "").lower() or status is None:
            return "needs_manual_check"  # unresolved, not proof of death
        return "rejected"
    if score >= 4 and term_hits >= MIN_TERM_HITS_FOR_VERIFIED:
        return "verified"
    if score >= 1 or term_hits >= 1:
        return "needs_manual_check"
    return "rejected"


# ---------------------------------------------------------------------------
# Dedup + main
# ---------------------------------------------------------------------------
def load_jsonl(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


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


def dedup(raw_path: Path) -> dict:
    by_domain: dict[str, dict] = {}
    for row in load_jsonl(raw_path):
        domain = normalize_domain(row["url"])
        if not domain:
            continue
        entry = by_domain.setdefault(domain, {
            "domain": domain,
            "sample_url": row["url"],
            "title": row.get("title", ""),
            "snippet": row.get("snippet", ""),
            "source_queries": [],
            "hit_count": 0,
        })
        entry["hit_count"] += 1
        q = row.get("query", "")
        if q and q not in entry["source_queries"]:
            entry["source_queries"].append(q)
        if not entry["title"] and row.get("title"):
            entry["title"] = row["title"]
    return by_domain


def load_known_domains(path: str | None) -> set[str]:
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    out = set()
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.add(normalize_domain(line) if "//" in line or "." in line else line.lower())
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--verify", action="store_true", help="Run tiered validation + scoring")
    ap.add_argument("--browser-fallback", action="store_true",
                     help="Retry via local headless browser when direct HTTP fails or looks JS-only")
    ap.add_argument("--known-domains", default=None,
                     help="Optional file of already-tracked domains/URLs (one per line) for duplicate marking")
    ap.add_argument("--relevance-terms", default=None,
                     help="Comma-separated relevance terms; defaults to config.json's relevance_terms")
    ap.add_argument("--negative-terms", default=None,
                     help="Comma-separated negative/directory-signal terms")
    ap.add_argument("--delay-seconds", type=float, default=2.0)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    raw_path = run_dir / "raw_results.jsonl"
    candidates_path = run_dir / "candidates.jsonl"
    verify_state_path = run_dir / "verify_state.json"
    config_path = run_dir / "config.json"

    if not raw_path.exists():
        print(f"ERROR: {raw_path} not found — run discovery_worker.py first.", file=sys.stderr)
        sys.exit(1)

    config = load_json(config_path, {})

    relevance_terms = (
        [t.strip() for t in args.relevance_terms.split(",")] if args.relevance_terms
        else config.get("relevance_terms", [])
    )
    negative_terms = (
        [t.strip() for t in args.negative_terms.split(",")] if args.negative_terms
        else config.get("negative_terms", DEFAULT_NEGATIVE_TERMS)
    )
    known_domains = load_known_domains(args.known_domains)

    by_domain = dedup(raw_path)
    print(f"Deduped {sum(e['hit_count'] for e in by_domain.values())} raw hits "
          f"into {len(by_domain)} unique domains.")

    if args.verify:
        if not relevance_terms:
            print("WARNING: no relevance_terms configured (config.json or --relevance-terms) — "
                  "scoring will rely only on generic service/contact signals.", file=sys.stderr)

        verify_state = load_json(verify_state_path, {})
        pending = [d for d in by_domain if d not in verify_state]
        print(f"Validating: {len(verify_state)} already checked, {len(pending)} to go.")

        fetcher = Fetcher(delay_seconds=args.delay_seconds)
        budget_stop = None
        for i, domain in enumerate(pending, 1):
            print(f"  [{i}/{len(pending)}] {domain} ...", end=" ", flush=True)
            result = fetcher.fetch(domain)
            source = "http"

            need_fallback = not result.get("blocked_by_robots") and (
                not result["ok"] or len(extract_page(result["html"]).get("text", "")) < MIN_TEXT_LEN_FOR_JS_SUSPECT
            )
            if need_fallback and args.browser_fallback:
                bresult = browser_fallback_fetch(domain)
                if bresult["ok"]:
                    result = bresult
                    source = "browser"
            if not result["ok"] and not result.get("blocked_by_robots"):
                kresult = keenable_tavily_fetch(domain)
                if kresult.get("budget"):
                    # leave this domain unchecked (it is retried next run) and stop instead of spending more
                    budget_stop = kresult["error"]
                    print("STOPPED: " + budget_stop)
                    break
                if kresult["ok"]:
                    result = kresult
                    source = "keenable_tavily"

            extracted = extract_page(result["html"]) if result["html"] else {
                "title": "", "meta_description": "", "headings": [], "text": "",
            }
            score, term_hits, reasons = score_page(extracted, relevance_terms, negative_terms)
            status = classify(score, term_hits, result, known_domains, domain)

            verify_state[domain] = {
                "ok": result["ok"],
                "status": result.get("status"),
                "error": result.get("error"),
                "blocked_by_robots": result.get("blocked_by_robots", False),
                "source": source,
                "score": score,
                "term_hits": term_hits,
                "reasons": reasons,
                "validation_status": status,
                "title": extracted.get("title", ""),
                "meta_description": extracted.get("meta_description", ""),
                "evidence_url": result.get("final_url") if result["ok"] else None,
                "page_excerpt": extracted.get("text", "")[:1600] if result["ok"] else "",
            }
            save_json(verify_state_path, verify_state)
            print(f"{status} (score {score}, via {source})")

        for domain, entry in by_domain.items():
            v = verify_state.get(domain, {})
            entry["validation_status"] = v.get("validation_status")
            entry["score"] = v.get("score")
            entry["term_hits"] = v.get("term_hits")
            entry["reasons"] = v.get("reasons")
            entry["fetch_ok"] = v.get("ok")
            entry["fetch_error"] = v.get("error")
            entry["fetched_title"] = v.get("title")
            entry["fetched_meta_description"] = v.get("meta_description")
            entry["fetch_source"] = v.get("source")
            entry["evidence_url"] = v.get("evidence_url")
            entry["page_excerpt"] = v.get("page_excerpt", "")
    else:
        for entry in by_domain.values():
            entry["validation_status"] = None
            entry["score"] = None

    with open(candidates_path, "w", encoding="utf-8") as f:
        for entry in sorted(by_domain.values(), key=lambda e: (-(e.get("score") or -99), -e["hit_count"])):
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if args.verify:
        counts: dict[str, int] = {}
        for entry in by_domain.values():
            s = entry.get("validation_status") or "unknown"
            counts[s] = counts.get(s, 0) + 1
        print("\nValidation summary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    print(f"Wrote {len(by_domain)} candidates to {candidates_path}")
    print("Nothing was written to any Sheet/tracker — review candidates.jsonl "
          "and add confirmed rows through the normal tracker-write process.")

    if args.verify and args.browser_fallback:
        _close_browser_fallback_session()
    if args.verify and budget_stop:
        print(f"Validation stopped early ({budget_stop}); unchecked domains stay pending for the next run.")
        sys.exit(_budget.BUDGET_EXIT)


def _close_browser_fallback_session() -> None:
    """Release the shared browser session if the optional browser stage opened it."""
    try:
        agent_dir = os.environ.get("HERMES_AGENT_DIR", "")
        if agent_dir and agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore

        SUPERVISOR_REGISTRY.stop(_BROWSER_FALLBACK_TASK_ID)
        print(f"Closed browser-fallback session ({_BROWSER_FALLBACK_TASK_ID}).")
    except Exception as exc:  # noqa: BLE001
        print(f"NOTE: could not confirm browser-fallback session closed ({exc}). "
              f"Check `ps aux | grep agent-browser/browsers/chrome` on this VPS "
              f"and kill any leftover process by hand if present.", file=sys.stderr)


if __name__ == "__main__":
    main()
