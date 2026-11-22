"""Pure text helpers: handle normalisation, markdown stripping, LLM output parsing, auto-responder detection."""
from __future__ import annotations

import datetime as dt
import re

# Output contract the compose prompt asks for (prompts.py); markers are fixed, content is free text.
MARKERS = ("STAGE:", "REPLY:", "CTA:", "TRANSLATION:", "INCOMING_TRANSLATION:")
EMPTY = ("", "-", "—", "'-'")


def normalize_handle(h) -> str:
    return str(h or "").strip().lstrip("@").lower()


def is_muted(entries, target, now: dt.datetime | None = None) -> bool:
    """Entries are bare handles/ids (permanent) or {"handle"|"id": ..., "until": iso|None} (snooze)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    for entry in entries or []:
        key = entry.get("handle", entry.get("id")) if isinstance(entry, dict) else entry
        key = normalize_handle(key) if isinstance(key, str) else key
        if key != target:
            continue
        until = entry.get("until") if isinstance(entry, dict) else None
        if not until:
            return True
        try:
            if now < dt.datetime.fromisoformat(str(until).replace("Z", "+00:00")):
                return True
        except ValueError:
            return True  # malformed snooze: treat as permanent rather than silently unmuting
    return False


def sanitize_markdown(text: str) -> str:
    """Telegram user accounts send text verbatim, so markdown would reach the contact as raw symbols.
    Enforced in code because a prompt instruction alone is not a guarantee."""
    text = re.sub(r"^#{1,6}\s*", "", text or "", flags=re.MULTILINE)
    text = re.sub(r"^[\-\*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    return text.strip()


def parse_compose_output(raw: str) -> dict:
    """Split the marker-delimited LLM answer. Never raises; text without markers is the reply itself."""
    raw = (raw or "").strip()
    out = {"stage": "", "reply": "", "cta": "", "translation": "", "incoming_translation": ""}
    if "REPLY:" not in raw:
        out["reply"] = sanitize_markdown(raw)
        return out
    pattern = "(" + "|".join(re.escape(m) for m in MARKERS) + ")"
    parts = re.split(pattern, raw)
    current = None
    for chunk in parts:
        if chunk in MARKERS:
            current = chunk[:-1].lower()
            continue
        if current:
            value = chunk.strip()
            out[current] = "" if value in EMPTY else value
    for key in ("reply", "cta", "translation", "incoming_translation"):
        out[key] = sanitize_markdown(out[key]) if out[key] else ""
    out["stage"] = out["stage"].splitlines()[0].strip() if out["stage"] else ""
    return out


def needs_translation(text: str, operator_language: str) -> bool:
    """Cheap script check: only Cyrillic-vs-Latin operator languages are detected."""
    lat = len(re.findall(r"[A-Za-z]", text or ""))
    cyr = len(re.findall(r"[А-Яа-яЁё]", text or ""))
    if operator_language in ("ru", "uk", "be", "kk", "bg", "sr"):
        return lat >= 4 and lat > cyr
    return cyr >= 4 and cyr > lat


# --------------------------------------------------------------------- auto-responders and bot menus
_AUTO_STRONG = re.compile(
    r"(out of (the )?office|automatic(ally generated)? (reply|response|message)|auto[- ]?(reply|response)|this is an automated|"
    r"автоответ|автоматическ(ое|ий) (сообщение|ответ|уведомление)|нерабочее время|вне рабочего времени|"
    r"(our|the) (working|business|office) hours|график работы|自动回复|自動回覆|稍等|已收到|收到您的|请稍候|請稍候|将尽快回复)", re.I)
_AUTO_ACK = re.compile(
    r"(thank(s| you) for (reaching|contacting|getting in touch|your (message|inquiry|enquiry|email|request|interest))|"
    r"(we|i)( have|'ve) received your|спасибо за (ваше )?(обращение|сообщение)|"
    r"(ваше )?(обращение|сообщение|заявка|запрос) (принят|получен|зарегистрирован))", re.I)
# A real answer ("not interested", "we already use X", any question) is never an auto-responder.
_AUTO_SUBSTANCE = re.compile(
    r"(not interested|no,? thank|already|(currently|now) (use|work)|we (use|work with)|don't need|do not need|"
    r"не интересн|уже (есть|польз|работ)|пользуемся|используем|не нужн|отказ|\?)", re.I)
_BOT_MENU = re.compile(
    r"(choose (the |your )?(preferred )?language|выберите (удобный )?язык|оберіть (зручну )?мову|are you still there|"
    r"вы (еще|ещё) здесь|добро пожаловать|welcome to|верификац|verif(y|ication)|нажмите (на )?кнопк|press (the )?button|"
    r"select (an )?option|выберите (пункт|раздел|действие)|главное меню|main menu|ваш(е)? (обращение|тикет)|"
    r"your (ticket|request) (has been|is)|/start)", re.I)


def is_autoreply_text(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 300 or _AUTO_SUBSTANCE.search(t):
        return False
    return bool(_AUTO_STRONG.search(t)) or (len(t) <= 160 and bool(_AUTO_ACK.search(t)))


def is_bot_menu_text(text: str) -> bool:
    t = (text or "").strip()
    return len(t) < 4 or bool(_BOT_MENU.search(t))
