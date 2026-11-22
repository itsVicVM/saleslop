"""Small SQLite-backed store for normalized Telegram message history."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from .telegram_export import TelegramMessage


@dataclass(frozen=True)
class StoredMessage:
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


class MessageStore:
    """Persist messages locally and make them searchable without external services."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "MessageStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                chat_id TEXT NOT NULL,
                chat_name TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                date TEXT,
                edited TEXT,
                sender TEXT,
                sender_id TEXT,
                text TEXT NOT NULL,
                reply_to_message_id INTEGER,
                text_entities_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat_date
                ON messages(chat_id, date);
            CREATE INDEX IF NOT EXISTS idx_messages_sender
                ON messages(chat_id, sender_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                chat_id UNINDEXED,
                message_id UNINDEXED,
                text
            );
            """
        )
        self.connection.commit()

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> StoredMessage:
        return StoredMessage(
            chat_id=row["chat_id"],
            chat_name=row["chat_name"],
            message_id=row["message_id"],
            kind=row["kind"],
            date=row["date"],
            edited=row["edited"],
            sender=row["sender"],
            sender_id=row["sender_id"],
            text=row["text"],
            reply_to_message_id=row["reply_to_message_id"],
        )

    def upsert(self, message: TelegramMessage) -> bool:
        """Insert or update one message; return True when stored content changed."""
        changed = self._upsert_one(message)
        self.connection.commit()
        return changed

    def _upsert_one(self, message: TelegramMessage) -> bool:
        existing = self.connection.execute(
            "SELECT text, edited, kind FROM messages WHERE chat_id = ? AND message_id = ?",
            (message.chat_id, message.message_id),
        ).fetchone()
        changed = existing is None or tuple(existing) != (message.text, message.edited, message.kind)
        self.connection.execute(
            """
            INSERT INTO messages
                (chat_id, chat_name, message_id, kind, date, edited, sender, sender_id,
                 text, reply_to_message_id, text_entities_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                chat_name=excluded.chat_name, kind=excluded.kind, date=excluded.date,
                edited=excluded.edited, sender=excluded.sender, sender_id=excluded.sender_id,
                text=excluded.text, reply_to_message_id=excluded.reply_to_message_id,
                text_entities_json=excluded.text_entities_json
            """,
            (
                message.chat_id,
                message.chat_name,
                message.message_id,
                message.kind,
                message.date,
                message.edited,
                message.sender,
                message.sender_id,
                message.text,
                message.reply_to_message_id,
                json.dumps(message.raw_text_entities, ensure_ascii=False),
            ),
        )
        if changed:
            self.connection.execute(
                "DELETE FROM messages_fts WHERE chat_id = ? AND message_id = ?",
                (message.chat_id, str(message.message_id)),
            )
            self.connection.execute(
                "INSERT INTO messages_fts(chat_id, message_id, text) VALUES (?, ?, ?)",
                (message.chat_id, str(message.message_id), message.text),
            )
        return changed

    def upsert_many(self, messages: Iterable[TelegramMessage]) -> int:
        items = list(messages)
        if not items:
            return 0
        existing: dict[tuple[str, int], tuple[object, ...]] = {}
        for chat_id in {item.chat_id for item in items}:
            rows = self.connection.execute(
                "SELECT chat_id, message_id, text, edited, kind FROM messages WHERE chat_id = ?",
                (chat_id,),
            ).fetchall()
            existing.update(
                {(row["chat_id"], row["message_id"]): (row["text"], row["edited"], row["kind"])
                 for row in rows}
            )
        changed = sum(
            existing.get((item.chat_id, item.message_id))
            != (item.text, item.edited, item.kind)
            for item in items
        )
        rows = [
            (
                item.chat_id, item.chat_name, item.message_id, item.kind, item.date,
                item.edited, item.sender, item.sender_id, item.text,
                item.reply_to_message_id, json.dumps(item.raw_text_entities, ensure_ascii=False),
            )
            for item in items
        ]
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO messages
                    (chat_id, chat_name, message_id, kind, date, edited, sender, sender_id,
                     text, reply_to_message_id, text_entities_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    chat_name=excluded.chat_name, kind=excluded.kind, date=excluded.date,
                    edited=excluded.edited, sender=excluded.sender, sender_id=excluded.sender_id,
                    text=excluded.text, reply_to_message_id=excluded.reply_to_message_id,
                    text_entities_json=excluded.text_entities_json
                """,
                rows,
            )
            self.connection.execute("DELETE FROM messages_fts")
            self.connection.execute(
                "INSERT INTO messages_fts(chat_id, message_id, text) "
                "SELECT chat_id, CAST(message_id AS TEXT), text FROM messages"
            )
        return changed

    def get(self, chat_id: str, message_id: int) -> StoredMessage | None:
        row = self.connection.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        return self._row_to_message(row) if row else None

    def search(self, query: str, *, chat_id: str | None = None, limit: int = 50) -> list[StoredMessage]:
        if not query.strip():
            return []
        params: list[object] = [query]
        where = ""
        if chat_id is not None:
            where = " AND m.chat_id = ?"
            params.append(chat_id)
        params.append(max(1, min(limit, 500)))
        rows = self.connection.execute(
            f"""
            SELECT m.* FROM messages_fts f
            JOIN messages m ON m.chat_id = f.chat_id AND CAST(m.message_id AS TEXT) = f.message_id
            WHERE messages_fts MATCH ?{where}
            ORDER BY m.date DESC, m.message_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def stats(self, *, chat_id: str | None = None) -> dict[str, int]:
        where = " WHERE chat_id = ?" if chat_id is not None else ""
        params = (chat_id,) if chat_id is not None else ()
        row = self.connection.execute(
            f"SELECT COUNT(*) AS total, SUM(kind = 'message') AS ordinary FROM messages{where}",
            params,
        ).fetchone()
        return {"total": int(row["total"] or 0), "ordinary": int(row["ordinary"] or 0)}
