"""Telegram Bot API client for the operator ("ops") bot: cards, menus, callbacks.

A separate bot token on purpose: callback_query of a bot reaches only the single process that polls its
getUpdates, so the ops bot must not share a token with any other agent/gateway bot."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class OpsBot:
    def __init__(self, token: str | None, chat_id: str | None, timeout: int = 15):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    @property
    def bot_id(self):
        """A bot's own user id is the numeric prefix of its token (used for the echo guard)."""
        try:
            return int(str(self.token).split(":", 1)[0])
        except (ValueError, AttributeError):
            return None

    def _call(self, method: str, payload: dict, timeout: int | None = None):
        if not self.token:
            print(f"[ops/bot] no bot token configured, {method} skipped")
            return None
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}",
                                     data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"[ops/bot] {method} failed: {e}")
            return None
        if not data.get("ok"):
            print(f"[ops/bot] {method} not ok: {str(data)[:200]}")
            return None
        return data.get("result")

    def send(self, text: str, buttons=None, force_reply: bool = False, reply_to=None):
        """Returns the message id or None: callers must not assume the operator saw anything."""
        payload = {"chat_id": self.chat_id, "text": text[:4096]}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        elif force_reply:
            payload["reply_markup"] = {"force_reply": True, "selective": True}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        res = self._call("sendMessage", payload)
        return res.get("message_id") if isinstance(res, dict) else None

    def edit(self, message_id, text: str, buttons=None) -> None:
        if not message_id:
            return
        payload = {"chat_id": self.chat_id, "message_id": message_id, "text": text[:4096]}
        if buttons is not None:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        self._call("editMessageText", payload)

    def delete(self, message_id) -> bool:
        """Telegram refuses to delete bot messages older than ~48h: callers still send a fresh confirmation."""
        if not message_id:
            return False
        return bool(self._call("deleteMessage", {"chat_id": self.chat_id, "message_id": message_id}))

    def answer(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def get_updates(self, offset: int, timeout: int = 30):
        return self._call("getUpdates", {"offset": offset, "timeout": timeout}, timeout=timeout + 5) or []
