"""Operator-facing texts and inline keyboards of the ops bot (ru/en, picked by bot.ops.operator_language)."""
from __future__ import annotations

UI = {
    "ru": {
        "check_delivery": "Проверьте переписку перед ручной отправкой. Результат отправки неизвестен.",
        "draft_for": "Черновик ответа для", "incoming": "Входящее", "translation": "перевод", "context": "Контекст",
        "stage": "Этап", "draft": "Черновик", "cta": "CTA (вторым сообщением, кнопка «Ответить + CTA»)",
        "draft_tr": "Перевод черновика", "why_card": "Почему не автоответ",
        "send": "Ответить", "skip": "Пропустить", "send_cta": "Ответить + CTA", "edit": "Править",
        "own": "Свой текст", "mute": "🔇 Не следить",
        "sent": "✅ Отправлено", "skipped": "⏭ Пропущено", "auto_sent": "🤖 Автоответ отправлен",
        "own_sent": "✅ Отправлено (твой текст)", "what_to_fix": "Что поправить в черновике для",
        "own_prompt": "Свой текст для (отправлю как есть):", "send_failed": "Ошибка отправки, смотри лог",
        "redraft_failed": "Не получилось перегенерировать черновик (сбой LLM), попробуй ещё раз",
        "ambiguous": "Несколько карточек ждут текст: ответь (reply) именно на нужную карточку.",
        "compose_failed": "черновик не удалось составить, обработай вручную",
        "new_dm": "Новое сообщение от", "lead_card": "🎯 Лид для передачи человеку",
        "not_found": "Запись не найдена (возможно, уже обработана)", "done_already": "Уже обработано",
        "unknown": "Неизвестное действие", "saved": "Сохранено", "closed": "Закрыто", "muted": "Больше не слежу за этим чатом",
        "settings": "⚙️ Настройки ops-бота", "mode": "Режим ответа", "notify": "Уведомления о новых ЛС",
        "accounts": "Аккаунты", "close": "❌ Закрыть", "back": "⬅️ Назад", "on": "ВКЛ", "off_": "ВЫКЛ",
        "modes": {"off": "выкл (только уведомление)", "draft": "черновик + подтверждение", "auto": "авто (без подтверждения)"},
        "reasons": {"takeover": "человек недавно писал этому контакту сам", "daily_limit": "исчерпан суточный лимит автоответов",
                    "gate": "контролёр не пропустил", "route": "маршрут требует человека", "send_error": "автоотправка не удалась",
                    "paused": "аккаунт на паузе (FloodWait/PeerFlood)"},
        "crm_offer": "Похоже на сделку в CRM (нечёткий поиск, нужно подтверждение) для", "crm_skip": "Не привязывать",
        "crm_linked": "✅ Привязано к сделке", "crm_skipped": "Привязка пропущена",
        "cat_cold_inbound": "входящая заявка", "cat_first_reply": "первый ответ",
        "outreach": "📨 рассылка", "outreach_locked": "🔒 выключена в паке",
        "accounts_hint": "Слева режим ответов на входящие, справа рассылка (новые контакты и фоллоуапы). "
                         "Входящие бот видит в любом случае.",
    },
    "en": {
        "check_delivery": "Check the conversation before sending manually. Delivery is unconfirmed.",
        "draft_for": "Reply draft for", "incoming": "Incoming", "translation": "translation", "context": "Context",
        "stage": "Stage", "draft": "Draft", "cta": "CTA (second message, button \"Send + CTA\")",
        "draft_tr": "Draft translation", "why_card": "Why not auto",
        "send": "Send", "skip": "Skip", "send_cta": "Send + CTA", "edit": "Edit",
        "own": "My text", "mute": "🔇 Stop watching",
        "sent": "✅ Sent", "skipped": "⏭ Skipped", "auto_sent": "🤖 Auto-reply sent",
        "own_sent": "✅ Sent (your text)", "what_to_fix": "What should change in the draft for",
        "own_prompt": "Your text for (sent as is):", "send_failed": "Send failed, see the log",
        "redraft_failed": "Could not redraft (LLM failure), try again",
        "ambiguous": "Several cards are waiting for text: reply to the exact card.",
        "compose_failed": "no draft could be composed, handle it manually",
        "new_dm": "New message from", "lead_card": "🎯 Lead for a human",
        "not_found": "Item not found (maybe already handled)", "done_already": "Already handled",
        "unknown": "Unknown action", "saved": "Saved", "closed": "Closed", "muted": "Not watching this chat any more",
        "settings": "⚙️ Ops bot settings", "mode": "Reply mode", "notify": "New DM notifications",
        "accounts": "Accounts", "close": "❌ Close", "back": "⬅️ Back", "on": "ON", "off_": "OFF",
        "modes": {"off": "off (notify only)", "draft": "draft + approval", "auto": "auto (no approval)"},
        "reasons": {"takeover": "a human wrote to this contact recently", "daily_limit": "daily auto-reply limit reached",
                    "gate": "the controller blocked it", "route": "this route needs a human", "send_error": "auto-send failed",
                    "paused": "account paused (FloodWait/PeerFlood)"},
        "crm_offer": "Looks like a CRM deal (fuzzy search, please confirm) for", "crm_skip": "Do not link",
        "crm_linked": "✅ Linked to deal", "crm_skipped": "Linking skipped",
        "cat_cold_inbound": "cold inbound", "cat_first_reply": "first reply",
        "outreach": "📨 outreach", "outreach_locked": "🔒 off in the pack",
        "accounts_hint": "Left: reply mode for incoming messages; right: outreach (new contacts and follow-ups). "
                         "Incoming messages are always seen.",
    },
}

MODE_ORDER = ("draft", "auto", "off")


def ui(lang: str) -> dict:
    return UI.get(lang, UI["en"])


def approval_buttons(t: dict, sid: str, has_cta: bool):
    rows = [[{"text": t["send"], "callback_data": f"appr:send:{sid}"},
             {"text": t["skip"], "callback_data": f"appr:skip:{sid}"}]]
    if has_cta:
        rows.append([{"text": t["send_cta"], "callback_data": f"appr:sendcta:{sid}"}])
    rows.append([{"text": t["edit"], "callback_data": f"appr:edit:{sid}"},
                 {"text": t["own"], "callback_data": f"appr:owntext:{sid}"}])
    rows.append([{"text": t["mute"], "callback_data": f"appr:mute:{sid}"}])
    return rows


def card_body(t: dict, item: dict) -> str:
    """Always renders the LATEST draft: a correction round appends a new one."""
    draft = (item.get("drafts") or [{}])[-1]
    head = f"[{item.get('account', '?')}] {t['draft_for']} {item['peer']}"
    if item.get("category"):
        head += f" ({item['category']})"
    body = f"{head}\n\n{t['incoming']}:\n{item['text'][:700]}"
    if item.get("incoming_translation"):
        body += f"\n\n({t['translation']}: {item['incoming_translation']})"
    if item.get("context"):
        body += f"\n\n{t['context']}: {item['context'][:400]}"
    if item.get("stage"):
        body += f"\n\n🧭 {t['stage']}: {item['stage']}"
    body += f"\n\n{t['draft']}:\n{draft.get('text', '')}"
    if draft.get("cta"):
        body += f"\n\n{t['cta']}:\n{draft['cta']}"
    if draft.get("translation"):
        body += f"\n\n{t['draft_tr']}:\n{draft['translation']}"
    if item.get("hold_reason"):
        body += f"\n\n⚠️ {t['why_card']}: {item['hold_reason']}"
    return body


def lead_card(t: dict, item: dict, route_id: str) -> str:
    lines = [t["lead_card"], f"[{item.get('account')}] {item['peer']} · {item.get('category') or '-'} · {route_id}"]
    if item.get("stage"):
        lines.append(f"{t['stage']}: {item['stage']}")
    if item.get("context"):
        lines.append(f"{t['context']}: {item['context'][:400]}")
    lines.append(f"\n{t['incoming']}:\n{item['text'][:700]}")
    return "\n".join(lines)


def settings_menu(t: dict, cfg, settings: dict, extra_rows=None):
    default_mode = settings.get("default_mode") or cfg.default_mode
    notify = settings.get("notify", True)
    text = "\n".join([t["settings"], "", f"{t['mode']}: {t['modes'][default_mode]}",
                      f"{t['notify']}: {t['on'] if notify else t['off_']}"])
    rows = [[{"text": f"{t['mode']}: {t['modes'][default_mode]}", "callback_data": "set:mode:cycle"}],
            [{"text": f"{t['notify']}: {t['on'] if notify else t['off_']}", "callback_data": "set:notify:toggle"}],
            [{"text": f"👥 {t['accounts']} →", "callback_data": "nav:accounts"}],
            *(extra_rows or []),
            [{"text": t["close"], "callback_data": "nav:close"}]]
    return text, rows


def accounts_menu(t: dict, cfg, settings: dict, outreach_off=()):
    modes = {**cfg.account_modes, **(settings.get("account_modes") or {})}
    default_mode = settings.get("default_mode") or cfg.default_mode
    rows = []
    for name in cfg.accounts:
        m = modes.get(name) or default_mode
        if name in cfg.outreach_disabled:
            out = t["outreach_locked"]
        else:
            out = f"{t['outreach']}: {t['off_'] if name in outreach_off else t['on']}"
        rows.append([{"text": f"{name}: {t['modes'][m]}", "callback_data": f"set:acc:{name}"},
                     {"text": out, "callback_data": f"set:tgout:{name}"}])
    rows.append([{"text": t["back"], "callback_data": "nav:main"}])
    return f"👥 {t['accounts']}\n\n{t['accounts_hint']}", rows


def next_mode(mode: str) -> str:
    return MODE_ORDER[(MODE_ORDER.index(mode) + 1) % len(MODE_ORDER)] if mode in MODE_ORDER else "draft"
