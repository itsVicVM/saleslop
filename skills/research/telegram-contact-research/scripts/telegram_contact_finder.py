#!/usr/bin/env python3
"""
Find public, business-relevant Telegram contacts for company outreach.

The script is intentionally evidence-first:
- official website mentions are scored highest;
- search-engine discoveries are kept only with a source URL/snippet;
- private/leaked data is out of scope;
- every row carries confidence and notes for manual review.

No third-party Python packages are required.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from typing import Iterable, Optional

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "ContactFinder/1.0"
)

TG_URL_RE = re.compile(
    r"(?:(?:https?:)?//)?(?:www\.)?(?P<host>t\.me|telegram\.me|telegram\.dog)/(?P<path>[A-Za-z0-9_+./-]{2,100})",
    re.IGNORECASE,
)
HANDLE_RE = re.compile(r"(?<![\w.])@(?P<handle>[A-Za-z][A-Za-z0-9_]{4,31})(?![\w.])")
LINK_RE = re.compile(r"href=[\"'](?P<href>[^\"']+)[\"']", re.IGNORECASE)
TITLE_RE = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)
META_RE = re.compile(
    r"<meta[^>]+(?:name|property)=[\"'](?:description|og:description|twitter:description)[\"'][^>]+content=[\"'](?P<content>.*?)[\"'][^>]*>",
    re.IGNORECASE | re.DOTALL,
)
TG_TITLE_RE = re.compile(r"tgme_page_title[^>]*>(?P<title>.*?)</div>", re.IGNORECASE | re.DOTALL)
TG_DESC_RE = re.compile(r"tgme_page_description[^>]*>(?P<desc>.*?)</div>", re.IGNORECASE | re.DOTALL)

SKIP_TG_PATH_PREFIXES = {
    "share",
    "iv",
    "addstickers",
    "addemoji",
    "proxy",
    "setlanguage",
    "joinchat",  # handled specially only when source is official
    "c",         # private/internal channel path, not a public handle
}

BUSINESS_KEYWORDS = {
    "support", "help", "sales", "partner", "partners", "partnership", "affiliate",
    "business", "bizdev", "bd", "manager", "founder", "ceo", "co-founder", "owner",
    "contact", "official", "community", "billing", "finance", "marketing",
    "advertising", "ads", "admin",
    "поддерж", "саппорт", "продаж", "партнер", "основатель", "директор", "менеджер",
}
# Vertical-specific role keywords come from the brand pack (targeting.contact_keywords),
# passed in as a comma-separated env var by the kit runner.
BUSINESS_KEYWORDS |= {
    k.strip().lower() for k in os.environ.get("KIT_CONTACT_KEYWORDS", "").split(",") if k.strip()
}
LOW_VALUE_KEYWORDS = {"news", "announcement", "announcements", "airdrop", "signal", "signals"}
# Recognize contact pages across the languages used by the crawler.
CONTACT_KEYWORDS = [
    "contact", "kontakt", "contacto", "contato", "contatti", "контакт",
    "support", "поддержка", "help", "помощь",
    "about", "über", "acerca", "partner", "партнер",
    "terms", "privacy", "impressum", "legal",
]
# Bare @tokens in modern websites are often CSS at-rules or JSON-LD keys, not Telegram handles.
# These are skipped unless they appear inside an explicit t.me/telegram URL handled by TG_URL_RE.
BARE_HANDLE_EXCLUDE = {
    "charset", "context", "type", "graph", "id", "media", "supports", "support", "keyframes",
    "layer", "property", "import", "font-face", "container", "tailwind", "theme", "apply",
    "screen", "page", "namespace", "document", "starting-style", "counter-style",
}
# Public Telegram/system/generic handles that are usually documentation placeholders, not company contacts.
GENERIC_TG_HANDLES = {
    "username", "telegram", "telegramtips", "durov", "botfather", "stickers", "gif", "jobs",
}
TWO_LEVEL_SUFFIXES = {
    "com.ua", "net.ua", "org.ua", "co.uk", "com.au", "com.br", "com.cn", "com.hk",
    "co.jp", "co.kr", "co.in", "com.tr", "co.il", "co.za", "us.com",
}

@dataclass
class Candidate:
    input_site: str
    root_domain: str
    company_guess: str
    telegram_url: str
    telegram_handle: str
    contact_type: str
    contact_name_or_title: str
    role_keywords: str
    confidence: str
    score: int
    source_type: str
    evidence_url: str
    evidence_text: str
    checked_at: str
    notes: str


def clean_text(value: str, max_len: int = 280) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<script.*?</script>|<style.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > max_len:
        return value[: max_len - 1].rstrip() + "…"
    return value


THIN_PAGE_MARKERS = ("loading...", "please enable javascript", "you need to enable javascript", "requires javascript")


def is_thin_page(html_text: str) -> bool:
    """Flag a page with little visible text for optional browser rendering."""
    body_text = clean_text(html_text, max_len=4000).lower()
    if len(body_text) < 200:
        return True
    return any(marker in body_text for marker in THIN_PAGE_MARKERS)


def normalize_input_site(raw: str) -> str:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return ""
    raw = raw.replace("http://", "").replace("https://", "").strip(" /")
    return raw


def host_from_site(site: str) -> str:
    parsed = urllib.parse.urlparse("https://" + site if "://" not in site else site)
    host = parsed.netloc or parsed.path.split("/")[0]
    return host.lower().strip(".")


def root_domain(host: str) -> str:
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    suffix2 = ".".join(parts[-2:])
    if suffix2 in TWO_LEVEL_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def company_guess_from_root(root: str) -> str:
    labels = root.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES:
        return labels[-3]
    return labels[0]


def fetch_url(url: str, timeout: float = 12.0) -> tuple[Optional[str], str, int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 0) or 0
            final_url = resp.geturl()
            ctype = resp.headers.get("content-type", "")
            raw = resp.read(700_000)
            charset = "utf-8"
            m = re.search(r"charset=([^;\s]+)", ctype, flags=re.I)
            if m:
                charset = m.group(1)
            text = raw.decode(charset, errors="replace")
            return text, final_url, status, ""
    except urllib.error.HTTPError as e:
        try:
            text = e.read(200_000).decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return text or None, url, e.code, f"HTTP {e.code}"
    except Exception as e:
        return None, url, 0, type(e).__name__ + ": " + str(e)[:120]


def candidate_urls_for_site(site: str, max_pages: int) -> list[str]:
    host = host_from_site(site)
    root = root_domain(host)
    base_hosts = []
    for h in [host, root, "www." + root]:
        if h not in base_hosts:
            base_hosts.append(h)
    paths = [
        "/", "/contact", "/contacts", "/contact-us", "/support", "/help", "/about", "/about-us", "/partners",
        "/terms", "/terms-of-service", "/privacy", "/privacy-policy", "/legal",
    ]
    urls = []
    for h in base_hosts:
        for scheme in ["https", "http"]:
            for path in paths:
                urls.append(f"{scheme}://{h}{path}")
                if len(urls) >= max_pages:
                    return urls
    return urls


def context_around(text: str, start: int, end: int, width: int = 140) -> str:
    return clean_text(text[max(0, start - width): min(len(text), end + width)])


def normalize_tg_url(host: str, path: str, official_source: bool = False) -> tuple[str, str, str, str]:
    # Returns (url, handle, contact_type, note). Empty url means skip.
    path = html.unescape(path)
    path = re.split(r"[?#&\s'\")<>]", path)[0].strip("/.,;:!")
    if not path:
        return "", "", "", "empty path"
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "", "", "", "empty path"
    first = parts[0]
    lower_first = first.lower()

    if lower_first == "s" and len(parts) >= 2:
        first = parts[1]
        lower_first = first.lower()

    if lower_first in {"joinchat"} and len(parts) >= 2:
        # Invite links are useful only when the official site publishes them.
        if official_source:
            invite = "joinchat/" + parts[1]
            return f"https://t.me/{invite}", "", "group_invite", "official invite link"
        return "", "", "", "non-official invite skipped"

    if first.startswith("+"):
        if official_source:
            return f"https://t.me/{first}", "", "group_invite", "official invite link"
        return "", "", "", "non-official invite skipped"

    if lower_first in SKIP_TG_PATH_PREFIXES:
        return "", "", "", f"skipped Telegram utility path: {first}"

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", first):
        return "", "", "", "not a public username"
    if first.lower() in GENERIC_TG_HANDLES:
        return "", "", "", "generic Telegram/system username skipped"

    return f"https://t.me/{first}", first, "unknown", ""


def extract_telegram_mentions(text: str, evidence_url: str, source_type: str, input_site: str, root: str, brand: str) -> list[dict]:
    official = source_type == "official_site"
    mentions: list[dict] = []

    for m in TG_URL_RE.finditer(text or ""):
        url, handle, ctype, note = normalize_tg_url(m.group("host"), m.group("path"), official_source=official)
        if not url:
            continue
        ctx = context_around(text, m.start(), m.end())
        mentions.append({
            "url": url,
            "handle": handle,
            "contact_type": ctype,
            "evidence_url": evidence_url,
            "evidence_text": ctx,
            "source_type": source_type,
            "notes": note,
        })

    # Bare @handles are noisy. Keep only if local context explicitly says Telegram/t.me.
    # Do not treat generic words near "contact" as handles; CSS @supports/@media and JSON-LD
    # @context/@graph otherwise create convincing false positives.
    for m in HANDLE_RE.finditer(text or ""):
        handle = m.group("handle")
        if handle.lower() in BARE_HANDLE_EXCLUDE or handle.lower() in GENERIC_TG_HANDLES:
            continue
        ctx = context_around(text, m.start(), m.end())
        ctx_l = ctx.lower()
        if not any(k in ctx_l for k in ["telegram", "телеграм", " t.me/", "https://t.me", "tg:"]):
            continue
        mentions.append({
            "url": f"https://t.me/{handle}",
            "handle": handle,
            "contact_type": "unknown",
            "evidence_url": evidence_url,
            "evidence_text": ctx,
            "source_type": source_type,
            "notes": "bare @handle near explicit Telegram context",
        })
    return mentions


def extract_outlinks(html_text: str, base_url: str) -> list[str]:
    links = []
    for m in LINK_RE.finditer(html_text or ""):
        href = html.unescape(m.group("href"))
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        links.append(urllib.parse.urljoin(base_url, href))
    return links


def ddg_search(query: str, limit: int = 6, delay: float = 0.8) -> list[tuple[str, str]]:
    time.sleep(delay)
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    text, final_url, status, err = fetch_url(url, timeout=15)
    if not text:
        return []
    results: list[tuple[str, str]] = []
    # Extract redirect URLs and snippets. DDG HTML uses uddg=<encoded-url>.
    for m in re.finditer(r"uddg=(?P<u>[^&\"'>]+)", text):
        u = urllib.parse.unquote(html.unescape(m.group("u")))
        if u.startswith("http") and u not in [r[0] for r in results]:
            pos = m.start()
            results.append((u, context_around(text, pos, pos + 20, width=220)))
        if len(results) >= limit:
            break
    # If DDG exposes t.me URLs in raw text, keep them too.
    for m in TG_URL_RE.finditer(text):
        raw = m.group(0)
        if raw.startswith("//"):
            raw = "https:" + raw
        elif not raw.startswith("http"):
            raw = "https://" + raw
        if raw not in [r[0] for r in results]:
            results.append((raw, context_around(text, m.start(), m.end(), width=220)))
        if len(results) >= limit:
            break
    return results


def telegram_preview(handle: str) -> tuple[str, str, str]:
    if not handle:
        return "", "", ""
    url = f"https://t.me/{handle}"
    text, final, status, err = fetch_url(url, timeout=10)
    if not text:
        return "", "", err
    title = ""
    desc = ""
    mt = TG_TITLE_RE.search(text) or TITLE_RE.search(text)
    md = TG_DESC_RE.search(text) or META_RE.search(text)
    if mt:
        title = clean_text(mt.group("title"), 120)
    if md:
        desc = clean_text(md.group("desc") if "desc" in md.groupdict() else md.group("content"), 180)
    return title, desc, ""


def classify_and_score(mention: dict, root: str, brand: str, preview_title: str, preview_desc: str) -> tuple[int, str, str, str, str]:
    text = " ".join([
        mention.get("handle", ""), mention.get("evidence_text", ""), preview_title or "", preview_desc or ""
    ]).lower()
    handle = (mention.get("handle") or "").lower()
    score = 0
    if mention["source_type"] == "official_site":
        score += 70
    else:
        score += 35
    if brand and (brand.lower() in handle or brand.lower() in text):
        score += 18
    if root.lower() in text:
        score += 12
    role_hits = sorted({k for k in BUSINESS_KEYWORDS if k in text})
    low_hits = sorted({k for k in LOW_VALUE_KEYWORDS if k in text})
    score += min(20, len(role_hits) * 5)
    score -= min(15, len(low_hits) * 5)

    contact_type = mention.get("contact_type") or "unknown"
    if contact_type == "unknown":
        if any(k in text for k in ["support", "help", "поддерж", "саппорт"]):
            contact_type = "support"
        elif any(k in text for k in ["sales", "partner", "business", "affiliate", "manager", "продаж", "партнер"]):
            contact_type = "business_contact"
        elif any(k in text for k in ["founder", "ceo", "co-founder", "owner", "основатель", "директор"]):
            contact_type = "person_public_business"
        elif any(k in text for k in ["official", "community", "channel", "group", "telegram"]):
            contact_type = "official_channel_or_group"
        elif mention.get("handle"):
            contact_type = "telegram_username"

    if score >= 80:
        confidence = "high"
    elif score >= 55:
        confidence = "medium"
    else:
        confidence = "low"

    title = preview_title or ""
    if preview_desc and preview_desc.lower() not in title.lower():
        title = (title + " — " + preview_desc).strip(" —")
    notes = mention.get("notes", "")
    if low_hits:
        notes = (notes + "; low-value/news keywords: " + ",".join(low_hits)).strip("; ")
    return score, confidence, contact_type, ",".join(role_hits), clean_text(title, 180), notes


def build_queries(root: str, brand: str, deep: bool = False) -> list[str]:
    queries = [
        f'"{root}" "t.me/"',
        f'"{brand}" "Telegram"',
        f'site:t.me "{brand}"',
    ]
    if deep:
        queries.extend([
            f'"{brand}" "founder" "Telegram"',
            f'"{brand}" "CEO" "t.me"',
            f'"{brand}" "partnership" "Telegram"',
            f'"{brand}" "business development" "Telegram"',
        ])
    return queries


def process_site(site: str, args) -> list[Candidate]:
    host = host_from_site(site)
    root = root_domain(host)
    brand = company_guess_from_root(root)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    mentions: list[dict] = []
    errors: list[str] = []

    # Follow contact-related links breadth-first and retain thin pages for browser review.
    seen_urls: set[str] = set()
    thin_pages: list[str] = []
    queue: list[str] = list(candidate_urls_for_site(site, args.site_pages))
    queue_seen: set[str] = set(queue)
    while queue and len(seen_urls) < args.site_pages:
        url = queue.pop(0)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        text, final_url, status, err = fetch_url(url, timeout=args.timeout)
        if not text:
            if err:
                errors.append(f"{url}: {err}")
            continue
        if is_thin_page(text):
            thin_pages.append(final_url)
        mentions.extend(extract_telegram_mentions(text, final_url, "official_site", site, root, brand))
        for link in extract_outlinks(text, final_url)[:80]:
            if len(seen_urls) + len(queue) >= args.site_pages * 3:
                break
            link_host = host_from_site(link)
            if root_domain(link_host) != root:
                continue
            if not any(k in link.lower() for k in CONTACT_KEYWORDS):
                continue
            if link in seen_urls or link in queue_seen:
                continue
            queue_seen.add(link)
            queue.append(link)

    # 2) Web search for public evidence.
    if args.search:
        for query in build_queries(root, brand, deep=args.deep_search)[: args.search_queries]:
            for result_url, snippet in ddg_search(query, limit=args.search_results, delay=args.search_delay):
                # Direct Telegram result: parse URL itself.
                if re.search(r"(?:t\.me|telegram\.me|telegram\.dog)/", result_url, flags=re.I):
                    mentions.extend(extract_telegram_mentions(result_url + " " + snippet, result_url, "web_search", site, root, brand))
                    continue
                # Otherwise fetch lightweight result pages that are likely to contain contact mentions.
                if any(k in result_url.lower() for k in [root.lower(), brand.lower(), "telegram", "contact", "about", "support"]):
                    page, final_url, status, err = fetch_url(result_url, timeout=args.timeout)
                    text = (snippet + "\n" + (page or ""))
                    mentions.extend(extract_telegram_mentions(text, final_url or result_url, "web_search", site, root, brand))

    # Deduplicate by normalized telegram_url.
    by_url: dict[str, dict] = {}
    for m in mentions:
        if not m.get("url"):
            continue
        key = m["url"].lower().rstrip("/")
        existing = by_url.get(key)
        if not existing or existing.get("source_type") != "official_site" and m.get("source_type") == "official_site":
            by_url[key] = m

    candidates: list[Candidate] = []
    for m in by_url.values():
        title = desc = perr = ""
        if m.get("handle"):
            title, desc, perr = telegram_preview(m["handle"])
        score, confidence, contact_type, role_kw, contact_title, notes = classify_and_score(m, root, brand, title, desc)
        if perr:
            notes = (notes + "; telegram preview: " + perr).strip("; ")
        if score < args.min_score:
            continue
        candidates.append(Candidate(
            input_site=site,
            root_domain=root,
            company_guess=brand,
            telegram_url=m["url"],
            telegram_handle=m.get("handle", ""),
            contact_type=contact_type,
            contact_name_or_title=contact_title,
            role_keywords=role_kw,
            confidence=confidence,
            score=score,
            source_type=m.get("source_type", ""),
            evidence_url=m.get("evidence_url", ""),
            evidence_text=m.get("evidence_text", ""),
            checked_at=now,
            notes=notes,
        ))

    confidence_order = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: (confidence_order.get(c.confidence, 9), -c.score, c.telegram_handle))
    candidates = candidates[: args.per_domain_limit]

    if not candidates and args.include_empty:
        notes = "; ".join(errors[:3]) if errors else "No public Telegram mention found by automated scan"
        if thin_pages:
            notes += f"; THIN_PAGE (possible JS-rendered SPA, headless-browser check recommended): {', '.join(thin_pages[:3])}"
        candidates.append(Candidate(
            input_site=site,
            root_domain=root,
            company_guess=brand,
            telegram_url="",
            telegram_handle="",
            contact_type="no_public_telegram_found",
            contact_name_or_title="",
            role_keywords="",
            confidence="none",
            score=0,
            source_type="official_site+web_search" if args.search else "official_site",
            evidence_url="",
            evidence_text="",
            checked_at=now,
            notes=notes,
        ))
    return candidates


def confidence_threshold_to_score(value: str) -> int:
    return {"none": 0, "low": 1, "medium": 55, "high": 80}.get(value, 1)


def read_domains(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [s for line in f if (s := normalize_input_site(line))]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Find public Telegram contacts for company outreach.")
    parser.add_argument("domains_file", help="Text file with one domain/site per line")
    parser.add_argument("--out", default="contacts.csv", help="Output CSV path")
    parser.add_argument("--search", action="store_true", help="Use DuckDuckGo HTML search in addition to official website pages")
    parser.add_argument("--deep-search", action="store_true", help="Add founder/CEO/partnership search queries")
    parser.add_argument("--include-empty", action="store_true", default=True, help="Write a row when no contact is found")
    parser.add_argument("--no-include-empty", dest="include_empty", action="store_false")
    parser.add_argument("--min-confidence", choices=["none", "low", "medium", "high"], default="low")
    parser.add_argument("--per-domain-limit", type=int, default=5)
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--site-pages", type=int, default=5, help="Maximum official-site pages to fetch per input")
    parser.add_argument("--search-queries", type=int, default=3, help="Maximum search queries per domain")
    parser.add_argument("--search-results", type=int, default=5, help="Maximum search results per query")
    parser.add_argument("--search-delay", type=float, default=0.7, help="Delay before each search request")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    args.min_score = confidence_threshold_to_score(args.min_confidence)

    domains = read_domains(args.domains_file)
    if not domains:
        print("No domains found", file=sys.stderr)
        return 2

    rows: list[Candidate] = []
    print(f"Scanning {len(domains)} sites; search={args.search}; workers={args.max_workers}", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as ex:
        futs = {ex.submit(process_site, site, args): site for site in domains}
        for i, fut in enumerate(as_completed(futs), start=1):
            site = futs[fut]
            try:
                result = fut.result()
            except Exception as e:
                now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                host = host_from_site(site)
                root = root_domain(host)
                result = [Candidate(site, root, company_guess_from_root(root), "", "", "scan_error", "", "", "none", 0, "", "", "", now, type(e).__name__ + ": " + str(e)[:200])]
            rows.extend(result)
            found = sum(1 for r in result if r.telegram_url)
            print(f"[{i}/{len(domains)}] {site}: {found} candidate(s)", file=sys.stderr)

    fieldnames = list(asdict(rows[0]).keys()) if rows else list(Candidate.__dataclass_fields__.keys())
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))

    found_rows = [r for r in rows if r.telegram_url]
    high = sum(1 for r in found_rows if r.confidence == "high")
    med = sum(1 for r in found_rows if r.confidence == "medium")
    low = sum(1 for r in found_rows if r.confidence == "low")
    print(f"Wrote {len(rows)} rows to {args.out}; contacts: {len(found_rows)} (high={high}, medium={med}, low={low})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
