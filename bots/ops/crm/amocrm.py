"""Optional AmoCRM adapter (API v4). Holds no client data: pipelines and their statuses come from the
client's pack (`crm.amocrm.pipelines`), base URL and long-lived token from env (KIT_CRM_BASE_URL, KIT_CRM_TOKEN)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import CRMAdapter


class AmoCRM(CRMAdapter):
    name = "amocrm"

    def __init__(self, base_url: str | None, token: str | None, settings: dict):
        if not base_url or not token:
            raise ValueError("amocrm adapter needs KIT_CRM_BASE_URL and KIT_CRM_TOKEN")
        self.base_url = base_url.rstrip("/")
        self.token = token
        # {key: {"id": int, "label": str, "statuses": [{"id", "name", "sort", "type"}]}}; type 1 = entry status
        self.pipelines = settings.get("pipelines") or {}

    def _call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 204:
                    return {}
                txt = resp.read().decode("utf-8")
                return json.loads(txt) if txt.strip() else {}
        except urllib.error.HTTPError as e:
            print(f"[ops/amocrm] {method} {path.split('?')[0]} failed: {e.code}")
            return None
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"[ops/amocrm] {method} {path.split('?')[0]} error: {e}")
            return None

    def search_leads(self, query: str) -> list:
        res = self._call("GET", f"/api/v4/leads?query={urllib.parse.quote(query)}&limit=20")
        return [{"id": x.get("id"), "name": x.get("name")} for x in ((res or {}).get("_embedded") or {}).get("leads", [])]

    def add_note(self, lead_id, text: str) -> bool:
        return self._call("POST", f"/api/v4/leads/{lead_id}/notes", [{"note_type": "common", "params": {"text": text}}]) is not None

    def advance_from_unsorted(self, lead_id) -> bool:
        """Only for pipelines listed in the pack: never guess stages of an unknown funnel."""
        lead = self._call("GET", f"/api/v4/leads/{lead_id}")
        if not lead:
            return False
        pipeline = next((p for p in self.pipelines.values() if p.get("id") == lead.get("pipeline_id")), None)
        if not pipeline:
            return False
        statuses = pipeline.get("statuses") or []
        current = next((s for s in statuses if s.get("id") == lead.get("status_id")), None)
        if not current or current.get("type") != 1:
            return False
        nxt = min((s for s in statuses if s.get("sort", 0) > current.get("sort", 0)), key=lambda s: s["sort"], default=None)
        if not nxt:
            return False
        return self._call("PATCH", f"/api/v4/leads/{lead_id}", {"pipeline_id": pipeline["id"], "status_id": nxt["id"]}) is not None

    def _has_open_task(self, lead_id, text: str) -> bool:
        """Ongoing chatter would otherwise pile up identical open tasks on one lead."""
        res = self._call("GET", f"/api/v4/tasks?filter[entity_id]={lead_id}&filter[entity_type]=leads&filter[is_completed]=0&limit=250")
        return any((t.get("text") or "") == text for t in ((res or {}).get("_embedded") or {}).get("tasks", []))

    def create_task(self, lead_id, text: str, due_hours: int = 24) -> bool:
        if self._has_open_task(lead_id, text):
            return False
        lead = self._call("GET", f"/api/v4/leads/{lead_id}") or {}
        task = {"text": text, "complete_till": int(time.time()) + due_hours * 3600, "entity_id": lead_id, "entity_type": "leads"}
        if lead.get("responsible_user_id"):
            task["responsible_user_id"] = lead["responsible_user_id"]  # the lead's own owner, not a fixed person
        return self._call("POST", "/api/v4/tasks", [task]) is not None
