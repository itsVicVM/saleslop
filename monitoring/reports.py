"""Reports over stored chat history and scored signal candidates."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .signals import SignalCandidate
from .store import StoredMessage


@dataclass(frozen=True)
class HistoryReport:
    total_messages: int
    ordinary_messages: int
    service_messages: int
    first_date: str | None
    last_date: str | None
    messages_by_day: dict[str, int]


def build_history_report(messages: Iterable[StoredMessage]) -> HistoryReport:
    items = list(messages)
    dated = sorted(item.date for item in items if item.date)
    per_day = Counter(item.date[:10] for item in items if item.date)
    return HistoryReport(
        total_messages=len(items),
        ordinary_messages=sum(item.kind == "message" for item in items),
        service_messages=sum(item.kind != "message" for item in items),
        first_date=dated[0] if dated else None,
        last_date=dated[-1] if dated else None,
        messages_by_day=dict(sorted(per_day.items())),
    )


def weekly_activity(messages: Iterable[StoredMessage], *, end: date | None = None) -> dict[str, int]:
    """Return ordinary-message counts for the seven-day window ending at ``end``."""
    end = end or date.today()
    start = end.toordinal() - 6
    counts = Counter()
    for item in messages:
        if item.kind != "message" or not item.date:
            continue
        try:
            day = date.fromisoformat(item.date[:10])
        except ValueError:
            continue
        if start <= day.toordinal() <= end.toordinal():
            counts[day.isoformat()] += 1
    return {day.isoformat(): counts.get(day.isoformat(), 0) for day in (date.fromordinal(start + i) for i in range(7))}


def signal_report(candidates: Iterable[SignalCandidate]) -> dict[str, object]:
    items = list(candidates)
    return {
        "signals": len(items),
        "high_priority": sum(item.score >= 3 for item in items),
        "top_score": max((item.score for item in items), default=0),
        "items": [
            {
                "chat_id": item.message.chat_id,
                "message_id": item.message.message_id,
                "date": item.message.date,
                "score": item.score,
                "reasons": list(item.reasons),
            }
            for item in items
        ],
    }
