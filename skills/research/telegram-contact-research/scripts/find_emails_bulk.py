#!/usr/bin/env python3
"""Look up email contacts for a list of domains. Try Apollo for named contacts,
then use Findymail when Apollo is unavailable or returns no confident match.

Usage:
    python3 find_emails_bulk.py domains.txt --out emails.json [--roles support,sales,partnerships]

`domains.txt`: one bare domain per line (no scheme), same file format
`telegram_contact_finder.py` accepts (a scheme-prefixed URL is also accepted
and normalized). Set FINDYMAIL_API_KEY and optionally APOLLO_API_KEY in the
environment. The Findymail HTTP requests include a browser User-Agent.
"""
import argparse
from collections import Counter
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import apollo_email_finder
import _budget  # shared daily API budget (limits.budgets.findymail_daily / apollo_daily)

API_URL = "https://app.findymail.com/api/search/domain"
REOON_URL = "https://emailverifier.reoon.com/api/v1/verify"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def normalize_domain(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    raw = re.sub(r"^https?://", "", raw)
    raw = raw.split("/")[0]
    return raw.lower()


def search_domain(domain: str, roles: list[str], api_key: str) -> dict:
    _budget.reserve("findymail")   # raises BudgetExhausted; booked before the request, never refunded
    body = json.dumps({"domain": domain, "roles": roles}).encode()
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        return {"domain": domain, "contacts": data.get("contacts", [])}
    except urllib.error.HTTPError as e:
        return {"domain": domain, "error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
    except Exception as e:
        return {"domain": domain, "error": str(e)}


def verify_reoon(email: str, api_key: str) -> dict:
    """Return the mailbox status, or unverified with a reason if Reoon cannot verify it."""
    if not api_key:
        return {"status": "unverified", "reason": "REOON_API_KEY not set"}
    params = urllib.parse.urlencode({"email": email, "mode": "quick", "key": api_key})
    req = urllib.request.Request(f"{REOON_URL}?{params}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        return {"status": data.get("status", "unknown"), "is_safe_to_send": data.get("is_safe_to_send")}
    except urllib.error.HTTPError as e:
        try:
            reason = json.loads(e.read().decode()).get("reason", f"HTTP {e.code}")
        except Exception:
            reason = f"HTTP {e.code}"
        return {"status": "unverified", "reason": reason}
    except Exception as e:
        return {"status": "unverified", "reason": str(e)}


def verify_findymail(email: str, api_key: str) -> dict:
    """Fallback verifier when Reoon is unreachable (try Findymail's own /api/verify instead of leaving the email
    unverified). Same Bearer token as the domain-search call, but costs a
    separate credit per call. Returns {"status": "valid"|"invalid"}, or
    {"status": "unverified", "reason": ...} if this call itself fails too -
    in that case there is genuinely no verification for this email."""
    if not api_key:
        return {"status": "unverified", "reason": "FINDYMAIL_API_KEY not set"}
    try:
        _budget.reserve("findymail")
    except _budget.BudgetExhausted as exc:
        return {"status": "unverified", "reason": str(exc)}
    body = json.dumps({"email": email}).encode()
    req = urllib.request.Request(
        "https://app.findymail.com/api/verify", data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        verified = data.get("verified")
        if not isinstance(verified, bool):
            return {"status": "unverified", "reason": "verifier returned no boolean verdict"}
        return {"status": "valid" if verified else "invalid", "provider": data.get("provider")}
    except urllib.error.HTTPError as e:
        try:
            reason = json.loads(e.read().decode()).get("reason", f"HTTP {e.code}")
        except Exception:
            reason = f"HTTP {e.code}"
        return {"status": "unverified", "reason": reason}
    except Exception as e:
        return {"status": "unverified", "reason": str(e)}


def verify_mx(domain: str) -> dict:
    """Check whether the domain accepts mail when mailbox verification is unavailable.

    Return mx_only for domains with an MX record. The mailbox itself still needs review.
    """
    try:
        out = subprocess.run(["dig", "+short", "MX", domain], capture_output=True, text=True, timeout=8)
        has_mx = bool(out.stdout.strip())
        return {"status": "mx_only", "has_mx": has_mx} if has_mx else {"status": "unverified", "reason": "no MX records"}
    except Exception as e:
        return {"status": "unverified", "reason": f"mx lookup failed: {e}"}


def verify_email(email: str, reoon_key: str, findymail_key: str) -> dict:
    """Reoon first; if it can't answer (down/no credits), fall back to
    Findymail's own verifier so a real answer beats no answer whenever
    possible. If BOTH paid verifiers fail, fall back once more to a free MX
    lookup (see verify_mx). Keep "mx_only" in the research output for review;
    only "valid" enters the first-touch address column."""
    reoon = verify_reoon(email, reoon_key)
    if reoon["status"] != "unverified":
        return {"source": "reoon", **reoon}
    fm = verify_findymail(email, findymail_key)
    if fm["status"] != "unverified":
        return {"source": "findymail", **fm}
    domain = email.split("@", 1)[-1].strip().lower()
    mx = verify_mx(domain) if domain else {"status": "unverified", "reason": "no domain in email"}
    if mx["status"] == "mx_only":
        return {"source": "mx", **mx, "reoon_reason": reoon.get("reason"), "findymail_reason": fm.get("reason")}
    return {"source": "none", "status": "unverified", "reoon_reason": reoon.get("reason"), "findymail_reason": fm.get("reason")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("domains_file")
    ap.add_argument("--out", default="emails.json")
    ap.add_argument("--roles", default="support,sales,partnerships")
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--no-verify", action="store_true", help="Look up contacts without verifying their mailboxes")
    ap.add_argument("--no-apollo", action="store_true", help="Use Findymail only")
    ap.add_argument("--apollo-max-candidates", type=int, default=2, help="Max people-search candidates to pay for enriching per domain")
    args = ap.parse_args()

    # Brand pack search.email_providers (via KIT_EMAIL_PROVIDERS): a provider left out
    # of the list is skipped even if its key is set. Unset = all providers allowed.
    providers = {p.strip().lower() for p in os.environ.get("KIT_EMAIL_PROVIDERS", "").split(",") if p.strip()}
    findymail_key = os.environ.get("FINDYMAIL_API_KEY", "").strip()
    if providers and "findymail" not in providers:
        findymail_key = ""
    apollo_key = "" if args.no_apollo else os.environ.get("APOLLO_API_KEY", "").strip()
    if providers and "apollo" not in providers:
        apollo_key = ""
    if not findymail_key and not apollo_key:
        print(json.dumps({"ok": False, "error": "no usable email provider: set APOLLO_API_KEY/FINDYMAIL_API_KEY "
                          "for a provider listed in search.email_providers (KIT_EMAIL_PROVIDERS)"}))
        return 1
    reoon_key = os.environ.get("REOON_API_KEY", "").strip()

    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    with open(args.domains_file, encoding="utf-8") as f:
        domains = [normalize_domain(line) for line in f if normalize_domain(line)]

    results = {}
    found = 0
    apollo_found = 0
    findymail_found = 0
    statuses = Counter()
    paid_verifier_failures = 0  # both Reoon and Findymail unreachable for this email (any reason)
    budget_stopped: list[str] = []   # providers whose daily budget ran out during this run
    budget_reason = ""
    stopped_at = None
    for i, domain in enumerate(domains, 1):
        r = None
        if apollo_key:
            try:
                r = apollo_email_finder.find_contact(domain, apollo_key, max_candidates=args.apollo_max_candidates)
            except _budget.BudgetExhausted as exc:
                print(f"STOP apollo: {exc}", file=sys.stderr)
                budget_stopped.append("apollo")
                budget_reason = str(exc)
                apollo_key = ""
                r = {}
            if r.get("contacts"):
                apollo_found += 1
            else:
                r = None  # fall through to Findymail below
        if r is None:
            if not findymail_key:
                if budget_stopped:
                    stopped_at = i   # no provider left with budget: stop instead of marking the rest as empty
                    break
                r = {"domain": domain, "contacts": [], "error": "no Findymail fallback (FINDYMAIL_API_KEY not set)"}
            else:
                try:
                    r = search_domain(domain, roles, findymail_key)
                except _budget.BudgetExhausted as exc:
                    print(f"STOP findymail: {exc}", file=sys.stderr)
                    budget_stopped.append("findymail")
                    budget_reason = str(exc)
                    findymail_key = ""
                    if not apollo_key:
                        stopped_at = i
                        break
                    r = {"domain": domain, "contacts": [], "error": str(exc)}
                if r.get("contacts"):
                    findymail_found += 1

        if r.get("contacts") and not args.no_verify:
            top_email = r["contacts"][0]["email"]
            verification = verify_email(top_email, reoon_key, findymail_key)
            r["reoon"] = verification  # Keep the existing output key for downstream readers.
            status = verification["status"]
            statuses[status] += 1
            if status == "mx_only" or (status == "unverified" and "reoon_reason" in verification):
                paid_verifier_failures += 1
            time.sleep(args.delay)
        results[domain] = r
        if r.get("contacts"):
            found += 1
        v = r.get("reoon", {})
        status_note = f"{v.get('source', '?')}:{v.get('status')}" if v else ("skipped" if r.get("contacts") else "no-email")
        src_note = r.get("source", "findymail")
        print(f"[{i}/{len(domains)}] {domain} -> {'found ('+src_note+')' if r.get('contacts') else r.get('error', 'none')} (verify: {status_note})", file=sys.stderr)
        time.sleep(args.delay)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # Report verifier outages alongside the per-address results.
    checked = statuses.total()
    if checked and paid_verifier_failures >= checked * 0.8:
        print(
            f"Email verifiers returned no result for {paid_verifier_failures} of {checked} checks. "
            f"{statuses['mx_only']} addresses had only a domain MX match. "
            f"{statuses['unverified']} addresses could not be verified.",
            file=sys.stderr,
        )

    summary = {
        "ok": True, "total": len(domains), "found": found,
        "apollo_found": apollo_found, "findymail_found": findymail_found,
        "reoon_verified": statuses["valid"], "mx_only": statuses["mx_only"],
        "reoon_unverified": statuses["unverified"], "verification_statuses": dict(statuses),
        "verifier_outage": paid_verifier_failures >= checked * 0.8 if checked else False, "out": args.out,
    }
    if budget_stopped:
        summary["budget_exhausted"] = budget_stopped
        summary["budget_reason"] = budget_reason
    if stopped_at is not None:
        # partial results are written above; the remaining domains were not looked up at all
        summary.update(ok=False, stopped="budget", unprocessed=len(domains) - stopped_at + 1)
        print(json.dumps(summary))
        return _budget.BUDGET_EXIT
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
