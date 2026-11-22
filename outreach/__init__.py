"""Scripted email outreach for the kit.

Reads its configuration from KIT_OUTREACH_* env vars (see README.md / PACK_FIELDS.md),
never from the brand pack directly. Dry-run is the default; real sending needs --send.
Stdlib only, Python 3.11+.
"""

__all__ = ["config", "leads", "templates", "journal", "engine", "transport", "cli"]
