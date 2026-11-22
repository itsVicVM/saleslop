"""Reusable, generic signal hypotheses for the first offline benchmark."""

from .ab_test import ProfileVariant
from .signals import SignalProfile


def default_ab_variants() -> list[ProfileVariant]:
    return [
        ProfileVariant(
            "service_strict_v1", "service-seeking v1",
            SignalProfile("service_strict_v1", ("lead generation", "leadgen", "outbound", "business development"), ("looking for", "need", "recommend", "budget", "seeking"), ("hiring", "vacancy", "job", "airdrop", "giveaway"), 3),
        ),
        ProfileVariant(
            "service_strict_v2", "service-seeking with hiring exclusions",
            SignalProfile("service_strict_v2", ("lead generation", "leadgen", "outbound", "business development"), ("looking for", "need", "recommend", "budget", "seeking"), ("hiring", "vacancy", "job", "looking for candidates", "positions", "sales development representative", "opportunities", "resume", "cv", "airdrop", "giveaway"), 3),
        ),
        ProfileVariant(
            "partner_strict_v1", "partnership v1",
            SignalProfile("partner_strict_v1", ("partnership", "integration", "api"), ("looking for partner", "seeking partner", "integrate", "collaborate"), ("hiring", "vacancy", "job", "airdrop", "giveaway"), 3),
        ),
        ProfileVariant(
            "partner_strict_v2", "partnership with announcement exclusions",
            SignalProfile("partner_strict_v2", ("partnership", "integration", "api"), ("looking for partner", "seeking partner", "need a partner", "open to partnerships", "integrate with", "collaborate with"), ("hiring", "vacancy", "job", "already integrated", "integrates with", "announcing", "announcement", "cross marketing", "airdrop", "giveaway"), 3),
        ),
        ProfileVariant(
            "tool_strict_v1", "tool procurement v1",
            SignalProfile("tool_strict_v1", ("crm", "outreach", "sales tool"), ("looking for", "need", "budget", "alternative", "switching"), ("hiring", "vacancy", "job", "course", "free"), 3),
        ),
        ProfileVariant(
            "tool_strict_v2", "tool procurement with request phrases",
            SignalProfile("tool_strict_v2", ("crm", "outreach", "sales tool"), ("what crm", "looking for a crm", "need a crm", "alternative to", "switching from", "what tools"), ("hiring", "vacancy", "job", "course", "free", "selling", "we built"), 3),
        ),
    ]
