"""Verify report collection through the same CLI as the daily timer."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


KIT = Path(__file__).resolve().parents[1]


class ReportArchiveTests(unittest.TestCase):
    def test_missing_sheet_range_is_not_an_empty_tracker(self):
        from unittest.mock import Mock
        from jobs.sheets import Sheets

        service = Mock()
        call = service.spreadsheets.return_value.values.return_value.batchGet
        call.return_value.execute.return_value = {"valueRanges": [{"range": "A!A2:B"}]}
        with self.assertRaisesRegex(ValueError, "incomplete Sheets read"):
            Sheets("fixture", service=service).batch_get(["A!A2:B", "B!A2:B"])

    def test_collect_refresh_and_failed_input(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            rows = root / "rows.json"
            archive = root / "reports"
            env = {**os.environ, "KIT_HOME": work, "KIT_REPORT_ROWS_JSON": str(rows),
                   "PYTHONUTF8": "1", "KIT_OUTREACH_STATE_DIR": str(root / "mail")}
            command = [sys.executable, "-B", "runner/kit_env.py", "--pack", "config/pack.template.json",
                       "--module", "jobs", "run", "--", sys.executable, "-B", "-m", "jobs.report",
                       "--day", "2026-11-19", "--archive", str(archive)]

            def collect(expected):
                result = subprocess.run(command, cwd=KIT, env=env, capture_output=True,
                                        encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

            current = ["one.example", "", "", "One", "Interested", "2026-11-13", "", "retail",
                       "2026-11-14", "", "", "", ""]
            older = ["two.example", "", "", "Two", "", "2026-11-12", "", "retail", "",
                     "2026-11-18", "2026-11-18", "2026-11-19", ""]
            rows.write_text(json.dumps({"Operator": [current, older]}))
            collect(0)
            snapshot = archive / "2026-11-19.json"
            data = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertTrue(data["journal_missing"])
            self.assertEqual(data["tracker_source"], "rows-json")
            self.assertEqual(data["weekly_activity"]["start"], "2026-11-13")
            self.assertEqual(data["weekly_activity"]["by_hypothesis"]["retail"],
                             dict(first_touch=1, reply_on=1, meeting_on=1, accepted_on=1,
                                  won_on=1, lost_on=0, outcome_unknown=1))
            current[-1] = "2026-11-19"
            rows.write_text(json.dumps({"Operator": [current, older]}))
            collect(0)
            saved = snapshot.read_bytes()
            self.assertEqual(json.loads(saved)["weekly_activity"]["by_hypothesis"]["retail"]["outcome_unknown"], 0)
            rows.write_text("{}")
            collect(2)
            self.assertEqual(snapshot.read_bytes(), saved)
            self.assertEqual(list(archive.iterdir()), [snapshot])
            rows.write_text(json.dumps({"Pipeline": [current]}))
            preview = command[:-2]
            result = subprocess.run(preview, cwd=KIT, env=env, capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 2)
            self.assertIn("Operator", result.stderr)
            self.assertEqual(snapshot.read_bytes(), saved)

    def test_legacy_columns_leave_unknown_metrics_unavailable(self):
        from datetime import date
        from jobs.report import weekly_activity

        end = date(2026, 11, 19)
        result = weekly_activity([{"first_touch": end.isoformat(), "status": "Interested"}],
                                 {"first_touch": "F"}, end, end)
        counts = result["by_hypothesis"][""]
        self.assertEqual(counts["first_touch"], 1)
        self.assertIsNone(counts["won_on"])
        self.assertIsNone(counts["outcome_unknown"])
