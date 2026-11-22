#!/usr/bin/env python3
"""Find named business contacts by domain and role through the Apollo API.

People Search returns candidate IDs. Enrichment requests contact details for
a selected ID. The caller checks match confidence before accepting a result.
Supply an API key with access to both endpoints and check the credit terms
for your account. See https://docs.apollo.io/reference/authentication.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import _budget  # shared daily API budget (limits.budgets.apollo_daily)

SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"

DEFAULT_TITLES = [
    "Founder", "Co-Founder", "CEO", "Owner",
    "Head of Partnerships", "Business Development Manager", "Business Development",
    "Head of Marketing", "Marketing Manager",
]


def _post(url: str, params: list[tuple[str, str]], api_key: str, timeout: int = 20) -> dict | None:
    qs = urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        f"{url}?{qs}", data=b"", method="POST",
        headers={"x-api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {}
        return {"_error": f"HTTP {e.code}", "_detail": body}
    except Exception as e:
        return {"_error": str(e)}


def search_people(domain: str, api_key: str, titles: list[str] | None = None, per_page: int = 10) -> list[dict]:
    params: list[tuple[str, str]] = [
        ("q_organization_domains_list[]", domain),
        ("per_page", str(per_page)),
        ("page", "1"),
    ]
    for t in (titles or DEFAULT_TITLES):
        params.append(("person_titles[]", t))
    data = _post(SEARCH_URL, params, api_key)
    if not data or data.get("_error"):
        return []
    return data.get("people", []) or []


def enrich_person(person_id: str, api_key: str) -> dict | None:
    # Reserve one lookup before each enrichment request.
    _budget.reserve("apollo")
    params = [("id", person_id), ("reveal_personal_emails", "true")]
    data = _post(MATCH_URL, params, api_key)
    if not data or data.get("_error"):
        return None
    return data


def find_contact(domain: str, api_key: str, titles: list[str] | None = None, max_candidates: int = 2) -> dict:
    """Return contacts in the bulk finder's format for high- or medium-confidence matches."""
    try:
        people = search_people(domain, api_key, titles=titles)
    except Exception as e:
        return {"domain": domain, "contacts": [], "source": "apollo", "error": str(e)}

    tried = 0
    for p in people:
        if tried >= max_candidates:
            break
        pid = p.get("id")
        if not pid:
            continue
        tried += 1
        enriched = enrich_person(pid, api_key)
        if not enriched:
            continue
        person = enriched.get("person") or {}
        email = person.get("email")
        conf = person.get("match_confidence")
        if email and conf in ("high", "medium") and "email_not_unlocked" not in email:
            return {
                "domain": domain,
                "contacts": [{
                    "email": email,
                    "name": person.get("name"),
                    "title": person.get("title"),
                    "linkedin_url": person.get("linkedin_url"),
                    "match_confidence": conf,
                }],
                "source": "apollo",
            }
    return {"domain": domain, "contacts": [], "source": "apollo", "candidates_checked": tried}
