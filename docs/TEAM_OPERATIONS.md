# Review replies and the day’s results

Pick one company a rep worked on. Ask them to show the page that justified outreach, the first message, the buyer’s reply and the tracker row. Walking the same case through all four often exposes the problem: the wrong buyer, the wrong person, or a promised next step nobody recorded.

## Where the figures come from

| Question | Source |
|---|---|
| Who received a Telegram opener or follow-up, and who replied? | `KIT_HOME/outreach/send_log.jsonl` on the rep’s server |
| Which email attempts and bounces were recorded? | `KIT_OUTREACH_STATE_DIR/journal.jsonl` |
| Who owns the company and what did they agree to do? | Member tab from `team.members[].tracker_tab`. Column letters come from `sheets.columns` and status labels from `reports.statuses`. |

The [daily report](../jobs/report.py) defaults to yesterday in the team timezone. If the tracker shows a first touch absent from the journal, inspect one row. The rep may have sent manually, or the report may be reading another host. Agree on what counts as “warm.” Only record a win when the team’s agreed evidence exists.

Reconcile one day by hand before scheduling the archive or delivery to a chat. [Scheduling and collection](SELF_HOST.md)

## Why the bot suggested a particular reply

A buyer writes, “We already have a supplier. Can you integrate with our CRM?” A rep could spend the whole reply debating the supplier and miss the actual question. The assistant sees the thread and product facts. If the integration is documented, it can answer directly. If the CRM is unnamed, the rep can ask which one they use or hand the question to someone who knows.

Have the rep explain an edit: “I took out the invitation to a call. We owe them an answer about their CRM first.” In Telegram, the configured route decides whether the draft goes to an operator. Manual takeover pauses automatic replies. Email requires operator approval to send. [Prompts](../bots/ops/prompts.py) · [Dialogue playbook](../skills/ops/dialog-playbook/SKILL.md) · [Two review rounds](CALIBRATION.md)

## Ask a personal assistant for a weekly summary

> Read the latest archived report. Give its date and data source. Use its weekly_activity block to show first touches, replies, meetings, accepted leads and confirmed wins per hypothesis. Show how many new leads have an unknown outcome. Check which daily files are missing from the past week.

In the starter pack, H–M are the hypothesis and dates for first reply, meeting held, client acceptance, win and loss. Map your own columns or leave unsupported fields unmapped. The owner enters an event date when it occurs. Agree with the client on evidence of a win, such as payment.

One tracker row represents one opportunity. The block counts dated events in the seven-day window. A deal first contacted last month can be won this week. “New with unknown outcome” counts rows first touched in the window with no recorded win or loss by its end. A dash means the column is unavailable. Zero means the mapped column contains no dated event in that window. Follow up on missing information. Adjacent snapshots overlap, so pick the one ending on the desired date. [Report archive](SELF_HOST.md)

## Review several reps or hosts

A shared spreadsheet can have one tab per rep. A manager with read access can reconcile a row against the relevant rep’s journal. Each report generator reads logs on its own host. Collect the individual reports for a cross-host view.

A team can keep a shared feedback tab with the buyer’s question, the proposed draft, the seller’s edit and the missing fact or rule. Assign someone to review entries and update the playbook. Cross-server administration and automatic sharing of approved changes are still manual workflows.
