"""Exercise the public user-pack path without contacting services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicCoreTests(unittest.TestCase):
    def test_hypothesis_to_first_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            pack = work / "pack.json"
            shutil.copyfile(ROOT / "config/pack.template.json", pack)
            shutil.copyfile(ROOT / "config/first-touch.txt", work / "first-touch.txt")
            env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
            env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", KIT_HOME=str(work))

            def run(*args):
                result = subprocess.run([sys.executable, "-B", *args], cwd=ROOT, env=env,
                                        capture_output=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            run("runner/kit_env.py", "--pack", pack, "check")
            plan = run("runner/kit_env.py", "--pack", pack, "--module", "jobs", "run", "--",
                       sys.executable, "-m", "jobs.chain", "--pack", pack,
                       "--hypothesis", "my-segment", "--dry-run")
            self.assertIn("validation_worker.py", plan)
            rows = work / "rows.json"
            rows.write_text(json.dumps({"Operator": []}), encoding="utf-8")
            report = run("runner/kit_env.py", "--pack", pack, "--module", "jobs", "run", "--",
                         sys.executable, "-m", "jobs.report", "--rows-json", rows, "--day", "2026-11-19")
            self.assertIn("19.11", report)
            self.assertIn("Operator", report)
            cron = run("runner/kit_env.py", "--pack", pack, "--env-file", work / "client.env",
                       "cron", "--format", "json")
            job, = json.loads(cron)["jobs"]
            self.assertFalse(job["enabled"])
            self.assertEqual(job["schedule"]["expr"], "0 9 * * *")
            run("runner/kit_env.py", "--pack", pack, "discovery-config", "--hypothesis",
                "my-segment", "--out", work / "config.json")
            config = json.loads((work / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["run_id"], "my-segment")
            domain = "buyer.invalid"
            candidate = {"domain": domain, "sample_url": "https://" + domain, "validation_status": "verified"}
            (work / "candidates.jsonl").write_text(json.dumps(candidate), encoding="utf-8")
            run("-m", "jobs.qualify", "queue", "--pack", pack, "--run-dir", work)
            accepted = work / "accepted.json"
            run("-m", "jobs.qualify", "record", "--packet", work / "qualification.json",
                "--out", accepted, "--domain", domain, "--company", domain, "--source", candidate["sample_url"],
                "--evidence", "buyer requirement", "--reason", "fits profile", "--role", "billing",
                "--verdict", "merchant", "--accept")
            run("-m", "outreach.handoff", "prepare", accepted, "--domains", work / "domains.txt")
            self.assertEqual((work / "domains.txt").read_text().strip(), domain)
            contacts = work / "emails.json"
            entry = {"contacts": [{"email": "billing@" + domain}], "reoon": {"status": "valid"}}
            contacts.write_text(json.dumps({domain: entry}), encoding="utf-8")
            preview = run("-m", "outreach.handoff", "preview", accepted, "--contacts", contacts,
                          "--pack", pack, "--standalone", "--now", "2026-01-06T12:00:00+00:00")
            self.assertIn(domain + " — [reason for reaching out]", preview)
