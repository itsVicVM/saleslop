"""Optional local semantic scoring, kept separate from the baseline rules."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Protocol


class TextEmbedder(Protocol):
    def encode(self, texts: list[str]) -> Any: ...


@dataclass(frozen=True)
class SemanticProfile:
    positive_examples: tuple[str, ...] = ()
    negative_examples: tuple[str, ...] = ()


def _cosine(left: Iterable[float], right: Iterable[float]) -> float:
    a, b = list(left), list(right)
    denominator = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / denominator if denominator else 0.0


class LocalSemanticScorer:
    """Score text against positive/negative examples using an injected embedder.

    The embedder is injected so production can use BGE-M3, a smaller multilingual
    model, or a test double without coupling the kit to a model runtime.
    """

    def __init__(self, embedder: TextEmbedder, profile: SemanticProfile) -> None:
        self.embedder = embedder
        self.profile = profile
        examples = list(profile.positive_examples) + list(profile.negative_examples)
        vectors = embedder.encode(examples) if examples else []
        self._positive = list(vectors[: len(profile.positive_examples)])
        self._negative = list(vectors[len(profile.positive_examples) :])

    def score(self, text: str) -> float:
        vector = self.embedder.encode([text])[0]
        positive = max((_cosine(vector, item) for item in self._positive), default=0.0)
        negative = max((_cosine(vector, item) for item in self._negative), default=0.0)
        return max(-1.0, min(1.0, positive - negative))


def sentence_transformer_embedder(model_name: str = "BAAI/bge-m3") -> TextEmbedder:
    """Create a local embedder only when sentence-transformers is installed."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - depends on optional environment
        raise RuntimeError(
            "Optional semantic support requires sentence-transformers; "
            "install it separately before enabling local embeddings"
        ) from exc
    return SentenceTransformer(model_name)
