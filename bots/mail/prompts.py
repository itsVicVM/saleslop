"""LLM prompts of the mail bot. The format contract (plain text, no subject, no signature, language of the
incoming email, facts only) lives here; company, persona, facts and tariffs come from the pack via OpsConfig."""
from __future__ import annotations

LANG_NAMES = {"ru": "Russian", "en": "English", "de": "German", "es": "Spanish", "fr": "French", "pt": "Portuguese",
              "it": "Italian", "uk": "Ukrainian", "tr": "Turkish", "pl": "Polish"}


def compose_system(cfg) -> str:
    parts = [f"You draft email replies on behalf of {cfg.company_name}"
             + (f" ({cfg.company_description})" if cfg.company_description else "") + ".",
             "Write a short, businesslike, human reply in the language of the incoming email.",
             "Output only the body of the email: no subject line, no signature, no markdown.",
             "Address the sender by name when the name is known.",
             "Use only the facts below. If something is not covered, do not promise it: offer to clarify or to have a call.",
             "End with one concrete next step."]
    if cfg.persona_text:
        parts.append("Voice and rules of the company:\n" + cfg.persona_text)
    if cfg.facts_text:
        parts.append("FACTS:\n" + cfg.facts_text[:14000])
    tariffs = cfg.tariffs_text()
    if tariffs:
        parts.append("TARIFFS:\n" + tariffs)
    return "\n\n".join(parts)


def compose_user(card: dict, correction: str = "") -> str:
    tc = card.get("tracker") or {}
    ctx = (f"Tracker: company {tc.get('company', '')}, stage {tc.get('stage', '')}"
           + ("" if tc.get("exact", True) else " (matched by domain only)")) if tc else "The sender is not in the tracker."
    out = (f"{ctx}\n\nIncoming email from {card.get('from_name') or ''} <{card.get('from_addr', '')}>\n"
           f"Subject: {card.get('subject', '')}\n\n{(card.get('body') or '')[:3500]}\n\n")
    if correction:
        out += (f"Previous draft:\n{card.get('draft', '')}\n\nThe operator asked to change it: {correction}\n"
                "Rewrite the draft accordingly.\n")
    else:
        out += "Write the reply.\n"
    return out


def translate_system(lang: str) -> str:
    return (f"Translate the email text into {LANG_NAMES.get(lang, lang)}. Output only the translation, no comments "
            "or quotes. Keep names, addresses, links, amounts and company names as they are.")
