"""Runtime config of the ops bot, built from env vars that kit_env.py derives from the brand pack.

KIT_OPS_CONFIG   JSON with every non-secret setting (see BRAND_PACK.md, section bot.*); file paths are absolute
KIT_OPS_BOT_TOKEN, KIT_OPS_CHAT_ID   operator bot token and chat (from env:NAME refs in the pack)
KIT_HOME         deployment directory: telegram_sender/ (sessions, api config), outreach/send_log.jsonl, ops_state/,
                 mail_state/ (mail cards, see kit/bots/mail)
OPENAI_API_KEY   LLM key (OpenAI-compatible API; base URL from llm.base_url)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

MODES = ("off", "draft", "auto")
ROUTE_ACTIONS = ("auto", "draft", "handoff")


class ConfigError(Exception):
    pass


def _read_files(paths) -> str:
    parts = []
    for p in paths or []:
        try:
            parts.append(Path(p).read_text(encoding="utf-8").strip())
        except OSError as e:
            raise ConfigError(f"cannot read pack file {p}: {e}") from e
    return "\n\n".join(x for x in parts if x)


def kit_home(env=None) -> Path:
    env = os.environ if env is None else env
    home = (env.get("KIT_HOME") or env.get("HERMES_HOME") or "").strip()
    if not home:
        raise ConfigError("KIT_HOME (or HERMES_HOME) must be set: the bot will not guess a deployment directory")
    return Path(home)


@dataclass
class OpsConfig:
    company_name: str
    company_description: str = ""
    language: str = "en"
    operator_language: str = "en"
    timezone: str = "UTC"
    accounts: list = field(default_factory=list)
    default_mode: str = "draft"
    account_modes: dict = field(default_factory=dict)
    batching_seconds: float = 20.0
    reply_categories: list = field(default_factory=list)
    internal_markers: list = field(default_factory=list)
    internal_ids: list = field(default_factory=list)
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = "https://api.openai.com/v1"
    persona_text: str = ""
    facts_text: str = ""
    tariffs: list = field(default_factory=list)
    gate_enabled: bool = True
    gate_rules_text: str = ""
    daily_limit: int = 0
    typing_base_s: float = 3.0
    typing_per_char_ms: float = 40.0
    typing_max_s: float = 20.0
    takeover_hours: float = 24.0
    stages: list = field(default_factory=list)
    routes: list = field(default_factory=list)
    crm_type: str = "none"
    crm: dict = field(default_factory=dict)
    outreach_disabled: list = field(default_factory=list)   # Telegram accounts the pack forbids for outreach
    mail: dict = field(default_factory=dict)                # mail cards (kit/bots/mail), empty = off
    ops_bot_token: str | None = None
    ops_chat_id: str | None = None
    home: Path | None = None

    # ------------------------------------------------------------------ paths
    @property
    def state_dir(self) -> Path:
        return self.home / "ops_state"

    @property
    def send_log_path(self) -> Path:
        return self.home / "outreach" / "send_log.jsonl"

    @property
    def sender_dir(self) -> Path:
        return self.home / "telegram_sender"

    def mode_for(self, account: str) -> str:
        return self.account_modes.get(account) or self.default_mode

    def typing_seconds(self, text: str) -> float:
        raw = self.typing_base_s + len(text or "") * self.typing_per_char_ms / 1000.0
        return max(0.0, min(self.typing_max_s, raw))

    def tariffs_text(self) -> str:
        lines = []
        for t in self.tariffs:
            fee = t.get("fee_percent")
            fee_s = f"{fee}%" if fee is not None else "individual"
            note = f" ({t['notes']})" if t.get("notes") else ""
            lines.append(f"- {t.get('name', t.get('id'))}: {fee_s}{note}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_dict(cls, data: dict, *, home: Path, token=None, chat_id=None) -> "OpsConfig":
        company = data.get("company") or {}
        ops = data.get("ops") or {}
        conc = data.get("concierge") or {}
        gate = conc.get("gate") or {}
        typing = conc.get("typing_delay") or {}
        if not company.get("name"):
            raise ConfigError("company.name is required")
        default_mode = ops.get("mode", "draft")
        if default_mode not in MODES:
            raise ConfigError(f"bot.ops.mode must be one of {MODES}, got {default_mode!r}")
        account_modes = dict(ops.get("account_modes") or {})
        bad = {a: m for a, m in account_modes.items() if m not in MODES}
        if bad:
            raise ConfigError(f"bot.ops.account_modes: invalid modes {bad}")
        for r in conc.get("routes") or []:
            if r.get("action") not in ROUTE_ACTIONS:
                raise ConfigError(f"route {r.get('id')!r}: action must be one of {ROUTE_ACTIONS}")
        gate_enabled = bool(gate.get("enabled", True))
        return cls(
            company_name=company["name"],
            company_description=company.get("description", ""),
            language=company.get("language", "en"),
            operator_language=ops.get("operator_language") or company.get("language", "en"),
            timezone=company.get("timezone", "UTC"),
            accounts=list(ops.get("accounts") or []),
            default_mode=default_mode,
            account_modes=account_modes,
            batching_seconds=float(ops.get("batching_seconds", 20)),
            reply_categories=list(ops.get("reply_categories") or []),
            internal_markers=[m.lower() for m in data.get("internal_markers") or []],
            internal_ids=[int(x) for x in data.get("internal_ids") or []],
            llm_model=(data.get("llm") or {}).get("model", "gpt-4o-mini"),
            llm_base_url=(data.get("llm") or {}).get("base_url", "https://api.openai.com/v1"),
            persona_text=_read_files([conc["persona_file"]] if conc.get("persona_file") else []),
            facts_text=_read_files(data.get("facts_files") or []),
            tariffs=list(data.get("tariffs") or []),
            gate_enabled=gate_enabled,
            gate_rules_text=_read_files([gate["rules_file"]] if gate_enabled and gate.get("rules_file") else []),
            daily_limit=int(conc.get("daily_limit", 0)),
            typing_base_s=float(typing.get("base_s", 3)),
            typing_per_char_ms=float(typing.get("per_char_ms", 40)),
            typing_max_s=float(typing.get("max_s", 20)),
            takeover_hours=float(conc.get("takeover_hours", 24)),
            stages=list(conc.get("stages") or []),
            routes=list(conc.get("routes") or []),
            crm_type=(data.get("crm") or {}).get("type", "none"),
            crm=dict(data.get("crm") or {}),
            outreach_disabled=list(data.get("outreach_disabled") or []),
            mail=dict(data.get("mail") or {}),
            ops_bot_token=token,
            ops_chat_id=chat_id,
            home=home,
        )

    @classmethod
    def from_env(cls, env=None) -> "OpsConfig":
        env = os.environ if env is None else env
        raw = env.get("KIT_OPS_CONFIG")
        if not raw:
            raise ConfigError("KIT_OPS_CONFIG is not set: run through `kit_env.py --module ops run -- ...`")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ConfigError(f"KIT_OPS_CONFIG is not valid JSON: {e}") from e
        cfg = cls.from_dict(data, home=kit_home(env), token=env.get("KIT_OPS_BOT_TOKEN"),
                            chat_id=env.get("KIT_OPS_CHAT_ID"))
        override = [a.strip() for a in env.get("KIT_OPS_ACCOUNTS", "").split(",") if a.strip()]
        if override:
            cfg.accounts = override
        return cfg
