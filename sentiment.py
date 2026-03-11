"""
Sentiment Analysis — Detect caller frustration in real-time
Uses Vertex AI Gemini for nuanced French sentiment detection.
Feeds into escalation logic: frustrated callers get escalated faster.
"""
import os
import json
import time
import logging
import re
from dataclasses import dataclass
from enum import Enum

import vertexai
from vertexai.generative_models import GenerativeModel

logger = logging.getLogger("callbot")

MODEL_NAME = os.getenv("VERTEX_MODEL", "gemini-2.0-flash-001")


class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    FRUSTRATED = "frustrated"
    ANGRY = "angry"


@dataclass
class SentimentResult:
    sentiment: Sentiment
    confidence: float       # 0.0 - 1.0
    frustration_score: int  # 0-10 (0=calm, 10=furious)
    trigger_words: list[str]


# --- Rule-based (instant, $0) ---

FRUSTRATION_MARKERS = {
    3: ["je comprends pas", "encore", "toujours pas", "ça fait longtemps",
        "j'attends", "pas normal", "quand même"],
    5: ["inacceptable", "n'importe quoi", "ras le bol", "marre",
        "scandaleux", "honteux", "lamentable", "ridicule"],
    7: ["arnaque", "voleur", "plainte", "avocat", "tribunal",
        "remboursez-moi immédiatement", "je vais porter plainte"],
    9: ["putain", "merde", "bordel", "connard", "enculé"],
}

POSITIVE_MARKERS = [
    "merci", "parfait", "super", "excellent", "génial",
    "très bien", "c'est bon", "d'accord", "ok merci"
]


def rule_based_sentiment(text: str) -> SentimentResult:
    """Instant sentiment analysis using keyword matching. Cost: $0."""
    normalized = text.lower()
    max_score = 0
    triggers = []

    for score, keywords in FRUSTRATION_MARKERS.items():
        for kw in keywords:
            if kw in normalized:
                max_score = max(max_score, score)
                triggers.append(kw)

    if max_score == 0 and any(kw in normalized for kw in POSITIVE_MARKERS):
        return SentimentResult(
            sentiment=Sentiment.POSITIVE,
            confidence=0.7,
            frustration_score=0,
            trigger_words=[kw for kw in POSITIVE_MARKERS if kw in normalized]
        )

    if max_score >= 7:
        sentiment = Sentiment.ANGRY
    elif max_score >= 4:
        sentiment = Sentiment.FRUSTRATED
    else:
        sentiment = Sentiment.NEUTRAL

    return SentimentResult(
        sentiment=sentiment,
        confidence=0.6 if triggers else 0.3,
        frustration_score=max_score,
        trigger_words=triggers
    )


# --- LLM-based (Vertex AI, nuanced) ---

SENTIMENT_PROMPT = """Analyse le sentiment de ce message d'un client au téléphone.

Message : "{text}"

Contexte conversation (derniers échanges) :
{context}

Réponds UNIQUEMENT en JSON :
{{"sentiment": "positive|neutral|frustrated|angry", "frustration_score": <0-10>, "confidence": <0.0-1.0>, "trigger_words": ["mot1", "mot2"]}}

Critères :
- 0-2 : client calme, satisfait ou neutre
- 3-5 : client agacé, impatient, début de frustration
- 6-7 : client clairement frustré, mécontent
- 8-10 : client en colère, agressif, menaces"""


async def gemini_sentiment(text: str, context: str = "") -> SentimentResult:
    """
    Nuanced sentiment analysis via Vertex AI.
    Catches sarcasm, passive aggression, and escalating frustration.
    """
    model = GenerativeModel(MODEL_NAME)
    prompt = SENTIMENT_PROMPT.format(text=text, context=context)

    try:
        t0 = time.time()
        response = model.generate_content(
            prompt,
            generation_config={"max_output_tokens": 100, "temperature": 0.1}
        )
        latency = int((time.time() - t0) * 1000)

        json_match = re.search(r'\{[^}]+\}', response.text)
        if json_match:
            data = json.loads(json_match.group())
            sentiment_str = data.get("sentiment", "neutral")
            sentiment_map = {
                "positive": Sentiment.POSITIVE,
                "neutral": Sentiment.NEUTRAL,
                "frustrated": Sentiment.FRUSTRATED,
                "angry": Sentiment.ANGRY,
            }

            logger.info("sentiment_analysis", extra={
                "json_fields": {
                    "text": text[:80],
                    "sentiment": sentiment_str,
                    "frustration_score": data.get("frustration_score", 0),
                    "latency_ms": latency,
                }
            })

            return SentimentResult(
                sentiment=sentiment_map.get(sentiment_str, Sentiment.NEUTRAL),
                confidence=data.get("confidence", 0.5),
                frustration_score=data.get("frustration_score", 0),
                trigger_words=data.get("trigger_words", [])
            )
    except Exception as e:
        logger.error(f"Sentiment analysis error: {e}")

    return rule_based_sentiment(text)


# --- Combined pipeline ---

def analyze_sentiment(
    text: str,
    conversation_history: list[dict] = None,
    use_llm: bool = False
) -> SentimentResult:
    """
    Two-tier sentiment analysis:
    1. Rule-based (always, instant)
    2. Gemini (optional, for ambiguous cases)
    
    Escalation thresholds:
    - frustration_score >= 6 → suggest escalation
    - frustration_score >= 8 → force escalation
    """
    rule_result = rule_based_sentiment(text)

    # If rules are confident enough, skip LLM
    if rule_result.confidence >= 0.7 or rule_result.frustration_score >= 7:
        return rule_result

    # For ambiguous cases, could call Gemini (async)
    # In sync context, return rule-based
    return rule_result


def get_frustration_trend(history: list[dict]) -> dict:
    """
    Analyze frustration trend across the conversation.
    Rising frustration = escalate proactively.
    """
    scores = []
    for msg in history:
        if msg.get("role") == "user":
            result = rule_based_sentiment(msg["content"])
            scores.append(result.frustration_score)

    if len(scores) < 2:
        return {"trend": "stable", "current": scores[-1] if scores else 0, "rising": False}

    recent = scores[-2:]
    trend = "rising" if recent[-1] > recent[-2] else "falling" if recent[-1] < recent[-2] else "stable"

    return {
        "trend": trend,
        "current": scores[-1],
        "average": sum(scores) / len(scores),
        "peak": max(scores),
        "rising": trend == "rising" and scores[-1] >= 4,
    }
