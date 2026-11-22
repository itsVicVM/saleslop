"""Optional Jev System One adapter; never called unless explicitly configured."""

from __future__ import annotations

import json
from typing import Any
from urllib import request


class JevClient:
    def __init__(self, api_key: str, *, endpoint: str = "https://api.typesafe.ai/v1/systemone", model: str = "jev-latest"):
        if not api_key:
            raise ValueError("Jev API key is required")
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model

    def decide(self, state: str, questions: dict[str, dict[str, Any]], *, timeout: float = 20.0) -> dict[str, Any]:
        payload = json.dumps({"model": self.model, "state": state, "questions": questions}).encode("utf-8")
        req = request.Request(
            self.endpoint,
            data=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
