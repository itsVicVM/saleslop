"""Deal lookup and operator-approved notes for HubSpot and Bitrix24."""
import json
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .base import CRMAdapter


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_json(url, body, headers=None):
    req = Request(url, data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with build_opener(NoRedirect()).open(req, timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        print(f"[ops/crm] request failed (HTTP {exc.code})", file=sys.stderr)
    except (URLError, OSError, ValueError):
        print("[ops/crm] request failed", file=sys.stderr)
    return {}


class HubSpot(CRMAdapter):
    name = "hubspot"

    def __init__(self, token, field=None):
        if not token:
            raise ValueError("hubspot needs KIT_CRM_TOKEN")
        self.headers = {"Authorization": "Bearer " + token}
        self.field = field

    def search_leads(self, query):
        body = {"properties": ["dealname"], "limit": 5}
        if self.field:
            body["filterGroups"] = [{"filters": [{"propertyName": self.field, "operator": "EQ", "value": query}]}]
        else:
            body["query"] = query
        result = post_json("https://api.hubapi.com/crm/v3/objects/deals/search", body, self.headers)
        return [{"id": d["id"], "name": d["properties"]["dealname"]} for d in result.get("results", [])
                if d.get("id") and (d.get("properties") or {}).get("dealname")]

    def add_note(self, lead_id, text):
        body = {"properties": {"hs_timestamp": datetime.now(timezone.utc).isoformat(), "hs_note_body": text},
                "associations": [{"to": {"id": str(lead_id)}, "types": [
                    {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}]}]}
        return bool(post_json("https://api.hubapi.com/crm/v3/objects/notes", body, self.headers).get("id"))


class Bitrix24(CRMAdapter):
    name = "bitrix24"

    def __init__(self, webhook, field=None):
        parts = urlsplit(webhook or "")
        if parts.scheme != "https" or not parts.hostname or not parts.path.startswith("/rest/") or parts.query or parts.fragment:
            raise ValueError("bitrix24 needs an HTTPS incoming webhook URL in KIT_CRM_WEBHOOK")
        self.base = webhook.rstrip("/") + "/"
        self.field = field

    def search_leads(self, query):
        filters = {"=" + self.field: query} if self.field else {"%TITLE": query}
        result = post_json(self.base + "crm.deal.list.json", {"filter": filters, "select": ["ID", "TITLE"]})
        return [{"id": d["ID"], "name": d["TITLE"]} for d in result.get("result", [])[:5]
                if d.get("ID") and d.get("TITLE")]

    def add_note(self, lead_id, text):
        result = post_json(self.base + "crm.timeline.comment.add.json", {"fields": {
            "ENTITY_TYPE": "deal", "ENTITY_ID": lead_id, "COMMENT": text}})
        return bool(result.get("result")) and not result.get("error")
