# saleslop

saleslop is a self-hosted toolkit for B2B sales teams. Start with a description of your buyer and a phrase they might use. The research tools find candidate companies. A rep checks the evidence and chooses whom to contact. The outreach tools prepare messages, keep track of follow-ups and bring replies back to the rep. A daily report puts the send logs and tracker figures side by side.

You can use each part on its own. For example, a team can keep its existing CRM and just run the daily report, or use the reply queue to train a new rep. Email replies need a rep’s approval. In Telegram the team can also enable automatic replies for selected cases and take over a conversation at any time.

## What works today

| Module | What it produces | Validation and remaining checks |
|---|---|---|
| Company and contact research | Candidate domains, evidence for a fit decision, a target role and contact lookup results | The research workflow has been field-tested. Offline tests cover qualification and the handoff to a draft. Check relevance and provider access in each market. |
| Outreach and reply review | A first-touch preview, a sequence with limits and recorded stops, and operator cards for Telegram and email | Outreach and reply review have been field-tested. Local tests exercise planning, approvals and handoff. Verify delivery and account permissions on the target installation. |
| CRM and inbox adapters | amoCRM, HubSpot and Bitrix24 deal operations, plus Gmail, Microsoft 365 and IMAP/SMTP mailbox options | Operations differ by provider. HubSpot, Bitrix24 and Microsoft Graph have offline contract tests. Live validation on a team’s test accounts is pending. [Integration matrix](docs/INTEGRATIONS.md) |
| Reporting and deployment | Daily JSON snapshots and seven-day activity by hypothesis from Sheets, JSON or CSV | Local tests cover collection and archive updates. Check the current timer, restart and report delivery on the target host. |
| Rep onboarding and calibration | A product pack, a dialogue playbook and two rounds of draft review against a team’s examples | The materials and review procedure ship with the kit. An independent team still needs to assess draft quality on held-out conversations. |
| Group-chat signals · experimental | A scored shortlist from a saved Telegram JSON export, with a reason for each match | Offline evaluation only. Usefulness on new messages in day-to-day sales remains unverified. [Try the evaluator](docs/QUICKSTART_PUBLIC.md) |

## Start with one company

From the checkout root, validate the starter pack and inspect its search plan. These commands run offline with Python 3.11+.

```sh
python runner/kit_env.py --pack config/pack.template.json check
python runner/kit_env.py --pack config/pack.template.json discovery-config --hypothesis my-segment
```

Look for `OK` in the first command’s output and `my-segment` in the second. The search terms are placeholders. The [first-search guide](docs/CORE_START.md) takes you from that plan to an email preview with a recipient, a source quote and a target role. You can follow it offline before connecting a mailbox.

Offline previews, chat-export evaluation and file-based reports require no model subscription or API key. AI-drafted replies require model API access. Live research and messaging use the accounts and credentials for the selected providers. Paid lookup budgets start at zero. [Connection requirements](docs/INTEGRATIONS.md) · [OpenAI API billing](https://help.openai.com/en/articles/9039756-managing-billing-for-chatgpt-and-the-api-platform)

## Documentation

- [Module guide](docs/PUBLIC_CATALOG.md) — inputs, outputs, operator decisions and design rationale.
- [Calibration](docs/CALIBRATION.md) — review reply drafts and compare search queries using your own cases.
- [Operating model](docs/OPERATING_MODEL.md) and [server setup](docs/SELF_HOST.md) — deployment options, ownership and scheduled collection.
- [Team reporting](docs/TEAM_OPERATIONS.md) — trace a metric to its journal, tracker row and recorded outcome.
- [Project comparison](docs/COMPARISON.md) and [roadmap](docs/ROADMAP.md) — documented capabilities and planned pilots.
