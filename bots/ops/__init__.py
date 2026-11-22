"""Ops bot + concierge: Telegram DM listener, draft cards in an operator bot, off/draft/auto modes.

Nothing here reads the brand pack directly: `kit/runner/kit_env.py --module ops` turns the pack into
KIT_OPS_CONFIG (JSON) + secret env vars, and `config.OpsConfig.from_env()` reads them.
Importing any module of this package has no side effects (no network, no files, no telethon import).
"""
