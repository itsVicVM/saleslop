"""Offline checks for the documented server layout and terminal login."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
from bots.ops import sender


class PublicDeploymentTests(unittest.TestCase):
    def test_checkout_setup_and_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            kit = root / "kit"
            for source in ("install/setup.py", "install/healthcheck.py", "install/ops.service.in",
                           "install/report.service.in", "install/report.timer.in",
                           "runner/kit_env.py", "config/pack.template.json", "config/first-touch.txt"):
                target = kit / source
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(KIT / source, target)

            def run(script, *args, expect=0):
                p = subprocess.run([sys.executable, "-B", script, "--root", root, *args], cwd=kit,
                                   capture_output=True, encoding="utf-8", timeout=30)
                self.assertEqual(p.returncode, expect, p.stdout + p.stderr)
                return json.loads(p.stdout)

            plan = run("install/setup.py")
            self.assertFalse((root / "state").exists())
            self.assertIn("config/pack/pack.json", plan["create"])
            run("install/setup.py", "--apply")
            self.assertTrue(run("install/healthcheck.py")["ok"])
            self.assertFalse(run("install/healthcheck.py", "--configured", expect=1)["ok"])
            unit = (root / "systemd/leadgen-ops.service").read_text()
            self.assertIn(f"WorkingDirectory={kit.as_posix()}", unit)
            self.assertNotIn("@ROOT@", unit)
            report_unit = (root / "systemd/leadgen-report.service").read_text()
            self.assertIn(f"-m jobs.report --archive {root.as_posix()}/state/reports", report_unit)
            self.assertIn("User=leadgen", report_unit)
            self.assertIn("Persistent=true", (root / "systemd/leadgen-report.timer").read_text())
            pack = root / "config/pack/pack.json"
            data = json.loads(pack.read_text(encoding="utf-8"))
            data["company"]["name"] = "operator customization"
            pack.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(run("install/setup.py", "--apply")["create"], [])
            self.assertEqual(json.loads(pack.read_text()), data)
            self.assertFalse((root / "config/ENABLE_LIVE").exists())
            self.assertFalse((root / "config/client.env").exists())
            (root / "systemd/leadgen-report.timer").unlink()
            missing_timer = run("install/healthcheck.py", expect=1)
            self.assertFalse(missing_timer["checks"]["report_timer_rendered"])
            (pack.parent / "pack.yaml").write_text("company: [", encoding="utf-8")
            self.assertFalse(run("install/healthcheck.py", expect=1)["checks"]["pack_structure"])

    def test_interactive_login_uses_terminal_and_closes_session(self):
        client = AsyncMock()
        with patch.object(sender.sys.stdin, "isatty", return_value=True), \
             patch.object(sender, "load_config", return_value={}), \
             patch.object(sender, "get_client", return_value=client) as get_client:
            sender.main(["login", "--name", "primary", "--interactive"])
        get_client.assert_called_once_with("primary", {}, allow_copy=False)
        client.start.assert_awaited_once()
        client.disconnect.assert_awaited_once()
