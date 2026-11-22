"""Offline-first message history ingestion and monitoring primitives."""

from .telegram_export import TelegramExport, TelegramMessage, load_telegram_export
from .store import MessageStore, StoredMessage
from .signals import SignalCandidate, SignalProfile, find_signals, score_message
from .reports import HistoryReport, build_history_report, signal_report, weekly_activity
from .semantic import LocalSemanticScorer, SemanticProfile, sentence_transformer_embedder
from .jev import JevClient
from .ab_test import ProfileVariant, run_ab_test
from .hypotheses import default_ab_variants

__all__ = [
    "MessageStore",
    "StoredMessage",
    "TelegramExport",
    "TelegramMessage",
    "load_telegram_export",
    "HistoryReport",
    "SignalCandidate",
    "SignalProfile",
    "build_history_report",
    "find_signals",
    "score_message",
    "signal_report",
    "weekly_activity",
    "JevClient",
    "LocalSemanticScorer",
    "SemanticProfile",
    "sentence_transformer_embedder",
    "ProfileVariant",
    "run_ab_test",
    "default_ab_variants",
]
