"""Minimal OpenAI-compatible chat client. The engine only needs `complete(system, user, max_tokens) -> str | None`,
so tests and the offline dry run inject a fake with the same method."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class ChatLLM:
    def __init__(self, api_key: str | None, model: str, base_url: str = "https://api.openai.com/v1", timeout: int = 120):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(self, system: str, user: str, max_tokens: int = 2000):
        """Returns the text, or None on any failure: callers fall back to a notify-only card, never invent text."""
        if not self.api_key:
            return None
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # reasoning models spend completion tokens on thinking; a small cap returns an empty answer
            "max_completion_tokens": max_tokens,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"[ops/llm] request failed: {e}")
            return None
        try:
            choice = data["choices"][0]
            text = (choice["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError):
            print(f"[ops/llm] unexpected response shape: {str(data)[:200]}")
            return None
        if not text:
            print(f"[ops/llm] empty answer: finish_reason={choice.get('finish_reason')} usage={data.get('usage')}")
            return None
        return text
