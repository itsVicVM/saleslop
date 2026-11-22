# Find buying signals in a chat export

Export one chat as JSON from Telegram Desktop. Keep `result.json` in a separate `my-sales` folder next to the checkout. Create a UTF-8 `profile.json` in that folder. Use `name` for the hypothesis, `positive_terms` for topic phrases, `intent_terms` for ways a buyer asks for help and `negative_terms` for common false positives. Each phrase list is an array of strings.

For example, a rep looking for teams choosing a CRM could start with this profile.

```json
{"name":"CRM buyers","positive_terms":["CRM"],"intent_terms":["looking for","can anyone recommend"],"negative_terms":["we sell"],"min_score":3}
```

“Can anyone recommend a CRM?” earns one point for the topic and two for intent. A score of three qualifies. An exclusion subtracts three points. Each category counts once per message. Run the evaluator from the checkout root.

```sh
python -m monitoring.evaluate --export ../my-sales/result.json --profile ../my-sales/profile.json --output ../my-sales/review.json
```

Open `signals` in `review.json`. Use `message_id` to find the original and `reasons` to see which phrases matched. The default limit is 50 cards. Add `--limit 100` for a larger queue. If the queue is empty, look up a message you would have handed to a rep and compare its wording with your profile.

Save a copy before adding labels. Set each card’s `review_label` to one of the values in `review_labels`. A successful rerun replaces the report, including any labels edited there. The evaluator refuses to write over the export or profile. The report contains participants’ message text, so keep it with the team’s working files.
