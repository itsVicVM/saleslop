# Integration and region guide

Pick a connection for the action the seller needs. An Excel export is enough for local reporting. Writing a note back to a deal requires a CRM adapter. Inbox cards currently run inside the Telegram operator bot.

| Action | Available in the checkout | What to verify |
|---|---|---|
| Read mail and review replies | Gmail / Google Workspace via Gmail API, Microsoft 365 work mail via Graph, and IMAP/SMTP via Himalaya | Offline tests use substitute transports. Check account permissions and a real test mailbox. |
| Start an email campaign | SMTP or Himalaya from an account explicitly enabled for outreach | Graph and Gmail API handle cards and approved replies. Provider acceptance alone cannot confirm delivery. |
| Update a deal | amoCRM, HubSpot, Bitrix24 or no CRM | HubSpot and Bitrix24 support search, operator-selected linking and notes. The amoCRM adapter also supports tasks and advancing the entry stage. |
| Collect a daily report | Google Sheets, local JSON rows or UTF-8 CSV from Excel, LibreOffice or a CRM | A team supplying an export owns its refresh. Direct Excel writes or CRM-to-CSV synchronization need additional integration. |
| Search another market | Google results through Serper with language and country parameters, localized queries or an existing candidate file | Request parameters and spend limits were tested offline. Have a local seller assess result quality. |

## Mail

For public-cloud Microsoft 365, add this mailbox to `mail.accounts` in the operator pack and enable `mail.bot.enabled`. Include its ID in `mail.bot.accounts` if that list is configured. [Server setup](SELF_HOST.md)

```json
{"id":"office","address":"sales@example.invalid","env_prefix":"MAIL_OFFICE","provider":"ms_graph","outreach":false,"bot":{"reply":"off"}}
```

An administrator grants the application read and send access for the intended test mailbox. Set `MAIL_OFFICE_TENANT_ID`, `MAIL_OFFICE_CLIENT_ID` and `MAIL_OFFICE_CLIENT_SECRET` in the local environment file. Scope application access in Exchange to the intended mailboxes. The initial pass records the Inbox baseline. Later polls create cards for new mail and send an operator-approved MIME reply. The server stores the sync cursor. The adapter targets organizational mailboxes in Microsoft’s public cloud. Personal Outlook.com and national clouds need separate support. [Application access](https://learn.microsoft.com/en-us/graph/auth-v2-service) · [Exchange application roles](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac) · [MIME sending](https://learn.microsoft.com/en-us/graph/api/user-sendmail)

For Yandex Mail or Zoho, choose `yandex` or `zoho-us-business` explicitly in the connection CLI. In the operator environment run `python -m bots.mail.connect start --addr sales@example.invalid --provider imap --preset yandex`, then use `finish-imap` in the server terminal. The Zoho preset uses hosts for a paid US organization. Check the actual plan and data center. Override a preset in `mail.bot.connect.presets` for different hosts and TLS settings. Test IMAP, SMTP and the Sent folder separately. [Yandex settings](https://yandex.com/support/yandex-360/customers/mail/en/mail-clients/mail-clients-troubleshooting) · [Zoho settings](https://www.zoho.com/mail/help/imap-access.html) · [Mail operations](../bots/mail/README.md)

## CRM

Use one of these objects as the `crm` section of the pack. Keep credentials in the local environment. Deal lookup starts with the lead’s Telegram handle. To match an exact deal property, store the handle without `@` and configure the internal property name as `telegram_field`. The operator selects the deal before a note is written.

```json
{"type":"hubspot","writeback":true,"hubspot":{"token":"env:HUBSPOT_TOKEN","telegram_field":"telegram_handle"}}
```

```json
{"type":"bitrix24","writeback":true,"bitrix24":{"webhook":"env:BITRIX_WEBHOOK"}}
```

HubSpot needs an application token with access to search deals and add associated notes. Bitrix24 needs the full HTTPS incoming webhook URL with CRM permissions. Try two similar deal names and confirm the note lands on the selected record. [HubSpot deal API](https://developers.hubspot.com/blog/a-developers-guide-to-hubspot-crm-objects-deals-object) · [Bitrix24 comments](https://apidocs.bitrix24.com/api-reference/crm/timeline/comments/crm-timeline-comment-add.html)

## Regional search and CSV reporting

Set `targeting.hypotheses[].queries[].backends` to `["serper"]`, provide `SERPER_API_KEY` and set `limits.budgets.serper_daily`. A zero budget prevents paid requests. For a Brazilian Portuguese trial, for example, set `SERPER_COUNTRY=br` and `SERPER_LANGUAGE=pt`. Choose a team timezone such as `America/Sao_Paulo`. Set `reports.language` to `en` or `ru` independently of the search language. [Query calibration](CALIBRATION.md) · [Serper](https://serper.dev/)

Pass `--rows-csv /path/to/tracker.csv` to `jobs.report` or set `KIT_REPORT_ROWS_CSV` for the timer. Choose one input source per run. Use UTF-8 with or without BOM, separated by commas, semicolons or tabs. Required headers are `member` (an ID from `team.members`), `first_touch` and either `company` or `site`. Use YYYY-MM-DD dates. For weekly totals add `hypothesis,reply_on,meeting_on,accepted_on,won_on,lost_on`. One file can contain all reps. A personal assistant can read the resulting JSON. [Schedule the report](SELF_HOST.md)

## Extend the kit

| Change | Entry point | Acceptance case |
|---|---|---|
| Mail host or API | Add an IMAP/SMTP preset to the pack. For a new API implement new-message retrieval, MIME reading and sending in the mail module and register the provider in the validator. | Baseline, duplicate event, cursor after a failed read, uncertain send result. |
| CRM | Implement [CRMAdapter](../bots/ops/crm/base.py), wire the [factory](../bots/ops/crm/__init__.py) and configure the [runner](../runner/kit_env.py). | Candidate list, human confirmation, a note on the selected deal, API failure. |
| Search engine | Register a [worker](../skills/research/bulk-web-discovery-worker/scripts/discovery_worker.py), its runner name and a budget reservation for a paid request. | URL and source retained, region, empty results, zero budget. |
| Tracker | Start with CSV or JSON. For direct reads, add a row source before calculation in [jobs/report.py](../jobs/report.py). | Equal counts for equivalent inputs and a visible difference between missing data and zero. |

Keep an offline contract test and record live validation on a dedicated test account separately. [Roadmap](ROADMAP.md)
