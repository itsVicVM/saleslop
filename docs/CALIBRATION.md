# Calibrate replies and search queries

Start with the team’s product notes, current terms and examples of conversations that went well or stalled. Include the rep’s account of what moved the deal forward. Ask the manager about gaps that affect a real decision. A good setup can take several sessions.

Use authorized, de-identified copies in a private team folder. Give the model excerpts approved for that use. Conversations show how buyers talk. Check prices, capabilities and promises against current product material.

## Two rounds of reply review

1. Have the coding agent list facts it can support with a source, decisions a seller must make and claims that need checking. Put approved facts, tone and handoff rules in the client pack. [Telegram prompts](../bots/ops/prompts.py) · [Mail prompts](../bots/mail/prompts.py)
2. Set aside **20–30 cases for each of two rounds**. Split by conversation so the second round stays fresh. Include price requests, competitor objections, interest, refusal and a question the product notes cannot answer. Stop each thread before the rep’s reply. Let the reviewer keep the actual response. Mark invented cases if the team has little history.
3. Prepare **three drafts per case** using the rules for that channel. Where useful, vary the next step: answer directly, ask for one detail or involve a colleague. All three should respect a refusal. Save the case ID, channel, drafts and factual sources. This exercise runs in the coding agent. Check the connected model separately in draft mode.
4. The seller picks a draft or rejects all three, writes the version they would send and explains one edit. For example, “They asked whether we support their CRM. Find out which one they use before suggesting a call.” Group edits by cause. Fix a missing product fact in the pack and a recurring conversational mistake in the playbook.
5. Freeze that version and run the second set. Count how often the first draft worked, another draft worked or all three needed rewriting. Count invented facts and attempts to continue after a refusal separately. Set the acceptance criteria before reviewing. Resolve serious errors before considering automatic replies.

A starting instruction for the coding agent:

> Read the materials I selected. Hold back the second set of conversations until I have reviewed the first. Show five cases at a time. Save my choice, final wording and reason. Propose specific edits to the pack and test them on the held-out cases.

## Compare two sets of search queries

1. Pick companies you would sell to and lookalikes you would reject. Find a page and phrase that identifies the buyer. Check whether vendors, directories and job boards use the same phrase.
2. For one segment, try a category query and a query drawn from buyer-site wording. For example, compare “coffee subscription store” with “choose your coffee delivery frequency.” Label software vendors selling subscriptions *to* stores as false positives for this segment.
3. Hold language, region, provider, result depth and budget constant. Saved results work for a first comparison. Review the first ten unique domains per group and retain the queries that found each one. [Pack fields and commands](CORE_START.md)
4. Mix the domains and hide the query group from the seller. Record fit, miss or insufficient evidence, with a URL, quote and reason. Compare the number of relevant domains, overlap and relevant domains found by only one group. Report the actual denominator if a query returned fewer than ten.
5. Change one condition that explains a recurring miss. Repeat on fresh results, flagging domains already seen. Save the winning queries, date, budget and examples of misses with the hypothesis.

Use the same review cycle for qualification, CRM handoff and reporting. Keep a few borderline examples and the expected decisions. For a report, trace one company to a confirmed outcome and a second one whose outcome is still unknown.
