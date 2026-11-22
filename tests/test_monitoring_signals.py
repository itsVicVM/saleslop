import unittest

from monitoring.signals import SignalProfile, find_signals
from monitoring.store import StoredMessage


def stored(message_id, text, kind="message"):
    return StoredMessage("42", "Test", message_id, kind, "2026-11-17T10:00:00", None, "A", "u1", text, None)


class MonitoringSignalTests(unittest.TestCase):
    def test_short_terms_match_whole_words(self):
        profile = SignalProfile("word boundaries", ("API", "бот"), negative_terms=("job",), min_score=1)
        for phrase in ("API integration", "API-интеграция", "бот online", "API jobless"):
            with self.subTest(phrase=phrase):
                self.assertEqual(len(find_signals([stored(1, phrase)], profile)), 1)
        for phrase in ("capital", "работа", "API job"):
            with self.subTest(phrase=phrase):
                self.assertEqual(find_signals([stored(1, phrase)], profile), [])

    def test_scores_intent_and_excludes_negative_match(self):
        profile = SignalProfile(
            "crm",
            positive_terms=("sales team", "sdr"),
            intent_terms=("looking for", "recommend", "budget"),
            negative_terms=("hiring",),
            min_score=2,
        )
        results = find_signals([
            stored(1, "Our sales team is looking for a CRM, budget is $300"),
            stored(2, "Hiring an SDR"),
        ], profile)
        self.assertEqual([item.message.message_id for item in results], [1])
        self.assertIn("intent match", results[0].reasons[1])

    def test_service_messages_are_not_signals_by_default(self):
        profile = SignalProfile("crm", positive_terms=("crm",), min_score=1)
        self.assertEqual(find_signals([stored(1, "CRM", kind="service")], profile), [])

    def test_optional_semantic_score_is_explainable(self):
        class Scorer:
            def score(self, _text):
                return 0.8

        profile = SignalProfile("crm", positive_terms=("crm",), min_score=2)
        results = find_signals([stored(1, "CRM")], profile, semantic_scorer=Scorer())
        self.assertEqual(results[0].score, 2)
        self.assertEqual(results[0].semantic_score, 0.8)
        self.assertTrue(any("semantic match" in reason for reason in results[0].reasons))
