"""
Feedback Loop — Zendesk tickets feed back into the callbot stack.

The problem: tickets get escalated, humans resolve them, but the bot
never learns from those resolutions. Same questions keep getting escalated.

Pipeline:
  Zendesk (resolved tickets)
     │
     ▼
  1. Fetch resolved tickets tagged "callbot"
  2. Extract: original question + human resolution
  3. Gemini classifies: can the bot handle this next time?
  4. Auto-actions:
     a. Add to knowledge base (data.json) → no more escalation for this
     b. Improve system prompt → better handling
     c. Flag for review → edge case, needs human decision
  5. Store in BigQuery for trend analysis
  6. Weekly report → Slack/email

Result: bot gets smarter every week, escalation rate drops continuously.

Architecture:
  ┌──────────┐     fetch      ┌────────────┐    classify   ┌─────────┐
  │ Zendesk  │───────────────▶│ Feedback   │──────────────▶│ Gemini  │
  │ (solved) │  tickets       │ Processor  │  resolution   │ Flash   │
  └──────────┘               └────────────┘              └─────────┘
                                    │                         │
                                    ▼                         ▼
                              ┌─────────┐            ┌──────────────┐
                              │ BigQuery│            │ Actions:     │
                              │ (trends)│            │ - KB update  │
                              └─────────┘            │ - Prompt fix │
                                                     │ - Flag human │
                                                     └──────────────┘
"""
import os
import json
import logging
import time
from datetime import datetime, timedelta
from enum import Enum

import requests
import vertexai
from vertexai.generative_models import GenerativeModel
from google.cloud import firestore, bigquery

logger = logging.getLogger("callbot")

# --- Config ---
ZENDESK_SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN")
ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL")
ZENDESK_API_TOKEN = os.getenv("ZENDESK_API_TOKEN")
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
MODEL_NAME = os.getenv("VERTEX_MODEL", "gemini-2.0-flash-001")
KB_PATH = os.getenv("KB_PATH", "data.json")

db = firestore.Client(project=PROJECT_ID)


class FeedbackAction(Enum):
    ADD_TO_KB = "add_to_kb"           # Bot can handle this → add to knowledge base
    IMPROVE_PROMPT = "improve_prompt"   # Bot answered wrong → adjust prompt/behavior
    FLAG_REVIEW = "flag_review"         # Edge case → needs human decision
    NO_ACTION = "no_action"             # One-off case, not worth automating


# ============================================================
# 1. FETCH RESOLVED TICKETS FROM ZENDESK
# ============================================================

def fetch_resolved_tickets(since_hours: int = 24) -> list[dict]:
    """
    Fetch Zendesk tickets tagged 'callbot' that were resolved
    in the last N hours.
    """
    if not all([ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN]):
        logger.warning("Zendesk credentials not configured")
        return []
    
    since = (datetime.utcnow() - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/search.json"
    params = {
        "query": f'type:ticket tags:callbot status:solved updated>{since}',
        "sort_by": "updated_at",
        "sort_order": "desc",
    }
    
    try:
        response = requests.get(
            url,
            params=params,
            auth=(f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN),
            timeout=10,
        )
        response.raise_for_status()
        
        tickets = response.json().get("results", [])
        
        logger.info("zendesk_tickets_fetched", extra={
            "json_fields": {"count": len(tickets), "since_hours": since_hours}
        })
        
        return tickets
    
    except Exception as e:
        logger.error(f"Zendesk fetch error: {e}")
        return []


def get_ticket_conversation(ticket_id: int) -> list[dict]:
    """Get all comments on a ticket (original question + agent responses)."""
    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/tickets/{ticket_id}/comments.json"
    
    try:
        response = requests.get(
            url,
            auth=(f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN),
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("comments", [])
    except Exception as e:
        logger.error(f"Zendesk comments error: {e}")
        return []


# ============================================================
# 2. CLASSIFY WITH GEMINI — CAN THE BOT LEARN THIS?
# ============================================================

CLASSIFICATION_PROMPT = """Tu es un analyste de service client. Un ticket a été escaladé depuis un callbot vers un agent humain.
L'agent humain a résolu le ticket. Ta mission : déterminer si le callbot peut gérer ce cas la prochaine fois.

Ticket original (question du client via callbot) :
{original_question}

Résolution par l'agent humain :
{resolution}

Tags du ticket : {tags}

Analyse et réponds UNIQUEMENT en JSON :
{{
    "action": "add_to_kb" | "improve_prompt" | "flag_review" | "no_action",
    "confidence": 0.0-1.0,
    "reason": "explication courte",
    "kb_entry": {{
        "question": "formulation type du client",
        "answer": "réponse que le bot devrait donner",
        "keywords": ["mot1", "mot2"]
    }} // seulement si action = add_to_kb
    "prompt_suggestion": "suggestion de modification du prompt" // seulement si action = improve_prompt
}}

Critères :
- add_to_kb : La réponse est factuelle, stable, et applicable à d'autres clients (horaires, politique retour, etc.)
- improve_prompt : Le bot a mal répondu alors qu'il aurait pu (mauvais ton, info incorrecte, etc.)
- flag_review : Cas complexe nécessitant jugement humain (remboursement exceptionnel, cas juridique, etc.)
- no_action : Cas unique, pas de pattern récurrent"""


def classify_ticket(original_question: str, resolution: str, tags: list[str]) -> dict:
    """
    Use Gemini to classify if and how the bot should learn from this ticket.
    """
    try:
        model = GenerativeModel(MODEL_NAME)
        
        prompt = CLASSIFICATION_PROMPT.format(
            original_question=original_question,
            resolution=resolution,
            tags=", ".join(tags),
        )
        
        response = model.generate_content(
            prompt,
            generation_config={"max_output_tokens": 300, "temperature": 0.1},
        )
        
        import re
        json_match = re.search(r'\{[\s\S]*\}', response.text)
        if json_match:
            return json.loads(json_match.group())
    
    except Exception as e:
        logger.error(f"Classification error: {e}")
    
    return {"action": "no_action", "confidence": 0, "reason": "classification failed"}


# ============================================================
# 3. AUTO-ACTIONS — KB UPDATE, PROMPT FIX, FLAG
# ============================================================

def update_knowledge_base(kb_entry: dict) -> bool:
    """
    Add a new Q&A to the knowledge base (data.json).
    Next time a client asks this question → instant answer from KB, no LLM needed.
    """
    try:
        # Load current KB
        with open(KB_PATH, "r", encoding="utf-8") as f:
            kb = json.load(f)
        
        # Check for duplicate (fuzzy match on question)
        new_q = kb_entry["question"].lower()
        for existing in kb:
            existing_q = existing.get("question", existing.get("q", "")).lower()
            if _similarity(new_q, existing_q) > 0.8:
                logger.info("kb_duplicate_skipped", extra={
                    "json_fields": {"question": kb_entry["question"][:50]}
                })
                return False
        
        # Add new entry
        kb.append({
            "question": kb_entry["question"],
            "answer": kb_entry["answer"],
            "keywords": kb_entry.get("keywords", []),
            "source": "feedback_loop",
            "added_at": datetime.utcnow().isoformat(),
        })
        
        # Save
        with open(KB_PATH, "w", encoding="utf-8") as f:
            json.dump(kb, f, ensure_ascii=False, indent=2)
        
        logger.info("kb_updated", extra={
            "json_fields": {
                "question": kb_entry["question"][:50],
                "keywords": kb_entry.get("keywords", []),
            }
        })
        
        return True
    
    except Exception as e:
        logger.error(f"KB update error: {e}")
        return False


def _similarity(a: str, b: str) -> float:
    """Simple word overlap similarity."""
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    overlap = words_a & words_b
    return len(overlap) / max(len(words_a), len(words_b))


def store_prompt_suggestion(ticket_id: int, suggestion: str):
    """
    Store prompt improvement suggestions in Firestore.
    Reviewed weekly by the team.
    """
    db.collection("prompt_suggestions").add({
        "ticket_id": ticket_id,
        "suggestion": suggestion,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
    })
    
    logger.info("prompt_suggestion_stored", extra={
        "json_fields": {"ticket_id": ticket_id, "suggestion": suggestion[:80]}
    })


def flag_for_review(ticket_id: int, reason: str):
    """Flag a ticket pattern for human review."""
    db.collection("flagged_patterns").add({
        "ticket_id": ticket_id,
        "reason": reason,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
    })


# ============================================================
# 4. BIGQUERY TREND ANALYSIS
# ============================================================

def store_feedback_event(ticket_id: int, classification: dict):
    """
    Store every feedback classification in BigQuery for trend analysis.
    
    Questions to answer:
    - What % of escalations could the bot have handled?
    - Which categories cause the most escalations?
    - Is the escalation rate decreasing over time?
    """
    try:
        bq_client = bigquery.Client(project=PROJECT_ID)
        table_id = f"{PROJECT_ID}.callbot_analytics.feedback_events"
        
        row = {
            "ticket_id": ticket_id,
            "action": classification.get("action", "unknown"),
            "confidence": classification.get("confidence", 0),
            "reason": classification.get("reason", ""),
            "timestamp": datetime.utcnow().isoformat(),
        }
        
        errors = bq_client.insert_rows_json(table_id, [row])
        if errors:
            logger.error(f"BigQuery insert error: {errors}")
    
    except Exception as e:
        # BigQuery is nice-to-have, not critical
        logger.warning(f"BigQuery feedback store skipped: {e}")


# ============================================================
# 5. WEEKLY REPORT
# ============================================================

def generate_weekly_report() -> dict:
    """
    Generate a weekly feedback loop report.
    
    Returns:
    {
        "period": "2026-03-04 → 2026-03-11",
        "tickets_processed": 47,
        "actions": {
            "add_to_kb": 12,     ← 12 new KB entries (bot is smarter)
            "improve_prompt": 3,  ← 3 prompt suggestions pending
            "flag_review": 5,     ← 5 cases need human decision
            "no_action": 27,      ← 27 one-off cases
        },
        "kb_growth": "+12 entries (was 156, now 168)",
        "estimated_escalation_reduction": "~25% of this week's tickets",
    }
    """
    week_ago = datetime.utcnow() - timedelta(days=7)
    
    # Count processed feedback from Firestore
    docs = db.collection("feedback_log").where(
        "processed_at", ">=", week_ago.isoformat()
    ).stream()
    
    actions = {"add_to_kb": 0, "improve_prompt": 0, "flag_review": 0, "no_action": 0}
    total = 0
    
    for doc in docs:
        data = doc.to_dict()
        action = data.get("action", "no_action")
        actions[action] = actions.get(action, 0) + 1
        total += 1
    
    report = {
        "period": f"{week_ago.strftime('%Y-%m-%d')} → {datetime.utcnow().strftime('%Y-%m-%d')}",
        "tickets_processed": total,
        "actions": actions,
        "kb_additions": actions.get("add_to_kb", 0),
        "prompt_suggestions_pending": actions.get("improve_prompt", 0),
        "flagged_for_review": actions.get("flag_review", 0),
    }
    
    logger.info("weekly_report_generated", extra={"json_fields": report})
    
    return report


# ============================================================
# MAIN PROCESSOR — Run daily via Cloud Scheduler or cron
# ============================================================

def process_feedback(since_hours: int = 24) -> dict:
    """
    Main entry point. Run daily.
    
    1. Fetch resolved tickets from Zendesk
    2. For each: extract question + resolution
    3. Classify with Gemini
    4. Execute action (KB update / prompt suggestion / flag)
    5. Store in BigQuery + Firestore
    6. Return summary
    """
    results = {
        "processed": 0,
        "add_to_kb": 0,
        "improve_prompt": 0,
        "flag_review": 0,
        "no_action": 0,
        "errors": 0,
    }
    
    tickets = fetch_resolved_tickets(since_hours)
    
    for ticket in tickets:
        try:
            ticket_id = ticket["id"]
            tags = ticket.get("tags", [])
            
            # Get conversation
            comments = get_ticket_conversation(ticket_id)
            if len(comments) < 2:
                continue
            
            # First comment = original question (from callbot)
            original = comments[0].get("body", "")
            # Last non-system comment = human resolution
            resolution = ""
            for c in reversed(comments):
                if not c.get("public", True):
                    continue
                resolution = c.get("body", "")
                break
            
            if not original or not resolution:
                continue
            
            # Classify
            classification = classify_ticket(original, resolution, tags)
            action = classification.get("action", "no_action")
            confidence = classification.get("confidence", 0)
            
            # Only act if confidence > 0.7
            if confidence >= 0.7:
                if action == "add_to_kb" and "kb_entry" in classification:
                    if update_knowledge_base(classification["kb_entry"]):
                        results["add_to_kb"] += 1
                
                elif action == "improve_prompt" and "prompt_suggestion" in classification:
                    store_prompt_suggestion(ticket_id, classification["prompt_suggestion"])
                    results["improve_prompt"] += 1
                
                elif action == "flag_review":
                    flag_for_review(ticket_id, classification.get("reason", ""))
                    results["flag_review"] += 1
                
                else:
                    results["no_action"] += 1
            else:
                results["no_action"] += 1
            
            # Store in BigQuery (all classifications, regardless of confidence)
            store_feedback_event(ticket_id, classification)
            
            # Log to Firestore
            db.collection("feedback_log").add({
                "ticket_id": ticket_id,
                "action": action,
                "confidence": confidence,
                "classification": classification,
                "processed_at": datetime.utcnow().isoformat(),
            })
            
            results["processed"] += 1
        
        except Exception as e:
            logger.error(f"Feedback processing error for ticket: {e}")
            results["errors"] += 1
    
    logger.info("feedback_loop_complete", extra={"json_fields": results})
    
    return results


# ============================================================
# CLOUD SCHEDULER ENDPOINT
# ============================================================

def create_feedback_endpoint(app):
    """
    Register Flask endpoint for Cloud Scheduler to trigger daily.
    
    Cloud Scheduler → POST /feedback/process → runs the loop
    Cloud Scheduler → GET /feedback/report → weekly summary
    """
    
    @app.route("/feedback/process", methods=["POST"])
    def trigger_feedback():
        """Triggered daily by Cloud Scheduler."""
        hours = int(request.args.get("hours", 24))
        results = process_feedback(since_hours=hours)
        return jsonify(results)
    
    @app.route("/feedback/report", methods=["GET"])
    def feedback_report():
        """Weekly report endpoint."""
        report = generate_weekly_report()
        return jsonify(report)
    
    @app.route("/feedback/stats", methods=["GET"])
    def feedback_stats():
        """Current KB size and feedback stats."""
        try:
            with open(KB_PATH, "r") as f:
                kb = json.load(f)
            
            auto_entries = sum(1 for e in kb if e.get("source") == "feedback_loop")
            
            return jsonify({
                "kb_total_entries": len(kb),
                "kb_auto_added": auto_entries,
                "kb_manual": len(kb) - auto_entries,
            })
        except Exception:
            return jsonify({"error": "KB not found"}), 404
