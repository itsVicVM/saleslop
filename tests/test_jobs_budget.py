"""Shared API budget (kit/jobs/budget.py) and its use in the search skills. No network: paid calls are refused
before a request, and a stub httpx makes any request that slips through fail loudly."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
from jobs import budget  # noqa: E402

DISCOVERY = KIT / "skills" / "research" / "bulk-web-discovery-worker" / "scripts"
CONTACTS = KIT / "skills" / "research" / "telegram-contact-research" / "scripts"
HTTPX_STUB = textwrap.dedent('''
    """Test stub: any HTTP request through httpx is a test failure."""
    def _fail(*a, **k):
        raise AssertionError("network call attempted: " + repr(a[:1]))
    get = post = _fail
    class HTTPError(Exception):
        pass
''')


def fixed_clock(iso):
    return lambda: datetime.fromisoformat(iso)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "budget" / "ledger.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reserve_until_exhausted(self):
        led = budget.Ledger(self.path, {"tavily": 2}, "UTC")
        self.assertEqual(led.reserve("tavily"), 1)
        self.assertEqual(led.reserve("tavily"), 0)
        with self.assertRaises(budget.BudgetExhausted) as cm:
            led.reserve("tavily")
        self.assertEqual((cm.exception.used, cm.exception.limit), (2, 2))
        self.assertEqual(led.status()["providers"]["tavily"], {"used": 2, "limit": 2, "left": 0})

    def test_unknown_provider_refused(self):
        led = budget.Ledger(self.path, {"tavily": 5}, "UTC")
        with self.assertRaises(budget.BudgetExhausted):
            led.reserve("apollo")
        self.assertFalse(self.path.exists())   # nothing booked

    def test_damaged_ledger_stops_booking_without_replacement(self):
        self.path.parent.mkdir()
        for content in ("{", "[]", "{}", '{"days":[]}', '{"days":{"today":{"tavily":-1}}}'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(budget.BudgetExhausted):
                    budget.Ledger(self.path, {"tavily": 5}).reserve("tavily")
                self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_zero_budget_is_off(self):
        with self.assertRaises(budget.BudgetExhausted):
            budget.Ledger(self.path, {"findymail": 0}).reserve("findymail")

    def test_day_boundary_in_pack_timezone(self):
        # 23:30 UTC is already the next day in Europe/Berlin (UTC+1 in winter)
        late = budget.Ledger(self.path, {"tavily": 1}, "Europe/Berlin", clock=fixed_clock("2026-11-02T23:30:00+00:00"))
        self.assertEqual(late.day(), "2026-11-03")
        late.reserve("tavily")
        earlier = budget.Ledger(self.path, {"tavily": 1}, "Europe/Berlin", clock=fixed_clock("2026-11-02T20:00:00+00:00"))
        earlier.reserve("tavily")   # a different local day: its own budget
        with self.assertRaises(budget.BudgetExhausted):
            late.reserve("tavily")

    def test_old_days_pruned(self):
        for d in range(1, 20):
            budget.Ledger(self.path, {"tavily": 1}, clock=fixed_clock(f"2026-10-{d:02d}T12:00:00+00:00")).reserve("tavily")
        days = json.loads(self.path.read_text(encoding="utf-8"))["days"]
        self.assertEqual(len(days), budget.KEEP_DAYS)
        self.assertIn("2026-10-19", days)

    def test_from_env_fails_closed(self):
        with self.assertRaises(budget.BudgetNotConfigured):
            budget.from_env({"KIT_HOME": self.tmp.name})                       # no KIT_BUDGETS
        with self.assertRaises(budget.BudgetNotConfigured):
            budget.from_env({"KIT_BUDGETS": "tavily=5"})                        # no ledger path
        with self.assertRaises(budget.BudgetNotConfigured):
            budget.from_env({"KIT_BUDGETS": "tavily=five", "KIT_HOME": self.tmp.name})
        led = budget.from_env({"KIT_BUDGETS": "tavily=5,apollo=0", "KIT_HOME": self.tmp.name, "KIT_BUDGET_TZ": "UTC"})
        self.assertEqual(led.path, Path(self.tmp.name) / "budget" / "ledger.json")
        self.assertEqual(led.limits, {"tavily": 5, "apollo": 0})

    def test_minute_quota_waits(self):
        now = [1000.0]
        clock = lambda: datetime.fromtimestamp(now[0], timezone.utc)  # noqa: E731
        led = budget.Ledger(self.path, {}, clock=clock)
        slept = []

        def sleep(s):
            slept.append(s)
            now[0] += s
        for _ in range(3):
            led.take_minute("sheets_read", 3, sleep=sleep)
        self.assertEqual(slept, [])
        led.take_minute("sheets_read", 3, sleep=sleep)
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 59)

    def test_report_text(self):
        led = budget.Ledger(self.path, {"tavily": 10, "apollo": 2}, "UTC")
        self.assertEqual(budget.render_report(led.status(), 80, False), "[SILENT]")
        led.reserve("apollo", 2)
        text = budget.render_report(led.status(), 80, False)
        self.assertIn("apollo: 2/2 ⛔", text)
        self.assertIn("tavily: 0/10", text)

    def test_parallel_processes_never_overspend(self):
        """The shared part: 8 processes race for a limit of 10, three attempts each -> exactly 10 granted."""
        code = textwrap.dedent(f'''
            import sys; sys.path.insert(0, {str(KIT)!r})
            from jobs import budget
            led = budget.Ledger({str(self.path)!r}, {{"findymail": 10}}, "UTC")
            ok = 0
            for _ in range(3):
                try:
                    led.reserve("findymail"); ok += 1
                except budget.BudgetExhausted:
                    pass
            print(ok)
        ''')
        procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True) for _ in range(8)]
        granted = sum(int(p.communicate(timeout=120)[0].strip()) for p in procs)
        self.assertEqual(granted, 10)
        self.assertEqual(budget.Ledger(self.path, {"findymail": 10}).status()["providers"]["findymail"]["used"], 10)


class VendoredCopiesTest(unittest.TestCase):
    def test_skill_copies_identical(self):
        canon = (KIT / "jobs" / "budget.py").read_bytes()
        for d in (DISCOVERY, CONTACTS):
            self.assertEqual((d / "_budget.py").read_bytes(), canon, f"{d}/_budget.py differs from kit/jobs/budget.py")


class SkillScriptsTest(unittest.TestCase):
    """Run the real skill scripts with a dead network: a used-up budget must stop them before any request."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "stub").mkdir()
        (self.root / "stub" / "httpx.py").write_text(HTTPX_STUB, encoding="utf-8")
        self.home = self.root / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, budgets: str, **extra):
        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and not k.startswith(("KIT_", "HERMES_"))}
        env.update({"PYTHONPATH": str(self.root / "stub"), "KIT_HOME": str(self.home), "KIT_BUDGETS": budgets,
                    "PYTHONIOENCODING": "utf-8", **extra})
        return env

    def run_script(self, script, args, env):
        return subprocess.run([sys.executable, str(script), *args], env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=120)

    def test_discovery_worker_stops_and_keeps_pairs_pending(self):
        run = self.root / "run"
        run.mkdir()
        (run / "config.json").write_text(json.dumps({"run_id": "t", "delay_seconds": 0, "queries": [
            {"text": "q one", "backends": ["tavily"]}, {"text": "q two", "backends": ["tavily"]}]}), encoding="utf-8")
        r = self.run_script(DISCOVERY / "discovery_worker.py", ["--run-dir", str(run)],
                            self.env("tavily=0", TAVILY_API_KEY="test-not-a-key"))
        self.assertEqual(r.returncode, budget.BUDGET_EXIT, r.stdout + r.stderr)
        self.assertNotIn("network call attempted", r.stdout + r.stderr)
        state = json.loads((run / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["completed_pairs"], [])
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertIn("STOPPED", r.stdout)

    def test_discovery_worker_without_budget_config_refuses(self):
        run = self.root / "run"
        run.mkdir()
        (run / "config.json").write_text(json.dumps({"queries": [{"text": "q", "backends": ["tavily"]}],
                                                     "delay_seconds": 0}), encoding="utf-8")
        env = self.env("x=1", TAVILY_API_KEY="test-not-a-key")
        env.pop("KIT_BUDGETS")
        r = self.run_script(DISCOVERY / "discovery_worker.py", ["--run-dir", str(run)], env)
        self.assertEqual(r.returncode, budget.BUDGET_EXIT, r.stdout + r.stderr)
        self.assertIn("KIT_BUDGETS is not set", r.stdout)

    def test_publicwww_check_refused(self):
        r = self.run_script(DISCOVERY / "publicwww_search.py", ["--check", "--query", '"a" "b"'],
                            self.env("publicwww=0", PUBLICWWW_API_KEY="test-not-a-key"))
        self.assertEqual(r.returncode, budget.BUDGET_EXIT, r.stdout + r.stderr)
        self.assertNotIn("network call attempted", r.stdout + r.stderr)

    def test_find_emails_bulk_stops_with_partial_output(self):
        domains = self.root / "domains.txt"
        domains.write_text("one.example\ntwo.example\n", encoding="utf-8")
        out = self.root / "emails.json"
        r = self.run_script(CONTACTS / "find_emails_bulk.py", [str(domains), "--out", str(out), "--delay", "0"],
                            self.env("findymail=0", FINDYMAIL_API_KEY="test-not-a-key", KIT_EMAIL_PROVIDERS="findymail"))
        self.assertEqual(r.returncode, budget.BUDGET_EXIT, r.stdout + r.stderr)
        summary = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual((summary["stopped"], summary["unprocessed"], summary["budget_exhausted"]),
                         ("budget", 2, ["findymail"]))
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {})

    def test_validation_fallback_respects_tavily_budget(self):
        code = textwrap.dedent(f'''
            import os, sys, types
            sys.path.insert(0, {str(DISCOVERY)!r})
            import validation_worker as vw
            fake = types.SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(OSError("keenable down")),
                                         post=lambda *a, **k: (_ for _ in ()).throw(AssertionError("tavily called")))
            sys.modules["httpx"] = fake
            r = vw.keenable_tavily_fetch("one.example")
            print(r["ok"], r.get("budget"))
        ''')
        r = subprocess.run([sys.executable, "-c", code], env=self.env("tavily=0", TAVILY_API_KEY="test-not-a-key"),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "False True", r.stderr)


class ApolloBudgetTest(unittest.TestCase):
    def test_only_enrichment_is_booked(self):
        sys.path.insert(0, str(CONTACTS))
        import apollo_email_finder as apollo
        import _budget as vendored
        with tempfile.TemporaryDirectory() as tmp:
            old_default, old_post = vendored._DEFAULT, apollo._post
            vendored._DEFAULT = vendored.Ledger(Path(tmp) / "l.json", {"apollo": 1})
            calls = []

            def fake_post(url, params, api_key, timeout=20):
                calls.append(url)
                if url == apollo.SEARCH_URL:
                    return {"people": [{"id": "p1"}, {"id": "p2"}]}
                return {"person": {"email": None}}
            apollo._post = fake_post
            try:
                with self.assertRaises(vendored.BudgetExhausted):
                    apollo.find_contact("one.example", "test-not-a-key", max_candidates=2)
            finally:
                vendored._DEFAULT, apollo._post = old_default, old_post
        self.assertEqual(calls, [apollo.SEARCH_URL, apollo.MATCH_URL])   # second enrichment never sent


if __name__ == "__main__":
    unittest.main()
