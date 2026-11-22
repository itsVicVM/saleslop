# Build a first search for your product

Pick a customer who recently paid. Ask the rep what the buyer needed and what someone could have spotted on the company’s website before that first conversation. Use one segment, one observable signal and one starting query. [Test two sets of queries](CALIBRATION.md)

## Prepare your pack

From the checkout root, create a sibling folder called `my-sales`. Copy the [pack template](../config/pack.template.json) to `../my-sales/pack.json` and the [first-touch template](../config/first-touch.txt) to `../my-sales/first-touch.txt`. Keep working data outside the checkout.

Under `targeting.hypotheses` edit `text` (who might buy and why), `relevance_terms` (phrases you expect on a buyer’s site) and `queries[0].text` (what you would search for yourself). Keep the ID `my-segment` for the commands below. Use `python3` if that is the interpreter name on your system.

```sh
python runner/kit_env.py --pack ../my-sales/pack.json check
python runner/kit_env.py --pack ../my-sales/pack.json discovery-config --hypothesis my-segment --out ../my-sales/run/config.json
```

The first command should print `OK`. Read the query in `../my-sales/run/config.json`. At this point you can still change your mind without making a network request.

## From a query to a draft

Set `KIT_HOME` to the absolute path of the working directory. In PowerShell use `$env:KIT_HOME = (Resolve-Path ../my-sales).Path`. In Bash use `export KIT_HOME="$(cd ../my-sales && pwd)"`. Preview the discovery job.

```sh
python runner/kit_env.py --pack ../my-sales/pack.json --module jobs run -- python -m jobs.chain --pack ../my-sales/pack.json --hypothesis my-segment --dry-run
```

The dry run prints the proposed steps. Continue below with sample results. When you are ready to search live, choose a backend, configure its credentials and set its budget. The starter pack names `keenable` as an example. A live chain writes the qualification queue to `KIT_HOME/runs/my-segment/qualification.json`.

Save the following single line as `../my-sales/run/candidates.jsonl`. It stands in for one search result. With a real result, open the source page and decide whether the quote gives you a reason to contact the company. The next two commands record that decision and the role to look for.

```json
{"domain":"buyer.example","sample_url":"https://buyer.example/request","validation_status":"verified","page_excerpt":"We are evaluating a billing provider."}
```

```sh
python -m jobs.qualify queue --pack ../my-sales/pack.json --run-dir ../my-sales/run
python -m jobs.qualify record --packet ../my-sales/run/qualification.json --out ../my-sales/accepted.json --domain buyer.example --company Buyer --source https://buyer.example/request --evidence "We are evaluating a billing provider." --reason "Explicit buying request" --role "Billing lead" --verdict merchant --accept
```

Edit the subject and body in `first-touch.txt` and the sender name in `mail.accounts` to see your own wording in the preview. Leaving the placeholders in place also works for the walkthrough. Prepare the domain for [email lookup](../skills/research/telegram-contact-research/scripts/find_emails_bulk.py).

```sh
python -m outreach.handoff prepare ../my-sales/accepted.json --domains ../my-sales/domains.txt
```

Save the contact result to `../my-sales/emails.json` or reuse an existing provider result. Each domain entry contains `contacts` with an `email` field. The historical key `reoon.status` stores verification status. The actual source is recorded in `reoon.source` and can, for example, be `findymail`.

After a live lookup, read `verification_statuses` in the command summary. A `valid` verdict qualifies an address for preview. A domain with an MX record can receive mail, but `mx_only` leaves the individual mailbox unconfirmed.

For the offline walkthrough, use the following contents for `emails.json`. The test value `valid` simulates a provider's verification result.

```json
{"buyer.example":{"contacts":[{"email":"billing@buyer.example"}],"reoon":{"status":"valid","source":"fixture"}}}
```

```sh
python -m outreach.handoff preview ../my-sales/accepted.json --contacts ../my-sales/emails.json --pack ../my-sales/pack.json --standalone
```

Check who would receive the message, which fact supports the opener and what the recipient could reasonably answer. An unconfirmed address requires review. Standalone preview uses an empty journal and sends nothing. A live campaign must consult its own journal for previous touches and refusals.
