"""
Call Analytics — Real-time metrics and reporting from Firestore data.
Provides dashboards for call volume, resolution rates, escalation patterns.
"""
import os
import logging
from datetime import datetime, timedelta
from collections import Counter
from google.cloud import firestore

logger = logging.getLogger("callbot")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
db = firestore.Client(project=PROJECT_ID)


def get_daily_stats(date: str = None) -> dict:
    """
    Get statistics for a specific day.
    
    Returns:
    {
        "date": "2026-03-11",
        "total_calls": 45,
        "resolved_by_kb": 18,
        "resolved_by_gemini": 15,
        "escalated": 7,
        "avg_turns": 3.2,
        "avg_duration_ms": 145000,
        "top_intents": {"delivery_status": 20, "return_request": 8, "general": 17},
        "escalation_reasons": {"customer_request": 4, "sensitive_topic": 2, "max_turns": 1},
        "resolution_rate": "84.4%",
    }
    """
    if not date:
        date = datetime.utcnow().strftime("%Y-%m-%d")

    sessions = db.collection("callbot_sessions")\
        .where("updated_at", ">=", f"{date}T00:00:00")\
        .where("updated_at", "<", f"{date}T23:59:59")\
        .stream()

    total = 0
    resolved_kb = 0
    resolved_gemini = 0
    escalated = 0
    total_turns = 0
    intents = Counter()
    escalation_reasons = Counter()
    sentiments = Counter()

    for s in sessions:
        total += 1
        data = s.to_dict()
        history = data.get("history", [])
        user_turns = sum(1 for m in history if m.get("role") == "user")
        total_turns += user_turns

        # Count sources
        for msg in history:
            source = msg.get("source", "")
            if source == "knowledge_base":
                resolved_kb += 1
            elif source == "gemini":
                resolved_gemini += 1

        if data.get("escalated"):
            escalated += 1
            reason = data.get("escalation_reason", "unknown")
            escalation_reasons[reason] += 1

        # Track sentiment
        sentiment = data.get("final_sentiment", "neutral")
        sentiments[sentiment] += 1

    resolution_rate = ((total - escalated) / total * 100) if total > 0 else 0

    return {
        "date": date,
        "total_calls": total,
        "resolved_by_kb": resolved_kb,
        "resolved_by_gemini": resolved_gemini,
        "escalated": escalated,
        "avg_turns": round(total_turns / total, 1) if total > 0 else 0,
        "top_intents": dict(intents.most_common(5)),
        "escalation_reasons": dict(escalation_reasons),
        "sentiments": dict(sentiments),
        "resolution_rate": f"{resolution_rate:.1f}%",
    }


def get_weekly_trend(weeks: int = 4) -> list[dict]:
    """Get daily stats for the last N weeks."""
    results = []
    today = datetime.utcnow()
    for i in range(weeks * 7):
        date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        stats = get_daily_stats(date)
        if stats["total_calls"] > 0:
            results.append(stats)
    return results


def get_top_unanswered_questions(limit: int = 10) -> list[dict]:
    """
    Find questions the bot couldn't answer (went to Gemini or escalated).
    These should be added to the knowledge base.
    """
    sessions = db.collection("callbot_sessions")\
        .order_by("updated_at", direction=firestore.Query.DESCENDING)\
        .limit(500)\
        .stream()

    unanswered = Counter()

    for s in sessions:
        data = s.to_dict()
        history = data.get("history", [])
        for i, msg in enumerate(history):
            if msg.get("role") == "user" and msg.get("source") in ("gemini", "escalation"):
                unanswered[msg["content"]] += 1

    return [
        {"question": q, "count": c}
        for q, c in unanswered.most_common(limit)
    ]


def get_performance_summary() -> dict:
    """
    Overall performance metrics for the callbot.
    """
    # Last 30 days
    sessions = db.collection("callbot_sessions")\
        .order_by("updated_at", direction=firestore.Query.DESCENDING)\
        .limit(1000)\
        .stream()

    total = 0
    escalated = 0
    total_turns = 0
    response_sources = Counter()
    avg_frustration = []

    for s in sessions:
        total += 1
        data = s.to_dict()
        history = data.get("history", [])
        user_turns = sum(1 for m in history if m.get("role") == "user")
        total_turns += user_turns

        if data.get("escalated"):
            escalated += 1

        for msg in history:
            source = msg.get("source", "unknown")
            if msg.get("role") == "assistant":
                response_sources[source] += 1

        if data.get("frustration_score") is not None:
            avg_frustration.append(data["frustration_score"])

    return {
        "total_calls_30d": total,
        "resolution_rate": f"{((total - escalated) / total * 100):.1f}%" if total > 0 else "N/A",
        "escalation_rate": f"{(escalated / total * 100):.1f}%" if total > 0 else "N/A",
        "avg_turns_per_call": round(total_turns / total, 1) if total > 0 else 0,
        "response_sources": dict(response_sources),
        "avg_frustration": round(sum(avg_frustration) / len(avg_frustration), 1) if avg_frustration else 0,
        "knowledge_base_hit_rate": f"{(response_sources.get('knowledge_base', 0) / sum(response_sources.values()) * 100):.1f}%" if response_sources else "N/A",
    }
