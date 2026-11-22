"""Default adapter: no CRM. Every call is a no-op, so the bot runs without any CRM account."""
from .base import CRMAdapter


class NoneCRM(CRMAdapter):
    name = "none"
