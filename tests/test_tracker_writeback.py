"""Check tracker acknowledgements using a local Sheets stand-in."""
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/research/telegram-contact-research/scripts"
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    import write_tracker_results as tracker


class WritebackTests(unittest.TestCase):
    def run_write(self, readback):
        api = Mock()
        values = api.build_service.return_value.spreadsheets.return_value.values.return_value
        values.get.return_value.execute.return_value = {"values": [["buyer.example"]]}
        values.batchGet.return_value.execute.return_value = {"valueRanges": readback}
        args = ["write", "--sheet-id", "fixture", "--tab", "Rep's tracker", "--telegram-csv", "unused",
                "--email-json", "unused", "--col-site", "A", "--col-telegram", "B", "--col-email", "C"]
        out = io.StringIO()
        with (patch.dict(sys.modules, google_api=api), patch.object(sys, "argv", args),
              patch.object(sys, "path", list(sys.path)), patch.object(tracker.time, "sleep"),
              patch.object(tracker, "load_telegram_results", return_value={"buyer.example": "@fixture"}),
              patch.object(tracker, "load_email_results", return_value=({}, {})),
              redirect_stdout(out), redirect_stderr(io.StringIO())):
            status = tracker.main()
        self.assertEqual(values.batchUpdate.call_args.kwargs["body"]["valueInputOption"], "RAW")
        self.assertEqual(values.batchGet.call_args.kwargs["ranges"], ["'Rep''s tracker'!A2:C2"])
        return status, json.loads(out.getvalue())

    def test_verified_contact_and_domain(self):
        status, result = self.run_write([{"values": [["buyer.example", "@fixture"]]}])
        self.assertEqual((status, result["cells_written"]), (0, 1))

    def test_incomplete_and_reassigned_rows_are_not_acknowledged(self):
        for readback in ([], [{"values": [["different.example", "@fixture"]]}]):
            with self.subTest(readback=readback):
                status, result = self.run_write(readback)
                self.assertEqual(status, 1)
                self.assertFalse(result["ok"])
