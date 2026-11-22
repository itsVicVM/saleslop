"""Offline A/B comparison of signal profiles without persisting message text."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from typing import Iterable

from .signals import SignalCandidate, SignalProfile, find_signals
from .store import StoredMessage


@dataclass(frozen=True)
class ProfileVariant:
    name: str
    hypothesis: str
    profile: SignalProfile


def run_ab_test(
    messages: Iterable[StoredMessage],
    variants: Iterable[ProfileVariant],
    *,
    limit: int = 100,
) -> dict[str, object]:
    items = list(messages)
    results: dict[str, list[SignalCandidate]] = {}
    variant_rows: list[dict[str, object]] = []
    for variant in variants:
        candidates = find_signals(items, variant.profile, limit=limit)
        results[variant.name] = candidates
        scores = [candidate.score for candidate in candidates]
        variant_rows.append(
            {
                "name": variant.name,
                "hypothesis": variant.hypothesis,
                "min_score": variant.profile.min_score,
                "positive_terms": list(variant.profile.positive_terms),
                "intent_terms": list(variant.profile.intent_terms),
                "negative_terms": list(variant.profile.negative_terms),
                "signals": len(candidates),
                "high_priority": sum(score >= 3 for score in scores),
                "average_score": round(sum(scores) / len(scores), 3) if scores else 0.0,
                "score_histogram": dict(sorted(Counter(scores).items())),
                "top_message_ids": [candidate.message.message_id for candidate in candidates[:10]],
            }
        )

    overlaps = []
    for left, right in combinations(results, 2):
        left_ids = {candidate.message.message_id for candidate in results[left]}
        right_ids = {candidate.message.message_id for candidate in results[right]}
        union = left_ids | right_ids
        overlaps.append(
            {
                "left": left,
                "right": right,
                "intersection": len(left_ids & right_ids),
                "jaccard": round(len(left_ids & right_ids) / len(union), 3) if union else 1.0,
            }
        )
    return {
        "ordinary_messages": sum(item.kind == "message" for item in items),
        "variants": variant_rows,
        "overlaps": overlaps,
        "review_message_ids": {
            name: [candidate.message.message_id for candidate in candidates]
            for name, candidates in results.items()
        },
    }
