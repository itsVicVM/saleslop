"""Offline delivery regressions for an isolated deployment."""
import asyncio
import datetime as dt
import os
import smtplib
import subprocess
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from bots.mail.engine import MailEngine
from bots.mail.gmail import Gmail
from bots.mail.state import MailState
from bots.ops import followup
from bots.ops.core import Engine
from bots.ops.store import Store
from outreach.journal import Journal
from outreach.transport import HimalayaTransport, MailboxError, SmtpTransport


class DeliveryRecovery(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.box = SimpleNamespace(id="fixture", address="team@fixture.invalid", env_prefix="FIXTURE",
                                   himalaya_account="fixture", provider="himalaya", sender_name="Operator")

    def test_himalaya_timeout_after_handoff_holds_recipient(self):
        runner = Mock(side_effect=subprocess.TimeoutExpired("himalaya", 60))
        tr = HimalayaTransport({"FIXTURE_HIMALAYA_ACCOUNT": "fixture"}, "himalaya {account}", runner=runner)
        with self.assertRaises(MailboxError) as error:
            tr.send(self.box, EmailMessage())
        self.assertFalse(error.exception.retryable)

    def test_temporary_smtp_refusal_is_not_a_permanent_bounce(self):
        smtp = MagicMock()
        env = {"FIXTURE_SMTP_HOST": "mail.fixture.invalid", "FIXTURE_SMTP_PASSWORD": "fixture", "FIXTURE_SMTP_SECURITY": "ssl"}
        smtp.SMTP_SSL.return_value.send_message.side_effect = smtplib.SMTPRecipientsRefused(
            {"lead@fixture.invalid": (450, b"temporary")})
        with self.assertRaises(MailboxError) as error:
            SmtpTransport(env, smtp_module=smtp).send(self.box, EmailMessage())
        self.assertTrue(error.exception.retryable)

    def test_journal_replays_under_writer_lock(self):
        reader = Journal(self.home)
        writer = Journal(self.home)
        writer.lock()
        writer.append({"event": "bounce", "ts": "2026-01-01T10:00:00+00:00", "email": self.box.address})
        writer.unlock()
        reader.lock()
        try:
            self.assertIn(self.box.address, reader.view.bounced)
        finally:
            reader.unlock()

    def telegram(self):
        config = SimpleNamespace(operator_language="en", state_dir=self.home, send_log_path=self.home / "out.jsonl",
                                 crm={})
        transport = Mock()
        transport.send = AsyncMock(return_value=42)
        engine = Engine(config, llm=None, ops=Mock(), transport=transport, crm=SimpleNamespace(name="none"))
        return engine

    def card(self, engine):
        engine.store.save_item({"id": "fixture", "short_id": "fixture", "status": "awaiting_approval",
                                "drafts": [{"text": "Reply text", "cta": "Schedule a meeting?"}],
                                "account": "fixture", "peer": "@fixture_lead"})

    def test_cancel_between_reply_and_cta_then_replay_callback(self):
        engine = self.telegram()
        self.card(engine)
        engine.sleep = AsyncMock(side_effect=asyncio.CancelledError)
        cb = {"data": "appr:sendcta:fixture"}
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(engine.handle_callback(cb))
        self.assertEqual(engine.transport.send.await_count, 1)
        restarted = self.telegram()
        restarted.recover_sends()
        self.assertEqual(asyncio.run(restarted.handle_callback(cb)), "unknown")
        restarted.transport.send.assert_not_awaited()
        self.assertIn("Schedule a meeting?", restarted.ops.send.call_args.args[0])

    def test_ambiguous_telegram_send_leaves_no_active_approval(self):
        engine = self.telegram()
        self.card(engine)
        engine.transport.send.side_effect = TimeoutError()
        cb = {"data": "appr:send:fixture"}
        asyncio.run(engine.handle_callback(cb))
        self.assertEqual(asyncio.run(engine.handle_callback(cb)), "unknown")
        self.assertEqual(engine.transport.send.await_count, 1)

    def test_followup_reservation_survives_restart(self):
        candidate = {"handle": "fixture_lead", "step": 1, "opener": "42"}
        store = Store(self.home, self.home / "send.jsonl")
        self.assertTrue(followup._claim_step(store, candidate))
        self.assertFalse(followup._claim_step(Store(self.home, store.send_log_path), candidate))
        followup._claim_step(store, candidate, release=True)
        self.assertTrue(followup._claim_step(store, candidate))

    def test_delivered_followup_repairs_stage_only_for_exact_contact(self):
        cols = dict(stage="A", status="B", next_touch="C", telegram="D", account="E")
        row = ["1", "active", "2026-01-01", "@fixture_lead", "fixture_main"]
        log = [{"peer": "@fixture_lead", "kind": "followup1", "account": "fixture_main", "direction": "out"}]
        cfg = {"status_value": "active"}
        candidates, _ = followup.plan([row], cfg, cols, log, ["fixture_main"], {"fixture_main"}, dt.date(2026, 1, 2))
        candidate, = candidates
        self.assertTrue(candidate["already_sent"])
        svc = Mock()
        api = svc.spreadsheets.return_value.values.return_value
        results = api.get.return_value.execute
        tracker = {"columns": cols, "tab": "Tracker"}
        with patch.dict(os.environ, {"SHEET_ID": "fixture"}):
            results.return_value = {"values": [["1", "active", "", "@fixture_lead_other"]]}
            self.assertIn("changed", followup._update_stage(candidate, svc, tracker, cfg))
            api.update.assert_not_called()
            results.side_effect = [{"values": [row]}, {"values": [["2"]]}]
            self.assertEqual(followup._update_stage(candidate, svc, tracker, cfg), "stage 2")
            api.update.assert_called_once()

    def mail(self):
        cfg = SimpleNamespace(state_dir=self.home, mailboxes=[self.box])
        return MailEngine(cfg, SimpleNamespace(operator_language="en"), llm=None, ops=Mock(), himalaya=Mock(), gmail=Mock())

    def test_mail_crash_restores_review_or_existing_receipt(self):
        state = MailState(self.home)
        for sid in ("aaa", "bbb"):
            state.save_card({"id": sid, "status": "sending", "sent_message_id": sid, "mailbox": "fixture"})
        state.append("sent.jsonl", {"card": "aaa", "message_id": "aaa", "outcome": "sent"})
        engine = self.mail()
        engine.recover_sends()
        self.assertEqual(state.load_card("aaa")["status"], "sent")
        self.assertEqual(state.load_card("bbb")["status"], "unknown")
        self.assertEqual(engine.send_card("bbb"), "unknown")
        engine.himalaya.send_raw.assert_not_called()

    def test_reply_to_closed_prompt_cannot_send_another_contacts_message(self):
        engine = self.telegram()
        self.card(engine)
        current = engine.store.load_item("fixture")
        current.update(status="awaiting_own_text", prompt_message_id=42)
        engine.store.save_item(current)
        result = asyncio.run(engine.handle_text({"text": "For a different contact",
                                                "reply_to_message": {"message_id": 41}}))
        self.assertEqual(result, "ignored")
        engine.transport.send.assert_not_awaited()

    def test_read_failures_do_not_advance_past_unread_mail(self):
        engine = self.mail()
        engine.process_raw = Mock()
        state = engine.state
        state.update_box("fixture", last_uid=1, seen_ids=["old"])
        engine.himalaya.list_ids.return_value = [2, 3]
        engine.himalaya.read_raw.side_effect = [None, b"second", b"third"]
        stats = {"errors": 0}
        engine._poll_himalaya(self.box, set(), stats)
        self.assertEqual(state.box("fixture")["last_uid"], 1)
        engine._poll_himalaya(self.box, set(), stats)
        self.assertEqual(state.box("fixture")["last_uid"], 3)
        engine.gmail.inbox_ids.return_value = ["new", "old"]
        engine.gmail.read_raw.side_effect = [None, (b"new", "thread")]
        engine._poll_gmail(self.box, set(), stats)
        self.assertNotIn("new", state.box("fixture")["seen_ids"])
        engine._poll_gmail(self.box, set(), stats)
        self.assertIn("new", state.box("fixture")["seen_ids"])

    def test_gmail_ambiguous_response_requires_operator_review(self):
        gmail = Gmail({}, None)
        for code, outcome in ((503, "unknown"), (408, "unknown"), (400, "failed")):
            with self.subTest(code=code), patch.object(gmail, "_call", return_value=(code, {})):
                self.assertEqual(gmail.send_raw("fixture", b"Operator")[0], outcome)
