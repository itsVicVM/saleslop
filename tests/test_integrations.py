"""Offline contracts for optional providers and regional reporting."""
import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from bots.mail.config import MailConfig
from bots.mail.engine import MailEngine
from bots.mail.connect import pick_preset
from bots.ops.crm import get_adapter
from jobs import report
from runner import kit_env


KIT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def test_mailbox_verifier_requires_an_explicit_boolean(self):
        import sys
        scripts = KIT / "skills/research/telegram-contact-research/scripts"
        with patch.object(sys, "path", [str(scripts), *sys.path]):
            import find_emails_bulk as lookup
        with patch.object(lookup._budget, "reserve") as reserve, patch.object(lookup.urllib.request, "urlopen") as request:
            self.assertEqual(lookup.verify_findymail("a@one.example", "")["status"], "unverified")
            reserve.assert_not_called()
            request.assert_not_called()
        for verdict, expected in ((True, "valid"), (False, "invalid"), ("false", "unverified"), (None, "unverified")):
            body = json.dumps({"verified": verdict}).encode()
            with (self.subTest(verdict=verdict), patch.object(lookup._budget, "reserve"),
                  patch.object(lookup.urllib.request, "urlopen", return_value=io.BytesIO(body))):
                self.assertEqual(lookup.verify_findymail("a@one.example", "fixture")["status"], expected)

    def test_email_lookup_counts_only_valid_mailboxes(self):
        import sys
        scripts = KIT / "skills/research/telegram-contact-research/scripts"
        with patch.object(sys, "path", [str(scripts), *sys.path]):
            import find_emails_bulk as lookup
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            domains = root / "domains.txt"
            domains.write_text("one.example\ntwo.example\nthree.example\n", encoding="utf-8")
            args = ["lookup", str(domains), "--out", str(root / "out.json"), "--delay", "0"]
            verdicts = [{"status": status} for status in ("valid", "invalid", "catch_all")]
            stream = io.StringIO()
            with (patch.object(sys, "argv", args), patch.dict(os.environ, {"FINDYMAIL_API_KEY": "fixture"}, clear=True),
                  patch.object(lookup, "search_domain", return_value={"contacts": [{"email": "a@one.example"}]}),
                  patch.object(lookup, "verify_email", side_effect=verdicts), redirect_stdout(stream)):
                self.assertEqual(lookup.main(), 0)
            summary = json.loads(stream.getvalue())
            self.assertEqual(summary["reoon_verified"], 1)
            self.assertEqual(summary["verification_statuses"], {"valid": 1, "invalid": 1, "catch_all": 1})
            self.assertFalse(summary["verifier_outage"])

    def test_robots_exclusion_stops_all_validation_fallbacks(self):
        import sys
        scripts = KIT / "skills/research/bulk-web-discovery-worker/scripts"
        with patch.object(sys, "path", [str(scripts), *sys.path]):
            import validation_worker as worker

        refusal = {"ok": False, "html": "", "blocked_by_robots": True, "error": "robots exclusion"}
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "raw_results.jsonl").write_text('{"url":"https://buyer.example"}\n', encoding="utf-8")
            args = ["validate", "--run-dir", tmp, "--verify", "--browser-fallback", "--relevance-terms", "test"]
            with (patch.object(sys, "argv", args), patch.object(worker.Fetcher, "fetch", return_value=refusal),
                  patch.object(worker, "browser_fallback_fetch") as browser,
                  patch.object(worker, "keenable_tavily_fetch") as provider,
                  patch.object(worker, "_close_browser_fallback_session"), redirect_stdout(io.StringIO())):
                worker.main()
            browser.assert_not_called()
            provider.assert_not_called()
            result = json.loads((run / "candidates.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(result["validation_status"], "needs_manual_check")

    def test_crm_links_do_not_follow_a_provider_switch(self):
        from bots.ops.core import Engine
        instance = object.__new__(Engine)
        for name, key in (("amocrm", "seller"), ("hubspot", "hubspot:seller"), ("bitrix24", "bitrix24:seller")):
            instance.crm = Mock()
            instance.crm.name = name
            self.assertEqual(instance._crm_peer_key("@Seller"), key)
        instance.store, instance.ops = Mock(), Mock()
        instance.t = {"done_already": "stale"}
        instance.store.load.return_value = {"abc": {"peer": "seller", "ids": ["7"]}}
        self.assertEqual(instance._crm_callback("cb", 3, "abc", "7"), "done_already")
        instance.store.set_crm_link.assert_not_called()

    def test_crm_candidates_and_note(self):
        for kind, key, setting, source, result in (
            ("hubspot", "token", "test", {"results": [{"id": "7", "properties": {"dealname": "Example"}}]}, {"id": "9"}),
            ("bitrix24", "webhook", "https://portal.invalid/rest/1/test",
             {"result": [{"ID": "7", "TITLE": "Example"}]}, {"result": 9}),
        ):
            with self.subTest(kind=kind):
                crm = {"type": kind, kind: {key: "env:TEST", "telegram_field": "channel"}}
                pack = {"company": {"name": "Example"}, "bot": {"ops": {"bot_token": "env:BOT", "owner_chat": "env:CHAT"}}, "crm": crm}
                env, refs, problems = kit_env.build_ops_env(pack, {"BOT": "bot", "CHAT": "1", "TEST": setting}, KIT)
                self.assertEqual(problems, [])
                cfg = json.loads(env["KIT_OPS_CONFIG"])["crm"]
                adapter = get_adapter(cfg, env)
                with patch("bots.ops.crm.deals.post_json", side_effect=[source, result, {}]) as api:
                    self.assertEqual(adapter.search_leads("sample"), [{"id": "7", "name": "Example"}])
                    self.assertTrue(adapter.add_note("7", "Confirmed by operator"))
                    self.assertFalse(adapter.add_note("7", "Failure"))
                    self.assertIn("channel", json.dumps(api.call_args_list[0].args[1]))
                    self.assertIn('"7"', json.dumps(api.call_args_list[1].args[1]))
                crm["task_on_reply"] = True
                self.assertTrue(any("lookup and notes only" in p for p in kit_env.check_ops_sections(pack)))

    def test_regional_report_from_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            rows = path / "excel.csv"
            rows.write_text("member;company;first_touch;hypothesis;won_on\noperator;Café;2026-11-19;export;2026-11-19\n",
                            encoding="utf-8-sig")
            for region, language, title in (("America/New_York", "en", "Report for"), ("Europe/Moscow", "ru", "Отчёт за")):
                pack = json.loads((KIT / "config/pack.template.json").read_text(encoding="utf-8"))
                pack["company"]["timezone"] = region
                pack["reports"]["language"] = language
                env, _, errors = kit_env.build_jobs_env(pack, {}, KIT)
                self.assertEqual(errors, [])
                with patch.dict(os.environ, {**env, "KIT_HOME": tmp}, clear=True), redirect_stdout(io.StringIO()) as out:
                    report.main(["--rows-csv", str(rows), "--day", "2026-11-19", "--archive", tmp])
                self.assertIn(title, out.getvalue())
                data = json.loads((path / "2026-11-19.json").read_text(encoding="utf-8"))
                self.assertEqual(data["tracker_source"], "csv")
                self.assertEqual(data["weekly_activity"]["by_hypothesis"]["export"]["won_on"], 1)

    def test_mail_presets_with_explicit_override(self):
        cfg = MailConfig.from_dict({"enabled": True, "connect": {"presets": [
            {"id": "yandex", "imap": "imaps://custom.invalid:993"}]}}, home=KIT)
        self.assertEqual(pick_preset(cfg.presets, "example.invalid", "yandex")["imap"], "imaps://custom.invalid:993")
        self.assertFalse(pick_preset(cfg.presets, "example.invalid", "zoho-us-business")["starttls"])

    def test_graph_cursor_kept_on_failed_read(self):
        engine = Mock()
        mb = Mock(id="one", address="one@example.invalid")
        state = {}
        engine.state.box.side_effect = lambda _: state.copy()
        engine.state.update_box.side_effect = lambda _, **kw: state.update(kw)
        api = engine._graph.return_value
        api.page.side_effect = [(["old"], "next", True), (["old2"], "delta", False),
                                (["new"], "later", False), (["new"], "later", False)]
        for _ in range(2):
            MailEngine._poll_graph(engine, mb, set(), {})
        api.raw.assert_not_called()
        api.raw.side_effect = [ValueError("HTTP 503"), b"From: customer@example.invalid"]
        with self.assertRaises(ValueError):
            MailEngine._poll_graph(engine, mb, set(), {})
        self.assertEqual(state["graph_cursor"], "delta")
        MailEngine._poll_graph(engine, mb, set(), {})
        engine.process_raw.assert_called_once()
        self.assertEqual(state["graph_cursor"], "later")

    @unittest.skipUnless(importlib.util.find_spec("httpx"), "install runtime dependencies to test HTTP adapters")
    def test_graph_requests_and_unknown_send(self):
        import httpx
        from bots.mail.ms_graph import GraphMailbox
        tenant = "00000000-0000-0000-0000-000000000001"
        fake = Mock(side_effect=[
            httpx.Response(200, json={"access_token": "dummy", "expires_in": 3600}),
            httpx.Response(200, json={"value": [{"id": "old"}], "@odata.deltaLink": "https://graph.microsoft.com/test"}),
            httpx.Response(200, content=b"Subject: Hello"), httpx.Response(202), httpx.Response(503)])
        api = GraphMailbox(Mock(env_prefix="M", address="op@example.invalid"), {
            "M_TENANT_ID": tenant, "M_CLIENT_ID": "test", "M_CLIENT_SECRET": "dummy"}, request=fake)
        self.assertEqual(api.page(None)[0], ["old"])
        with self.assertRaises(ValueError):
            api.page("https://other.invalid/cursor")
        self.assertEqual(api.raw("a/b"), b"Subject: Hello")
        self.assertIn("a%2Fb", fake.call_args.args[1])
        self.assertEqual(api.send(b"mime")[0], "sent")
        self.assertEqual(fake.call_args.kwargs["content"], base64.b64encode(b"mime"))
        self.assertEqual(api.send(b"mime")[0], "unknown")

    @unittest.skipUnless(importlib.util.find_spec("httpx"), "install runtime dependencies to test HTTP adapters")
    def test_serper_region_and_budget(self):
        import sys
        directory = KIT / "skills/research/bulk-web-discovery-worker/scripts"
        with patch.object(sys, "path", [str(directory), *sys.path]), patch.dict(sys.modules):
            import discovery_worker as worker
            with patch.dict(os.environ, {"SERPER_API_KEY": "dummy", "SERPER_COUNTRY": "br", "SERPER_LANGUAGE": "pt"}), \
                 patch.object(worker._budget, "reserve") as reserve, patch("httpx.post") as post:
                post.return_value.json.return_value = {"organic": [{"link": "https://example.invalid"}]}
                self.assertEqual(worker.BACKENDS["serper"]("example")["results"][0]["url"], "https://example.invalid")
                reserve.assert_called_once_with("serper")
                self.assertEqual(post.call_args.kwargs["json"], {"q": "example", "gl": "br", "hl": "pt"})
                reserve.side_effect = worker._budget.BudgetExhausted("serper", 1, 1)
                self.assertTrue(worker.serper_search("example")["budget"])
                post.assert_called_once()
