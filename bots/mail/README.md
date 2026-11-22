# Mail cards in the operator bot

New email replies appear alongside Telegram DMs in the operator’s chat. A model can prepare a draft, and the rep can edit it in plain language or replace it. **Sending an email reply requires the operator to press Send.** The mail module shares the operator process and receives its configuration from `KIT_OPS_CONFIG["mail"]` via the runner.

## What arrives on a card

The module polls at `mail.bot.poll_seconds`. The initial pass establishes a baseline for each mailbox. Cards include sender, subject, message body without quoted history, optional tracker context matched by address or domain and a translation for the rep. Own addresses, automated mail, newsletters, bounces and no-reply messages go to `quiet.jsonl`.

Permanent delivery failures extracted from DSNs can be recorded in the outreach journal when `KIT_OUTREACH_STATE_DIR` is configured. If a campaign holds the journal lock, addresses wait in `bounce_queue.jsonl`.

Set `mail.accounts[].bot.reply` to `draft` to prepare a draft immediately. The default `off` lets the operator request one. Cards offer draft, edit, write your own, regenerate, skip and send. A new message composed in the mailbox screen also goes through a card. The sent confirmation names the mailbox, recipient and subject, and the event is journaled in `sent.jsonl`.

When delivery is uncertain, check Sent Items. For example, SMTP can accept a message before saving a copy fails. The card lets the operator mark the message as sent or explicitly retry. This avoids accidentally sending a second copy.

## Mailboxes and settings

- **Gmail / Google Workspace:** connect using the mail screen in `/settings` or `python3 -m bots.mail.connect start` followed by `finish-google`. Use the intended Google account and the team’s OAuth client referenced by `mail.bot.gmail_client_secret`.
- **Microsoft 365 work mail:** add a pack mailbox with provider `ms_graph`. Scope the application to the chosen mailbox. [Microsoft configuration](../../docs/INTEGRATIONS.md)
- **IMAP/SMTP:** configure a Himalaya account, its `<env_prefix>_HIMALAYA_ACCOUNT` variable and the client binary on the server. The terminal connection CLI accepts the mailbox password on stdin. It takes host settings from a preset selected by MX or `--preset`, writes a private credential file and rolls back configuration if the IMAP login fails. Yandex and Zoho US Business have named presets.
- `mail.accounts` is also used by the outreach module. Accounts connected later are recorded in `KIT_HOME/mail_state/connected.json`. The settings screen controls alerts, reply mode, permitted outreach accounts, new messages and disconnection. Himalaya SMTP checks run periodically. A rep can disable and re-enable an account only within the pack’s permissions. The shared switch is stored in `KIT_HOME/ops_state/outreach_off.json`.

## Run and check

Enable `mail.bot.enabled` in an operator pack. The module starts with `python3 -m bots.ops.telegram_listener`. To suspend polling without restarting, use `touch $KIT_HOME/mail_state/mail_off`.

```bash
# Run from the checkout root
python3 runner/kit_env.py --pack ../my-sales/pack.json --module ops --env-file /path/.env run -- python3 -m bots.mail.connect check
python3 runner/kit_env.py --pack ../my-sales/pack.json --module ops --env-file /path/.env run -- python3 -m bots.mail.connect status
```

Set `KIT_HOME`, the pack’s environment references and `OPENAI_API_KEY` for model drafts. Gmail needs its local OAuth client file. Optional settings include `KIT_MAIL_HIMALAYA_BIN`, `KIT_MAIL_HIMALAYA_CONFIG`, `KIT_OUTREACH_STATE_DIR` and `KIT_GOOGLE_API_DIR` for tracker context.

Tests cover mail flow with substitute transports. Verify OAuth, live reads and sends, DSN formats and the Sent folder with a test mailbox. Check the command forms and Seen-flag behavior for the installed Himalaya version; the adapter defines `LIST_ARGS`, `READ_ARGS` and `SEND_ARGS`. Attachment handling, deeper thread reconstruction and unattended replies are outside the current scope.
