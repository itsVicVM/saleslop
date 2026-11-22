---
name: operator-onboarding
description: Onboard an operator to an existing kit deployment, check pack readiness and explain the ops bot without launching services or live outreach.
---

# Operator onboarding

For an initial rollout, use the [team calibration guide](../../../docs/CALIBRATION.md). Start from the team's approved materials and cases, then ask about unresolved decisions. Keep review notes outside the checkout.

From the deployment's kit directory run:
`python3 runner/kit_env.py --pack <client-pack> ops-context`
Read `texts.onboarding`. Use the chosen client's pack, never a demo identity as a fallback. Establish the intended operator, host and pack before configuring accounts.

Run the existing validator with the client's local env file:
`python3 runner/kit_env.py --pack <client-pack> --module ops --env-file <local-env-file> check`
Report missing variable names, not values. An offline context or a successful pack check does not prove Telegram, mail or CRM connectivity.

Explain the configured mode: off gives notifications, draft requires the operator to send, auto can send after engine checks. Show how /settings controls account modes, mail and outreach. Operator toggles can only narrow pack permissions. Mail replies have draft/off modes and require Send; no mail auto mode exists.

Use a synthetic dialogue without real accounts to demonstrate a card, a plain-word revision and the separate confirmation after sending. Do not start a listener or send a test to a real contact as part of an offline walkthrough. One process polls each ops-bot token. A Telegram session belongs to one host; do not copy it to another host. Account login and credentials are entered locally by the operator, not in chat or skill files.

For mailbox connection use the installed mail-connect skill when available, otherwise the deployment's bots/mail/README.md. Do not improvise OAuth or password handling. Record readiness as checked / missing / not tested; list the next concrete action. Installation and service management belong to the installer stage, not this skill.
