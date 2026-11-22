"""Shared URL -> normalized-domain helper (used by both workers so the
'have we already seen this' notion of identity is identical in discovery
yield-tracking and in validation dedup)."""
from urllib.parse import urlsplit


def normalize_domain(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower()
    except Exception:
        return url.lower()
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host
