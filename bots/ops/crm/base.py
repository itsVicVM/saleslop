"""CRM adapter interface. The engine talks only to this; `crm.type` in the pack picks the implementation."""
from __future__ import annotations


class CRMAdapter:
    name = "base"

    def search_leads(self, query: str) -> list:
        """Loose full-text search. Results are candidates for the operator to confirm, never auto-linked."""
        return []

    def add_note(self, lead_id, text: str) -> bool:
        return False

    def advance_from_unsorted(self, lead_id) -> bool:
        """Move a lead out of the pipeline's entry ("unsorted") status into the next stage, once."""
        return False

    def create_task(self, lead_id, text: str, due_hours: int = 24) -> bool:
        return False
