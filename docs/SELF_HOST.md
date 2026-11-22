# Install saleslop on a team server

Start by producing a search plan and a report for one day. Connect live accounts once the team knows whom it wants to find, who will review replies and where the manager will read the report.

## 1. Prepare the checkout

Use Linux with Python 3.11+ and `venv`. The service and timer examples use systemd. The commands assume the checkout lives at `/srv/sales/kit` and the service user is `leadgen`. Substitute your user in both the shell commands and the installer’s `--user` argument. An administrator can create the user and directory as follows.

```bash
sudo useradd --system --create-home --home-dir /var/lib/leadgen leadgen
sudo install -d -o leadgen -g leadgen -m 750 /srv/sales
```

Place the checkout at `/srv/sales/kit`, with the repository’s README directly at `/srv/sales/kit/README.md`. If you downloaded the ZIP, rename its extracted top-level directory to `kit`. Continue as the service user with `sudo -u leadgen -H bash`.

```bash
cd /srv/sales/kit
python3 install/setup.py --root /srv/sales --user leadgen
python3 install/setup.py --root /srv/sales --user leadgen --apply
python3 install/healthcheck.py --root /srv/sales
```

The first setup command prints its plan. The second creates `config/pack/`, a `state/` directory for working data and service definitions in `systemd/`. Repeating setup lists existing files under `keep`. Look for `"ok": true` in the health check. It checks the local configuration, state directory, bot unit, report service and timer files. The first scheduled report is checked in step 3. Install dependencies into a virtual environment with access to the Python package index.

```bash
python3 -m venv /srv/sales/.venv
/srv/sales/.venv/bin/python -m pip install -r install/requirements.txt
/srv/sales/.venv/bin/python -m unittest discover -s tests
```

## 2. Validate the settings and first report

Edit `/srv/sales/config/pack/pack.json` following the [first-search guide](CORE_START.md). Edit `first-touch.txt` next to it. You can also bring an existing pack with its referenced files. Create the local environment file once.

```bash
cd /srv/sales
umask 077
cp -n config/client.env.example config/client.env
cd kit
P=/srv/sales/.venv/bin/python
"$P" runner/kit_env.py --pack ../config/pack check
"$P" runner/kit_env.py --pack ../config/pack discovery-config --hypothesis my-segment --out ../state/run/config.json
```

Read `../state/run/config.json` and check the actual query. For an offline report, write `{"Operator":[]}` to `../state/rows.json`. Use the display name from your pack if you renamed the member.

```bash
"$P" runner/kit_env.py --pack ../config/pack --module jobs --env-file ../config/client.env run -- "$P" -m jobs.report --rows-json ../state/rows.json
```

You should see the date, the rep’s name and counts. For live Sheets data, configure `sheets.spreadsheet_id`, member tabs and column letters. The Sheets reader requires the external `google_api` module from your Google Workspace runtime, `KIT_GOOGLE_API_DIR` and server-side Google authorization. Run the report again without `--rows-json` and reconcile one company. Teams with their own tracker can supply current JSON rows or a [CSV export](INTEGRATIONS.md).

## 3. Schedule a daily report

Choose where the manager reads the report. If the team uses the Hermes scheduler, set the time, recipient and `enabled: true` on `daily-sales-report` under `jobs.list`. Verify the server timezone. The command below prints the job specification for registration.

```bash
"$P" runner/kit_env.py --pack /srv/sales/config/pack --env-file /srv/sales/config/client.env cron --format json
```

To return text to the scheduler and keep an archive, set `KIT_REPORT_ARCHIVE_DIR=/srv/sales/state/reports` in `config/client.env`. For a standalone archive, the installer prepares a systemd service and timer. Run the collection command manually as the service user first.

```bash
"$P" runner/kit_env.py --pack ../config/pack --module jobs --env-file ../config/client.env run -- "$P" -m jobs.report --archive ../state/reports
```

The default source is Sheets. For local rows, set `KIT_REPORT_ROWS_JSON` or `KIT_REPORT_ROWS_CSV` to an absolute path in `client.env`. The team owns the refresh of that export. Reconcile a touch and an outcome with the tracker. Open `state/reports/YYYY-MM-DD.json`. The filename uses the report day in the team timezone. Its content includes collection time, source and the seven-day activity grouped by hypothesis. A successful rerun of the same day replaces that snapshot.

An administrator can enable the timer after the manual check.

```bash
sudo install -m 644 /srv/sales/systemd/leadgen-report.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now leadgen-report.timer
systemctl list-timers leadgen-report.timer
```

The timer runs at 09:00 server time. After the first scheduled run, check for the expected date and inspect `journalctl -u leadgen-report.service` for errors. After downtime systemd runs one missed invocation. Compare filenames to spot longer gaps. You can recalculate a specific day with `--day YYYY-MM-DD` using the tracker as it stands at collection time. Choose one scheduler per archive. The manager’s assistant needs read access to the report directory.

## 4. Connect Telegram replies

Prepare a Telegram account for conversations, a separate operator bot, an operator chat and model access. Set `OPS_BOT_TOKEN`, `OPS_OWNER_CHAT_ID` and `OPENAI_API_KEY` in `config/client.env`. Set `company.name` and add `"telegram": {"accounts": ["primary"]}` to the chosen member in `team.members`. Add this section at the top level of the pack.

```json
{"bot": {
  "ops": {
    "member": "operator", "accounts": ["primary"], "mode": "draft",
    "bot_token": "env:OPS_BOT_TOKEN", "owner_chat": "env:OPS_OWNER_CHAT_ID",
    "reply_categories": ["question", "objection", "decline", "other"]
  },
  "sender": {"outreach_disabled": ["primary"]}
}}
```

Set `offer.facts_files` to files containing your terms and product answers, relative to the pack directory. In draft mode a rep chooses which replies to send. Set up the Telegram session on the host that will run the bot. The interactive login prompts for credentials in the terminal.

```bash
cd /srv/sales/kit
export KIT_HOME=/srv/sales/state
P=/srv/sales/.venv/bin/python
"$P" -m bots.ops.sender init --name primary
"$P" -m bots.ops.sender login --name primary --interactive
"$P" runner/kit_env.py --pack ../config/pack --module ops --env-file ../config/client.env check
"$P" install/healthcheck.py --root /srv/sales --configured
```

After the local checks, return to the administrator shell. Only one process should poll the operator bot token. The following commands connect the live bot.

```bash
sudo install -m 644 /srv/sales/systemd/leadgen-ops.service /etc/systemd/system/leadgen-ops.service
sudo systemd-analyze verify /etc/systemd/system/leadgen-ops.service
sudo systemctl daemon-reload
sudo -u leadgen touch /srv/sales/config/ENABLE_LIVE
sudo systemctl enable --now leadgen-ops.service
sudo systemctl status leadgen-ops.service
```

From a second team account send a DM to `primary`. Confirm the rep can see it and edit the reply. Check `sudo journalctl -u leadgen-ops.service -n 50` if it fails. Stop the bot with `sudo systemctl disable --now leadgen-ops.service`. Its report timer is separate. [Mail setup](../bots/mail/README.md) · [Operator modes and CRM](../bots/ops/README.md)

Before an upgrade, back up `config/` and `state/`, pause the bot and scheduled jobs, update the checkout and dependencies, then validate configuration and a report before restarting. The installer preserves existing files. When changing users or paths, compare the installed unit against the new template.
