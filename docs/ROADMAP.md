# Roadmap

Priorities as of November 20, 2026. Each channel starts with one seller workflow and a way to check its outcome. Pilot results will determine the schedule.

| Order | First workflow | Pilot acceptance |
|---|---|---|
| Now · stack compatibility | Keep the team’s mail host, CRM, tracker and search region. New adapters have offline contract tests. | Receive a test message, approve the reply, add a note to the selected deal and read a fresh report after restart. Record the tested combination in the [integration matrix](INTEGRATIONS.md). |
| Next · LinkedIn | The seller chooses a profile, reviews the reason for contact and a draft, opens LinkedIn, sends manually and records the outcome. | Work through a small list, correct poor drafts and reconcile the results. This pilot uses manual actions on LinkedIn. [LinkedIn rules](https://www.linkedin.com/help/linkedin/answer/a1341543) |
| Then · WhatsApp Business | A buyer messages the business account and an operator approves the response. Business-initiated messages need opt-in and an approved template. | On a test number, check duplicate inbound events, an operator reply within the 24-hour window, the template path after that window and an opt-out. [WhatsApp policy](https://business.whatsapp.com/policy) |
| On request · X, Reddit, Facebook | Possible needs include a signal from a discussion, a public reply or an inbound message to a business page. | Bring a concrete example, an action and an owner. Verify the permitted API and app access for that action before choosing a pilot. |

For a new integration, describe the service, what the rep does and the expected result. “After an email reply, the rep chooses a deal and sees a note in its history” gives a maintainer enough to check whether configuration or an adapter is needed.
