"""CRM adapters: `none` (default) and `amocrm` (optional)."""
from __future__ import annotations

import os

from .base import CRMAdapter
from .none import NoneCRM

KNOWN = ("none", "amocrm", "hubspot", "bitrix24")


def get_adapter(crm_cfg: dict | None, env=None) -> CRMAdapter:
    crm_cfg = crm_cfg or {}
    kind = crm_cfg.get("type", "none")
    env = os.environ if env is None else env
    field = (crm_cfg.get(kind) or {}).get("telegram_field")
    if kind == "hubspot":
        from .deals import HubSpot
        return HubSpot(env.get("KIT_CRM_TOKEN"), field)
    if kind == "bitrix24":
        from .deals import Bitrix24
        return Bitrix24(env.get("KIT_CRM_WEBHOOK"), field)
    if kind == "none":
        return NoneCRM()
    if kind == "amocrm":
        from .amocrm import AmoCRM
        env = os.environ if env is None else env
        return AmoCRM(base_url=env.get("KIT_CRM_BASE_URL"), token=env.get("KIT_CRM_TOKEN"),
                      settings=crm_cfg.get("amocrm") or {})
    raise ValueError(f"unknown crm.type {kind!r} (known: {KNOWN})")
