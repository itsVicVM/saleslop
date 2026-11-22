# How a team runs saleslop

The manager owns targeting and reply quality. A rep owns the conversation. The server runs approved jobs and keeps the journals. One person can cover both roles, and a team can start with just research, an inbox or reporting.

| Handoff | What the next person gets |
|---|---|
| Manager to researcher | A segment, reviewed queries and a list of domains with sources. Qualification adds a quote, a decision and a contact role. [Search](CORE_START.md) |
| Researcher to rep | A public Telegram contact or email with its source and verification status. Uncertain addresses go back for review. [Module guide](PUBLIC_CATALOG.md) |
| Rep to buyer | The campaign checks previous touches and account limits. A reply brings up the thread and a draft. The rep edits or takes over. [Telegram](../bots/ops/README.md) · [Email](../bots/mail/README.md) |
| Rep to manager | The owner records the next step and confirmed outcome. The manager can trace a report entry back to the tracker and journal. [Reporting](TEAM_OPERATIONS.md) |

## Where to run it

| Deployment | How it works | Who operates it |
|---|---|---|
| Company VPS, cloud VM or always-on internal Linux server | One instance, local configuration and state, an operator service and a separate report timer. Live channels need outbound access to their providers. | An administrator installs services. A sales manager configures the product and channels. |
| Dedicated client VPS managed by a service provider | Use the same installation with a separate instance, accounts, secrets and archive for each client. | The provider operates the instance and delivers the agreed reports. |
| One VPS per rep | Each host has its own instance and Telegram sessions. A shared sheet can have one tab per rep. Each host produces its own logs and archive. | The team arranges read access and collects reports across hosts. |
| Manager’s workstation | Prepare the pack, preview a search, review drafts, run offline checks and inspect reports. | The manager uses a coding agent as needed. Scheduled jobs need an available host. |

The documented server route uses Linux, Python and systemd. Hosting several clients in a shared instance would require tenant isolation and authorization. A custom dashboard can read the JSON archive. A personal assistant can read the same files. [Installation](SELF_HOST.md)

## What the manager does with a coding agent

A coding agent is an AI tool that can read project files, propose edits and run checks at someone’s direction. Give it the checkout and a private folder of approved product material, de-identified conversations, settings and seller feedback. The manager decides which changes reach the working pack.

Suppose the bot keeps asking for a call too early. Find those cases, revise the rule and have a rep review another 20–30 conversations. This changes the pack and dialogue instructions. Model-weight training is outside this workflow. Cross-host administration, automatic access grants and automatic distribution of approved rules would need additional implementation. Teams can already keep a shared review sheet and apply approved edits manually. [Calibration rounds](CALIBRATION.md)

## Evidence to check before a pilot

| Area | Evidence available | Test with your team |
|---|---|---|
| Discovery, contacts, outreach and replies | These workflows have been field-tested. Offline tests cover qualification, first drafts and send limits in the portable kit. | Relevance in your segment, provider access and a test conversation in each chosen channel. |
| Team ownership and CRM | Assignment rules, operator controls, journals and CRM adapters ship with the kit. | Field mapping, permissions, repeated events and a note in the correct deal. |
| Deployment and reporting | Local checks cover the archive command, weekly figures, retries and preserving a snapshot after input failure. | Restart, timer, a current source of rows, assistant access and a missing day. |
| Calibration | The kit describes a two-round draft review and a comparison of two query sets. | Check factual accuracy and held-out cases. Ask a second rep to explain a decision. |
| Group-chat signals | Offline evaluation covers chat exports. | Have a seller rate new signals in a prospective pilot. |
