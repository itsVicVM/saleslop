# Telegram operator bot

The listener watches a rep’s account for DMs and puts the thread and draft reply into a separate operator bot. The runner builds its configuration with `runner/kit_env.py --module ops`. One process polls each operator bot token.

## Review modes

- `off` sends a notification. `draft` offers reply, reply with a call to action, skip, edit, write your own and mute. A rep can reply to a card with an edit or enter it directly when only one card is waiting.
- `auto` can send eligible replies after the configured checks. Human takeover pauses automation for `takeover_hours`. A `draft` or `handoff` route requires a person. A handoff also produces a lead card. The daily cap uses the company timezone. Zero disables automatic replies. The model gate must explicitly approve before sending.
- Set `bot.ops.mode` for the default and `bot.ops.account_modes` for individual accounts. Operators can switch modes in `/settings`. Configure reply categories with `bot.ops.reply_categories`. The bot distinguishes unsolicited inbound messages and first replies.
- Consecutive messages from the same buyer are batched after `batching_seconds`. Cards show the configured stage. Typing delay follows `typing_delay` up to `max_s`. Each Telegram account has a separate Telethon client and can reconnect independently.
- Own messages, internal markers and muted contacts are filtered. Bot messages and automatic replies can be logged quietly. FloodWait or PeerFlood pauses the account, alerts the rep and brings the draft back for review.

## Outreach, CRM and mail

The sender checks signatures, language, account limits and spacing. A follow-up needs a recorded opener. Steps 1–3 use the tracker and `templates/followup_*.md`, then verify the recorded stage. Disable outgoing outreach with `bot.sender.outreach_disabled` or the account switch in `/settings`. Reply handling remains available. A rep can re-enable only an account allowed by the pack. Mail outreach reads the same `KIT_HOME/ops_state/outreach_off.json`.

CRM defaults to `none`. The operator confirms a deal match before a note is written. HubSpot and Bitrix24 support lookup and notes. For amoCRM, `crm.writeback`, `advance_on_reply` and `task_on_reply` control additional actions. When `mail.bot.enabled` is true, the same operator chat receives email cards. [CRM settings](../../docs/INTEGRATIONS.md) · [Mail cards](../mail/README.md)

## Run and check

```bash
# Run from the checkout root
python3 runner/kit_env.py --pack ../my-sales/pack.json --module ops --env-file /path/.env check
python3 runner/kit_env.py --pack ../my-sales/pack.json --module ops --env-file /path/.env run -- python3 -m bots.ops.telegram_listener
python3 runner/kit_env.py --pack ../my-sales/pack.json --module ops --env-file /path/.env run -- python3 -m bots.ops.sender list
python3 runner/kit_env.py --pack ../my-sales/pack.json --module ops --env-file /path/.env run -- python3 -m bots.ops.followup --dry-run
```

The listener command connects live accounts. Run the configuration check first. The environment supplies `KIT_HOME`, `OPENAI_API_KEY` and the pack’s references such as `OPS_BOT_TOKEN`, `OPS_OWNER_CHAT_ID` and `SHEET_ID`. State includes `telegram_sender/config.json`, `telegram_sender/sessions/`, `outreach/send_log.jsonl` and `ops_state/`. Keep each Telegram session on one host to avoid `AUTH_KEY_DUPLICATED`.

Local tests use substitute transports. Validate a real reply and takeover on a team test account. Current limits include group conversations, Sheets enrichment for Telegram drafts, CRM history backfill and adding sessions without a restart. Select follow-up accounts in the pack. The listener requires Telegram accounts even when the rep primarily wants mail cards.
