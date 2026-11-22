"""Operator-facing texts of the mail cards and screens (ru/en, picked by bot.ops.operator_language).
Neutral voice on purpose: the bot persona belongs to the pack, not to the operator UI."""
from __future__ import annotations

UI = {
    "ru": {
        "card_head": "📧 [{box}] письмо от {who}", "not_in_tracker": "нет в трекере", "by_domain": "по домену",
        "subject": "Тема", "no_text": "(текста нет)", "translation": "перевод", "draft": "Черновик ответа",
        "draft_tr": "перевод черновика", "new_email": "✉️ Новое письмо с {box} → {to}",
        "no_send_note": "⚠️ С этого ящика отправка с сервера сейчас недоступна (SMTP не отвечает): скопируй черновик и отправь сам.",
        "st_sent": "✅ Отправлено", "st_skipped": "⏭ Пропущено", "st_failed": "⚠️ Не отправилось: {err}",
        "st_unknown": "❓ Исход отправки неизвестен: письмо могло уйти. Проверь папку «Отправленные» этого ящика, потом нажми кнопку ниже.",
        "st_sending": "⏳ Отправляется…",
        "b_draft": "✍️ Черновик", "b_send": "✅ Отправить", "b_edit": "✏️ Править", "b_own": "📝 Свой текст",
        "b_redo": "🔄 Заново", "b_skip": "Пропустить", "b_was_sent": "Ушло (отметить)", "b_resend": "Не ушло — отправить снова",
        "sent_confirm": "✅ Письмо отправлено: {box} → {to}\nТема: {subject}",
        "marked_sent": "Отмечено как отправленное", "resend_confirm": "Повторная отправка после проверки «Отправленных»",
        "drafting": "Готовлю черновик…", "draft_failed": "⚠️ Черновик не получился (модель не ответила): попробуй ещё раз или напиши свой текст.",
        "sending": "Отправляю…", "already_sent": "Уже отправлено", "stale": "Карточка уже неактуальна",
        "no_send": "С этого ящика отправка сейчас недоступна", "skipped": "Пропущено", "unknown": "Неизвестное действие",
        "edit_prompt": "✏️ [{box}] Что поправить в черновике письма для {to}? Пиши словами, например «короче и без цифр».",
        "own_prompt": "📝 [{box}] Свой текст письма для {to} (покажу карточкой, отправка по кнопке).",
        "compose_prompt": "✉️ Новое письмо с {box}\n\nОтветь на это сообщение так:\nкому@example.com | тема\nс новой строки — текст письма\n\n(пришлю карточку, отправка только по кнопке)",
        "compose_format": "⚠️ Формат: первая строка «кому@example.com | тема», дальше текст письма. Нажми «✉️ Новое письмо» ещё раз.",
        "redraft_failed": "Не получилось переписать черновик (сбой LLM), попробуй ещё раз",
        "reply_hint": "Чтобы поправить письмо, нажми на карточке «✏️ Править» (правка словами) или «📝 Свой текст», потом напиши текст.",
        "mail_title": "📧 Почта", "mail_hint": "Ящики этого бота. Нажми на ящик, чтобы настроить.",
        "no_boxes": "Ящиков нет: добавь mail.accounts в пак или подключи ящик (CLI bots.mail.connect).",
        "menu_row": "📧 Почта →", "back": "⬅️ Назад", "on": "ВКЛ", "off": "ВЫКЛ",
        "notify": "Уведомления о письмах", "reply": "Ответы на входящие",
        "reply_draft": "черновик сразу", "reply_off": "черновик по кнопке",
        "outreach": "Рассылка с ящика", "out_on": "✅ можно", "out_off": "⛔ выключена", "out_locked": "🔒 выключена в паке",
        "send_ok": "отправка ✅", "send_bad": "отправка ⛔ (SMTP не отвечает)", "send_unknown": "отправка: не проверялась",
        "box_text": "📧 {addr}\n\nУведомления о новых письмах, ответы (черновик сразу или по кнопке) и разрешение рассылки. "
                    "Автоответы, уведомления о недоставке и рассылки карточек не дают, они только пишутся в лог. "
                    "Письмо уходит только по кнопке «Отправить».",
        "b_new": "✉️ Новое письмо с этого ящика", "b_disconnect": "🗑 Отключить ящик", "b_connect_google": "➕ Подключить Gmail / Google Workspace",
        "disconnected": "🗑 Ящик отключён, его токен удалён с сервера.", "not_disconnectable": "Ящик из пака отключается правкой пака.",
        "connect_addr": "➕ Подключение Gmail / Google Workspace\n\nОтветь на это сообщение адресом ящика (например name@company.example).\n\n"
                        "Ящики на другом хостинге (IMAP/SMTP) подключаются на сервере командой `python3 -m bots.mail.connect start --addr ...`: "
                        "пароль ящика в чат не присылай.",
        "connect_bad_addr": "⚠️ Не похоже на адрес почты. Нажми «➕ Подключить» ещё раз.",
        "connect_not_google": "Почта домена {domain} не на Google. Такой ящик подключается на сервере: `python3 -m bots.mail.connect start --addr {addr}`.",
        "connect_no_client": "⚠️ Не задан OAuth-клиент Google (mail.bot.gmail_client_secret в паке).",
        "connect_steps": "{addr}\n\nПодключаем входом в Google, пароль не нужен.\n1) Открой ссылку и войди в аккаунт Google с этой почтой:\n{url}\n\n"
                         "2) Разреши чтение и отправку почты.\n3) Браузер покажет ошибку «не удаётся открыть localhost»: так и должно быть. "
                         "Скопируй адрес из строки браузера целиком (начинается с http://localhost/?code=) и пришли ответом на следующее сообщение.",
        "connect_wait": "Жду адрес из браузера (ответом на это сообщение).",
        "connect_denied": "Доступ не выдан ({err}). Если Google пишет «доступ заблокирован», администратор Google клиента должен разрешить приложение для этого адреса.",
        "connect_code_bad": "Google не принял код: {err}. Нажми «➕ Подключить» и пройди шаги заново.",
        "connect_no_profile": "Не удалось проверить, в какой аккаунт Google был вход. Пройди шаги заново.",
        "connect_ok": "✅ Ящик {addr} подключён через Google: уведомления включены, черновики по кнопке, рассылка с него выключена. Настройки: /settings → Почта.",
        "connect_other": "\n(Введён {typed}, а вход был в {real}: подключён {real}.)",
        "connect_delete_hint": "\n\n(Можно удалить своё сообщение с адресом из браузера.)",
        "auth_lost": "⚠️ Доступ к Gmail {addr} истёк или отозван ({err}). Подключи ящик заново: /settings → Почта → Подключить.",
    },
    "en": {
        "card_head": "📧 [{box}] email from {who}", "not_in_tracker": "not in the tracker", "by_domain": "by domain",
        "subject": "Subject", "no_text": "(no text)", "translation": "translation", "draft": "Reply draft",
        "draft_tr": "draft translation", "new_email": "✉️ New email from {box} → {to}",
        "no_send_note": "⚠️ Sending from this mailbox is unavailable from the server right now (SMTP does not answer): copy the draft and send it yourself.",
        "st_sent": "✅ Sent", "st_skipped": "⏭ Skipped", "st_failed": "⚠️ Not sent: {err}",
        "st_unknown": "❓ Send outcome unknown: the email may have gone out. Check this mailbox's Sent folder, then press a button below.",
        "st_sending": "⏳ Sending…",
        "b_draft": "✍️ Draft", "b_send": "✅ Send", "b_edit": "✏️ Edit", "b_own": "📝 My text",
        "b_redo": "🔄 Redo", "b_skip": "Skip", "b_was_sent": "It went out (mark)", "b_resend": "It did not — send again",
        "sent_confirm": "✅ Email sent: {box} → {to}\nSubject: {subject}",
        "marked_sent": "Marked as sent", "resend_confirm": "Re-sending after checking Sent",
        "drafting": "Drafting…", "draft_failed": "⚠️ No draft (the model did not answer): try again or write your own text.",
        "sending": "Sending…", "already_sent": "Already sent", "stale": "This card is no longer current",
        "no_send": "Sending from this mailbox is unavailable", "skipped": "Skipped", "unknown": "Unknown action",
        "edit_prompt": "✏️ [{box}] What should change in the email draft for {to}? Plain words, e.g. \"shorter, no numbers\".",
        "own_prompt": "📝 [{box}] Your email text for {to} (shown as a card, sent on the button).",
        "compose_prompt": "✉️ New email from {box}\n\nReply to this message like this:\nto@example.com | subject\nthe email text on the next lines\n\n(you get a card, sending only on the button)",
        "compose_format": "⚠️ Format: first line \"to@example.com | subject\", then the text. Press \"✉️ New email\" again.",
        "redraft_failed": "Could not redraft (LLM failure), try again",
        "reply_hint": "To change the email, press \"✏️ Edit\" (plain-word correction) or \"📝 My text\" on the card, then type.",
        "mail_title": "📧 Mail", "mail_hint": "Mailboxes of this bot. Tap one to configure it.",
        "no_boxes": "No mailboxes: add mail.accounts to the pack or connect one (CLI bots.mail.connect).",
        "menu_row": "📧 Mail →", "back": "⬅️ Back", "on": "ON", "off": "OFF",
        "notify": "New email notifications", "reply": "Replies to incoming",
        "reply_draft": "draft right away", "reply_off": "draft on button",
        "outreach": "Outreach from this mailbox", "out_on": "✅ allowed", "out_off": "⛔ off", "out_locked": "🔒 off in the pack",
        "send_ok": "sending ✅", "send_bad": "sending ⛔ (SMTP does not answer)", "send_unknown": "sending: not checked",
        "box_text": "📧 {addr}\n\nNew email notifications, replies (draft right away or on button) and outreach permission. "
                    "Auto-replies, bounce notices and newsletters never become cards, they are only logged. "
                    "An email goes out only on the Send button.",
        "b_new": "✉️ New email from this mailbox", "b_disconnect": "🗑 Disconnect mailbox", "b_connect_google": "➕ Connect Gmail / Google Workspace",
        "disconnected": "🗑 Mailbox disconnected, its token removed from the server.", "not_disconnectable": "Pack mailboxes are removed by editing the pack.",
        "connect_addr": "➕ Connect Gmail / Google Workspace\n\nReply with the mailbox address (e.g. name@company.example).\n\n"
                        "Mailboxes on other hosting (IMAP/SMTP) are connected on the server: `python3 -m bots.mail.connect start --addr ...`; "
                        "never send a mailbox password to the chat.",
        "connect_bad_addr": "⚠️ That does not look like an email address. Press \"➕ Connect\" again.",
        "connect_not_google": "Mail of {domain} is not on Google. Connect it on the server: `python3 -m bots.mail.connect start --addr {addr}`.",
        "connect_no_client": "⚠️ No Google OAuth client configured (mail.bot.gmail_client_secret in the pack).",
        "connect_steps": "{addr}\n\nConnecting with a Google sign-in, no password needed.\n1) Open the link and sign in with this mailbox's Google account:\n{url}\n\n"
                         "2) Allow reading and sending mail.\n3) The browser then shows \"cannot connect to localhost\": expected. "
                         "Copy the whole address bar (starts with http://localhost/?code=) and send it as a reply to the next message.",
        "connect_wait": "Waiting for the browser address (reply to this message).",
        "connect_denied": "Access not granted ({err}). If Google says \"access blocked\", the client's Google admin has to allow the app for this address.",
        "connect_code_bad": "Google did not accept the code: {err}. Press \"➕ Connect\" and repeat the steps.",
        "connect_no_profile": "Could not check which Google account signed in. Repeat the steps.",
        "connect_ok": "✅ Mailbox {addr} connected via Google: notifications on, drafts on button, outreach from it off. Settings: /settings → Mail.",
        "connect_other": "\n(Typed {typed}, signed in as {real}: {real} connected.)",
        "connect_delete_hint": "\n\n(You can delete your message with the browser address.)",
        "auth_lost": "⚠️ Gmail access for {addr} expired or was revoked ({err}). Reconnect: /settings → Mail → Connect.",
    },
}


def ui(lang: str) -> dict:
    return UI.get(lang, UI["en"])
