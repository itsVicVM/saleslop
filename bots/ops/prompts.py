"""LLM prompts. The technical contract (markers, language mirroring, plain text) lives here; everything about
the company (name, description, persona, facts, tariffs, stages, route hints) comes from the brand pack."""
from __future__ import annotations

from .config import OpsConfig

LANG_NAMES = {"ru": "Russian", "en": "English", "es": "Spanish", "de": "German", "fr": "French", "pt": "Portuguese",
              "uk": "Ukrainian", "it": "Italian", "tr": "Turkish"}


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


def classify_prompt(cfg: OpsConfig) -> str:
    return (f"Classify an incoming Telegram message sent to a {cfg.company_name} manager. "
            "Answer with exactly one category from this list, copied verbatim, nothing else: "
            + "; ".join(cfg.reply_categories))


def compose_system(cfg: OpsConfig, route_hint: str = "") -> str:
    op_lang = lang_name(cfg.operator_language)
    parts = [
        f"You write a reply draft to a lead in Telegram on behalf of a manager of {cfg.company_name}"
        + (f" ({cfg.company_description})" if cfg.company_description else "") + ".",
        "Rules:",
        "- Reply strictly in the language of the contact's latest messages; never switch or mix languages.",
        "- Plain text only: no markdown, no headers, no bullet lists, no bold. Short: 1-4 sentences, one next step at the end.",
        "- Read the whole conversation: answer what the contact just asked, do not repeat what we already said, "
        "do not introduce yourself again if the conversation has already started.",
        "- Facts, prices and terms only from the FACTS and TARIFFS blocks. If a known fact answers the question, "
        "answer it directly. If the answer is not there, say you will check with the team; never invent facts.",
        "- Never ask for passwords, codes or card data.",
    ]
    if cfg.persona_text:
        parts += ["", "PERSONA AND TONE:", cfg.persona_text]
    if cfg.stages:
        parts += ["", "DIALOG STAGES (pick the one the conversation is at):"]
        parts += [f"- {s.get('id')}: {s.get('name', '')}" for s in cfg.stages]
    if route_hint:
        parts += ["", "SPECIAL INSTRUCTION FOR THIS MESSAGE:", route_hint]
    parts += [
        "",
        "Output format, exactly these markers, nothing else:",
        "STAGE:\n<stage id or ->",
        "REPLY:\n<the reply in the contact's language>",
        "CTA:\n<optional separate second message inviting to a call or a group chat, or ->",
        f"TRANSLATION:\n<{op_lang} translation of REPLY and CTA, or - if they are already in {op_lang}>",
        f"INCOMING_TRANSLATION:\n<{op_lang} translation of the contact's new messages, or - if already in {op_lang}>",
    ]
    return "\n".join(parts)


def compose_user(cfg: OpsConfig, item: dict) -> str:
    blocks = []
    if cfg.facts_text:
        blocks.append(f"FACTS ({cfg.company_name}):\n{cfg.facts_text}")
    tariffs = cfg.tariffs_text()
    if tariffs:
        blocks.append(f"TARIFFS:\n{tariffs}")
    lines = []
    if item.get("history"):
        lines.append(f"Conversation (oldest first):\n{item['history']}\n")
    lines.append(f"New message(s) from {item['peer']} (we answer these):\n{item['text']}\n")
    lines.append(f"Category: {item.get('category') or 'unknown'}")
    if item.get("context"):
        lines.append(f"Lead context: {item['context']}")
    if item.get("crm_hint"):
        lines.append(item["crm_hint"])
    if item.get("previous_draft") and item.get("correction"):
        lines.append(f"\nPrevious draft:\n{item['previous_draft']}\n\nThe operator asked to change it: {item['correction']}\n"
                     "Rewrite REPLY accordingly, keep the contact's language, everything else as before.")
    blocks.append("\n".join(lines))
    return "\n\n=====\n\n".join(blocks)


def gate_system(cfg: OpsConfig) -> str:
    return ("You are the controller that checks a bot reply before it is sent to a lead without a human. "
            "Check it against the rules below and against the FACTS and TARIFFS. "
            "Answer with one line only: `OK` if every rule holds, otherwise `BLOCK: <short reason>`.\n\n"
            f"RULES:\n{cfg.gate_rules_text}")


def gate_user(cfg: OpsConfig, item: dict, reply: str) -> str:
    return (f"FACTS:\n{cfg.facts_text}\n\nTARIFFS:\n{cfg.tariffs_text()}\n\n"
            f"Contact wrote:\n{item['text']}\n\nBot reply to check:\n{reply}")


def translate_system(cfg: OpsConfig) -> str:
    return (f"Translate the message into {lang_name(cfg.operator_language)}. Translation only, no comments, no quotes. "
            "Keep names, @handles, links, amounts and company names as they are.")
