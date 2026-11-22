"""Transparent first-pass signal scoring for message history."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable

from .store import StoredMessage


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _matches(term: str, text: str) -> bool:
    term = _normalize(term)
    if not term:
        return False
    if len(term) <= 3 and term.isalnum():
        return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None
    return term in text


@dataclass(frozen=True)
class SignalProfile:
    name: str
    positive_terms: tuple[str, ...] = ()
    intent_terms: tuple[str, ...] = ()
    negative_terms: tuple[str, ...] = ()
    min_score: int = 2


@dataclass(frozen=True)
class SignalCandidate:
    message: StoredMessage
    score: int
    reasons: tuple[str, ...] = field(default_factory=tuple)
    semantic_score: float = 0.0


def score_message(message: StoredMessage, profile: SignalProfile, *, semantic_scorer=None) -> SignalCandidate:
    text = _normalize(message.text)
    score = 0
    reasons: list[str] = []

    positive = [term for term in profile.positive_terms if _matches(term, text)]
    intents = [term for term in profile.intent_terms if _matches(term, text)]
    negatives = [term for term in profile.negative_terms if _matches(term, text)]

    if positive:
        score += 1
        reasons.append("profile match: " + ", ".join(positive[:3]))
    if intents:
        score += 2
        reasons.append("intent match: " + ", ".join(intents[:3]))
    if negatives:
        score -= 3
        reasons.append("negative match: " + ", ".join(negatives[:3]))
    if not message.text.strip():
        score -= 10
        reasons.append("empty text")

    semantic_score = float(semantic_scorer.score(message.text)) if semantic_scorer else 0.0
    if semantic_scorer and semantic_score >= 0.25:
        score += 1
        reasons.append(f"semantic match: {semantic_score:.2f}")
    elif semantic_scorer and semantic_score <= -0.25:
        score -= 1
        reasons.append(f"semantic mismatch: {semantic_score:.2f}")
    return SignalCandidate(
        message=message,
        score=score,
        reasons=tuple(reasons),
        semantic_score=semantic_score,
    )


def find_signals(
    messages: Iterable[StoredMessage],
    profile: SignalProfile,
    *,
    limit: int = 50,
    include_service: bool = False,
    semantic_scorer=None,
) -> list[SignalCandidate]:
    candidates = []
    for message in messages:
        if not include_service and message.kind != "message":
            continue
        candidate = score_message(message, profile, semantic_scorer=semantic_scorer)
        if candidate.score >= profile.min_score:
            candidates.append(candidate)
    candidates.sort(key=lambda item: (item.score, item.message.date or "", item.message.message_id), reverse=True)
    return candidates[: max(1, min(limit, 10000))]
