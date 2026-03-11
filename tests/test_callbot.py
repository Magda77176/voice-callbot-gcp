"""
Tests for Voice Callbot — pytest
Covers: sentiment, escalation, KB matching, cache, analytics endpoints.
"""
import pytest
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sentiment import rule_based_sentiment, Sentiment, get_frustration_trend
from zendesk import should_escalate, EscalationReason, summarize_conversation
from cache import ResponseCache


# ============================================================
# SENTIMENT TESTS
# ============================================================

class TestSentiment:
    def test_positive_message(self):
        result = rule_based_sentiment("Merci beaucoup, c'est parfait !")
        assert result.sentiment == Sentiment.POSITIVE
        assert result.frustration_score == 0

    def test_neutral_message(self):
        result = rule_based_sentiment("Quels sont vos horaires ?")
        assert result.sentiment == Sentiment.NEUTRAL
        assert result.frustration_score == 0

    def test_frustrated_message(self):
        result = rule_based_sentiment("C'est inacceptable, j'attends depuis 2 semaines")
        assert result.sentiment == Sentiment.FRUSTRATED
        assert result.frustration_score >= 4

    def test_angry_message(self):
        result = rule_based_sentiment("C'est une arnaque, je vais porter plainte")
        assert result.sentiment == Sentiment.ANGRY
        assert result.frustration_score >= 7

    def test_passive_aggressive(self):
        result = rule_based_sentiment("Encore une erreur, c'est toujours pas résolu")
        assert result.frustration_score >= 3

    def test_frustration_trend_rising(self):
        history = [
            {"role": "user", "content": "Où est ma commande ?"},
            {"role": "assistant", "content": "En cours de livraison."},
            {"role": "user", "content": "Ça fait longtemps, c'est pas normal !"},
            {"role": "assistant", "content": "Je comprends."},
            {"role": "user", "content": "C'est inacceptable !"},
        ]
        trend = get_frustration_trend(history)
        assert trend["rising"] or trend["current"] >= 4

    def test_empty_message(self):
        result = rule_based_sentiment("")
        assert result.sentiment == Sentiment.NEUTRAL


# ============================================================
# ESCALATION TESTS
# ============================================================

class TestEscalation:
    def test_customer_asks_for_human(self):
        escalate, reason, _ = should_escalate(
            "Je veux parler à quelqu'un", []
        )
        assert escalate is True
        assert reason == EscalationReason.CUSTOMER_REQUEST

    def test_sensitive_topic(self):
        escalate, reason, priority = should_escalate(
            "Je veux un remboursement immédiat", []
        )
        assert escalate is True
        assert reason == EscalationReason.SENSITIVE_TOPIC
        assert priority == "high"

    def test_normal_question_no_escalation(self):
        escalate, reason, _ = should_escalate(
            "Quels sont vos horaires ?", []
        )
        assert escalate is False
        assert reason is None

    def test_max_turns_escalation(self):
        history = [{"role": "user", "content": f"Question {i}"} for i in range(9)]
        escalate, reason, _ = should_escalate("Encore une question", history)
        assert escalate is True
        assert reason == EscalationReason.MAX_TURNS_REACHED

    def test_stt_failures_escalation(self):
        escalate, reason, _ = should_escalate(
            "test", [], stt_failures=3
        )
        assert escalate is True
        assert reason == EscalationReason.REPEATED_MISUNDERSTANDING

    def test_low_confidence_needs_two(self):
        """Gemini uncertainty should only escalate after 2 uncertain responses."""
        history = [
            {"role": "assistant", "content": "Je ne sais pas exactement."},
            {"role": "user", "content": "Et maintenant ?"},
            {"role": "assistant", "content": "Je ne peux pas confirmer."},
        ]
        escalate, reason, _ = should_escalate(
            "Vous savez ou pas ?", history,
            gemini_response="Je ne suis pas sûr de la réponse."
        )
        assert escalate is True
        assert reason == EscalationReason.LOW_CONFIDENCE

    def test_manager_request_high_priority(self):
        escalate, _, priority = should_escalate(
            "Je veux parler au responsable", []
        )
        assert escalate is True
        assert priority == "high"


# ============================================================
# CONVERSATION SUMMARY TESTS
# ============================================================

class TestSummary:
    def test_summary_with_order(self):
        history = [
            {"role": "user", "content": "Où est ma commande ?"},
            {"role": "assistant", "content": "Votre commande est en cours."},
        ]
        summary = summarize_conversation(history, "100456789")
        assert "100456789" in summary
        assert "Où est ma commande" in summary
        assert "Client" in summary

    def test_summary_without_order(self):
        history = [{"role": "user", "content": "Bonjour"}]
        summary = summarize_conversation(history)
        assert "Bonjour" in summary

    def test_empty_history(self):
        summary = summarize_conversation([])
        assert "Transcription" in summary


# ============================================================
# CACHE TESTS
# ============================================================

class TestCache:
    def test_cache_miss(self):
        cache = ResponseCache()
        assert cache.get("nouvelle question") is None

    def test_cache_hit(self):
        cache = ResponseCache()
        cache.put("Quels sont vos horaires ?", "9h-18h", "knowledge_base")
        result = cache.get("Quels sont vos horaires ?")
        assert result is not None
        assert result.response == "9h-18h"
        assert result.source == "knowledge_base"

    def test_cache_case_insensitive(self):
        cache = ResponseCache()
        cache.put("Horaires ?", "9h-18h", "kb")
        result = cache.get("horaires ?")
        assert result is not None

    def test_cache_eviction(self):
        cache = ResponseCache(max_size=2)
        cache.put("q1", "r1", "kb")
        cache.put("q2", "r2", "kb")
        cache.put("q3", "r3", "kb")  # Should evict q1
        assert cache.get("q1") is None
        assert cache.get("q3") is not None

    def test_cache_stats(self):
        cache = ResponseCache()
        cache.put("q1", "r1", "kb")
        cache.get("q1")  # hit
        cache.get("q2")  # miss
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["size"] == 1

    def test_cache_clear(self):
        cache = ResponseCache()
        cache.put("q1", "r1", "kb")
        cache.clear()
        assert cache.get("q1") is None
        assert cache.stats()["size"] == 0


# ============================================================
# KNOWLEDGE BASE TESTS
# ============================================================

class TestKnowledgeBase:
    """Test the fuzzy matching logic."""

    def test_exact_match(self):
        from app import find_best_match, knowledge_base
        if knowledge_base:
            first_q = knowledge_base[0]["question"]
            result = find_best_match(first_q)
            assert result is not None

    def test_no_match(self):
        from app import find_best_match
        result = find_best_match("xyz abc impossible question 12345")
        assert result is None


# ============================================================
# INTENT DETECTION TESTS
# ============================================================

class TestIntentDetection:
    def test_delivery_intent(self):
        from app import detect_intent
        assert detect_intent("Où est ma commande ?") == "delivery_status"
        assert detect_intent("Quand va arriver mon colis ?") == "delivery_status"

    def test_return_intent(self):
        from app import detect_intent
        assert detect_intent("Je veux retourner un produit") == "return_request"
        assert detect_intent("Comment me faire rembourser ?") == "return_request"

    def test_general_intent(self):
        from app import detect_intent
        assert detect_intent("Bonjour") == "general"
        assert detect_intent("Quels sont vos horaires ?") == "general"
