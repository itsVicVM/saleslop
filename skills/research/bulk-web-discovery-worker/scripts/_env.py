"""Minimal .env loader — no external dependency. Reads $HERMES_HOME/.env if present."""
import os


def load_env():
    home = os.environ.get("HERMES_HOME")
    if not home:
        return
    path = os.path.join(home, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value
