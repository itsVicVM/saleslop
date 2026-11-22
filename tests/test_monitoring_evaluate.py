import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from monitoring.evaluate import evaluate_export
from monitoring.signals import SignalProfile


class MonitoringEvaluateTests(unittest.TestCase):
    def test_cli_preserves_inputs_and_last_good_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export = root / "result.json"
            profile = root / "profile.json"
            report = root / "review.json"
            export.write_text('{"messages":[{"id":4,"type":"message","text":"Looking for CRM"}]}', encoding="utf-8")
            profile.write_text('{"positive_terms":["CRM"],"intent_terms":["looking for"]}', encoding="utf-8")

            def run(output):
                return subprocess.run([sys.executable, "-B", "-m", "monitoring.evaluate",
                                       "--export", export, "--profile", profile, "--output", output],
                                      cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                      encoding="utf-8", timeout=20)

            self.assertEqual(run(report).returncode, 0)
            self.assertEqual(json.loads(report.read_text())["signals"][0]["message_id"], 4)
            alias = root / "linked.json"
            os.link(export, alias)
            for target in (export, profile, alias):
                with self.subTest(target=target):
                    before = target.read_bytes()
                    self.assertEqual(run(target).returncode, 2)
                    self.assertEqual(target.read_bytes(), before)
            before = report.read_bytes()
            profile.write_text('{"positive_terms":"CRM"}', encoding="utf-8")
            result = run(report)
            self.assertEqual(result.returncode, 2)
            self.assertIn("positive_terms", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(report.read_bytes(), before)

    def test_report_contains_reviewable_signal_cards(self):
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
        path = Path(handle.name)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        with handle:
            json.dump(
                {
                    "name": "Test chat",
                    "type": "private_supergroup",
                    "id": 42,
                    "messages": [
                        {"id": 1, "type": "message", "date": "2026-11-17T10:00:00", "from": "A", "text": "Looking for a CRM", "text_entities": []},
                        {"id": 2, "type": "service", "date": "2026-11-17T10:01:00", "actor": "System", "text": "", "text_entities": []},
                    ],
                },
                handle,
            )
        result = evaluate_export(path, SignalProfile("crm", intent_terms=("looking for",), min_score=2))
        self.assertEqual(result["source"]["ordinary_messages"], 1)
        self.assertEqual(result["summary"]["signals"], 1)
        self.assertIsNone(result["signals"][0]["review_label"])
        self.assertEqual(result["review_labels"][0], "good")
