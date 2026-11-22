---
name: crm-telegram-operations
description: Operate Telegram lead cards and confirmed CRM links in the kit; handle human takeover, outreach pauses and integration handoffs.
---

# CRM and Telegram operations

From the deployment's kit directory load:
`python3 runner/kit_env.py --pack <client-pack> ops-context`
Read `texts.handoff`, crm_type, protected_columns and columns. Use bots/ops/README.md in this deployment for supported operations.

Identify the exact account and contact/card before changing anything. If several cards await revision, reply to the intended card. Plain-word edits without a card are appropriate only when one card is pending. Sending is a distinct action; report the bot's confirmation, not merely that a draft exists.

For human takeover, use the existing conversation and account settings. A manual message suppresses auto replies to that contact for the configured interval. Setting mode off affects replies; the outreach switch affects new messages and follow-ups. These are distinct controls. Do not edit state JSON manually or bypass FloodWait, caps, outreach restrictions or an uncertain delivery outcome.

If crm_type is none, state that CRM integration is disabled and prepare a handoff summary without claiming a CRM write. With amocrm, linking to a deal requires the operator's explicit selection on the card; do not guess from names or automatically create/link a deal. Notes, stage advancement and tasks follow the pack's adapter flags after sending. Confirm actual results; a Telegram send does not prove CRM success. Resolve an ambiguous write before retrying it.

For integration handoff, include only known need, stage, agreed next step, owner and unresolved questions, using the pack wording. Do not put credentials in summaries. Group-chat allowlists, group-to-deal linking, history backfill and CRM reconciliation are not implemented in v1; describe the limitation instead of inventing commands.

For tracker updates use pack column mappings and existing tools. Research must not overwrite protected service columns; owner assignment belongs to the distribution job, not contact research. Do not move rows between tabs or write rectangular ranges spanning formula/service columns. Verify any authorized write by reading its result.
