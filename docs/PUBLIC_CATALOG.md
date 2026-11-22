# What the modules do, and why

A rep should be able to follow one company from the search that found it to the next conversation someone promised to have. Use the modules you need along that path.

### Company discovery and qualification

Start with a hypothesis and queries. The search worker returns domains and evidence. The qualification step records a source URL, a quote, a verdict and the role worth contacting. A directory page can repeat every keyword in a good customer’s website. Recording the reason lets the next rep challenge a weak match before anyone spends time finding an address. [Search workflow](CORE_START.md) · [Discovery](../skills/research/bulk-web-discovery-worker/scripts/discovery_worker.py) · [Qualification](../jobs/qualify.py)

### Contact research

Look for business contact details on the company’s site, or search by domain and role through a configured provider. Keep the source and verification status. An MX record tells you a domain receives mail. It leaves open whether the particular buyer’s mailbox exists. The first-draft handoff asks the operator to resolve uncertain addresses. [Public Telegram contacts](../skills/research/telegram-contact-research/scripts/telegram_contact_finder.py) · [Email lookup](../skills/research/telegram-contact-research/scripts/find_emails_bulk.py) · [Handoff](../outreach/handoff.py)

### First touches and follow-ups

Preview a first email, plan a sequence and consult the send journal before scheduling another touch. The Telegram follow-up job checks the tracker and the recorded opener. Mail campaigns respect mailbox budgets, prior attempts and recorded refusals. This is where automation earns its keep: a due follow-up survives a busy week, and a recorded stop survives a change of rep. The team chooses the wording, timing and eligible accounts. [Campaign planner](../outreach/cli.py) · [Telegram follow-ups](../bots/ops/followup.py) · [Company stops](../outreach/company_stop.py)

### Telegram operator bot

Incoming DMs become cards with the conversation and a suggested reply. A rep can edit, send, skip or take over. Someone asking “Will this work with our CRM?” deserves an answer grounded in product facts. Showing the thread beside the draft makes it easier to catch an invented promise or a premature meeting request. Draft review is the starting point. Telegram also offers configurable automatic routes with limits and a human handoff. [Operator modes](../bots/ops/README.md) · [Reply playbook](../skills/ops/dialog-playbook/SKILL.md)

### Email inbox

The mail module turns replies into cards in the same operator chat. It separates bounces and automatic messages from buyer replies. The rep approves each outgoing response. If delivery is uncertain, the card asks the rep to check Sent Items: clicking retry immediately could send the same reply twice. [Mail operations](../bots/mail/README.md)

### Lead assignment and CRM

Distribution assigns ownership by team rules. A CRM adapter offers possible deals, and the operator selects the record before writing a note. Two companies can have similar names. Keeping that one decision with the rep protects the history the rest of the team will rely on. Available actions vary by CRM. [Assignment rules](../jobs/distribute.py) · [CRM adapters](../bots/ops/crm/__init__.py) · [Provider capabilities](INTEGRATIONS.md)

### Reporting and scheduled jobs

The report reconciles activity logs with tracker rows and records outcomes by date. A manager can ask which conversations led to meetings, then trace a number back to its source. A warm reply and a confirmed win are separate events. The server can archive a daily snapshot for a personal assistant or a dashboard. Scheduling, timezone settings and shared lookup budgets make recurring work predictable. [Report](../jobs/report.py) · [Budget ledger](../jobs/budget.py) · [Schedule the archive](SELF_HOST.md)

### Team setup and coaching

The client pack holds product facts, search hypotheses, tone and operating rules. A manager and coding agent can review 20–30 conversations, compare three drafts per case and repeat on fresh examples. A new rep learns from the manager’s edits before taking responsibility for live replies. A checkout installer and offline health check let a second team repeat the setup with its own files. [Calibration method](CALIBRATION.md) · [Onboarding skill](../skills/ops/operator-onboarding/SKILL.md) · [Setup](../install/setup.py)

### Buyer signals in chat exports · experimental

Score messages in a saved Telegram export, then give a rep a shortlist with the phrases that triggered each match. Reviewing the shortlist helps distinguish a buyer asking for help from a seller advertising a similar service. Evaluation to date is offline. Its usefulness on new incoming messages still needs a prospective pilot with seller feedback. [Try the filter](QUICKSTART_PUBLIC.md) · [Evaluator](../monitoring/evaluate.py)
