"""Parse Telegram Desktop JSON exports without importing raw data into the kit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class TelegramMessage:
    chat_id: str
    chat_name: str
    message_id: int
    kind: str
    date: str | None
    edited: str | None
    sender: str | None
    sender_id: str | None
    text: str
    reply_to_message_id: int | None
    raw_text_entities: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class TelegramExport:
    chat_id: str
    chat_name: str
    chat_type: str
    messages: tuple[TelegramMessage, ...]

    @property
    def ordinary_messages(self) -> tuple[TelegramMessage, ...]:
        return tuple(message for message in self.messages if message.kind == "message")


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_flatten_text(item) for item in value)
    if isinstance(value, dict):
        return _flatten_text(value.get("text", ""))
    return ""


def _parse_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_message(payload: dict[str, Any], *, chat_id: str, chat_name: str) -> TelegramMessage:
    entities = payload.get("text_entities") or []
    safe_entities = tuple(item for item in entities if isinstance(item, dict))
    return TelegramMessage(
        chat_id=chat_id,
        chat_name=chat_name,
        message_id=int(payload["id"]),
        kind=str(payload.get("type") or "unknown"),
        date=payload.get("date"),
        edited=payload.get("edited"),
        sender=payload.get("from") or payload.get("actor"),
        sender_id=payload.get("from_id") or payload.get("actor_id"),
        text=_flatten_text(payload.get("text", "")),
        reply_to_message_id=_parse_int(payload.get("reply_to_message_id")),
        raw_text_entities=safe_entities,
    )


def load_telegram_export(path: str | Path) -> TelegramExport:
    """Load a Telegram Desktop JSON export into normalized immutable records.

    The caller owns the input file. This function does not copy, rewrite, or persist
    the export and intentionally keeps service messages so callers can audit counts.
    """
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise ValueError("Not a Telegram Desktop JSON export: messages array is missing")

    chat_id = str(payload.get("id", ""))
    chat_name = str(payload.get("name", ""))
    messages = tuple(
        _parse_message(item, chat_id=chat_id, chat_name=chat_name)
        for item in payload["messages"]
        if isinstance(item, dict) and item.get("id") is not None
    )
    return TelegramExport(
        chat_id=chat_id,
        chat_name=chat_name,
        chat_type=str(payload.get("type", "")),
        messages=messages,
    )


def iter_messages(export: TelegramExport, *, ordinary_only: bool = True) -> Iterator[TelegramMessage]:
    """Iterate messages in export order, optionally excluding service events."""
    yield from (export.ordinary_messages if ordinary_only else export.messages)
