"""
Pub/Sub Events — Async event processing for post-call workflows.
Decouples call handling from analytics, CRM updates, and notifications.

Events published during calls:
- call.started → log, init session
- call.turn → sentiment tracking, analytics
- call.escalated → notify team, create ticket
- call.ended → post-call survey, CRM update, analytics aggregate
"""
import os
import json
import logging
from datetime import datetime
from google.cloud import pubsub_v1

logger = logging.getLogger("callbot")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
TOPIC_ID = os.getenv("PUBSUB_TOPIC", "callbot-events")

publisher = None


def get_publisher():
    """Lazy init publisher (avoid cold start cost if not used)."""
    global publisher
    if publisher is None:
        publisher = pubsub_v1.PublisherClient()
    return publisher


def publish_event(event_type: str, data: dict):
    """
    Publish an event to the callbot-events topic.
    
    Subscribers can process these async:
    - Analytics worker → aggregate stats in BigQuery
    - CRM worker → update customer record
    - Notification worker → Slack/Teams alert on escalation
    - Survey worker → send post-call satisfaction survey
    """
    try:
        pub = get_publisher()
        topic_path = pub.topic_path(PROJECT_ID, TOPIC_ID)
        
        message = {
            "event_type": event_type,
            "timestamp": datetime.utcnow().isoformat(),
            "data": data,
        }
        
        future = pub.publish(
            topic_path,
            json.dumps(message).encode("utf-8"),
            event_type=event_type,  # Attribute for filtering
        )
        
        message_id = future.result(timeout=5)
        
        logger.info("pubsub_published", extra={
            "json_fields": {
                "event_type": event_type,
                "message_id": message_id,
            }
        })
        
        return message_id
    
    except Exception as e:
        logger.error(f"Pub/Sub publish error: {e}")
        return None


# --- Event helpers ---

def emit_call_started(call_sid: str, caller_phone: str):
    """Emitted when a new call begins."""
    publish_event("call.started", {
        "call_sid": call_sid,
        "caller_phone": caller_phone,
    })


def emit_call_turn(call_sid: str, user_text: str, bot_response: str, 
                    source: str, sentiment: str, frustration_score: int):
    """Emitted after each conversation turn."""
    publish_event("call.turn", {
        "call_sid": call_sid,
        "user_text": user_text,
        "bot_response": bot_response[:200],
        "source": source,
        "sentiment": sentiment,
        "frustration_score": frustration_score,
    })


def emit_call_escalated(call_sid: str, reason: str, priority: str,
                         ticket_id: str = None):
    """Emitted when a call is escalated to a human."""
    publish_event("call.escalated", {
        "call_sid": call_sid,
        "reason": reason,
        "priority": priority,
        "ticket_id": ticket_id,
    })


def emit_call_ended(call_sid: str, total_turns: int, 
                     resolution: str, duration_seconds: int = 0):
    """Emitted when a call ends (hangup or escalation)."""
    publish_event("call.ended", {
        "call_sid": call_sid,
        "total_turns": total_turns,
        "resolution": resolution,  # "resolved", "escalated", "abandoned"
        "duration_seconds": duration_seconds,
    })


# --- Subscriber example (for documentation) ---

SUBSCRIBER_EXAMPLE = """
# Example: Analytics subscriber that writes to BigQuery

from google.cloud import pubsub_v1, bigquery

subscriber = pubsub_v1.SubscriberClient()
subscription_path = subscriber.subscription_path(PROJECT_ID, "callbot-analytics-sub")

bq = bigquery.Client()

def callback(message):
    event = json.loads(message.data)
    
    if event["event_type"] == "call.ended":
        row = {
            "call_sid": event["data"]["call_sid"],
            "total_turns": event["data"]["total_turns"],
            "resolution": event["data"]["resolution"],
            "timestamp": event["timestamp"],
        }
        bq.insert_rows_json("dataset.call_analytics", [row])
    
    message.ack()

subscriber.subscribe(subscription_path, callback=callback)
"""
