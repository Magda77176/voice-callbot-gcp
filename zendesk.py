"""
Zendesk Integration — Escalation to human agents
When the callbot detects it can't handle a request, it creates a Zendesk ticket
with full conversation context for human follow-up.
"""
import os
import json
import logging
import httpx
from datetime import datetime
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("callbot")

# --- Config ---
ZENDESK_SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN")  # e.g. "mycompany"
ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL")           # agent email
ZENDESK_API_TOKEN = os.getenv("ZENDESK_API_TOKEN")   # API token
ZENDESK_BASE_URL = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"


class EscalationReason(str, Enum):
    """Why the callbot is escalating to a human."""
    CUSTOMER_REQUEST = "customer_request"        # "Je veux parler à quelqu'un"
    LOW_CONFIDENCE = "low_confidence"            # Gemini response uncertain
    SENSITIVE_TOPIC = "sensitive_topic"          # Complaint, legal, refund dispute
    MAX_TURNS_REACHED = "max_turns_reached"      # Too many back-and-forth
    ORDER_ISSUE = "order_issue"                  # Order problem needing human action
    REPEATED_MISUNDERSTANDING = "repeated_misunderstanding"  # STT failures


@dataclass
class EscalationContext:
    """Data sent to Zendesk when escalating."""
    call_sid: str
    caller_phone: str
    order_number: str
    reason: EscalationReason
    conversation_history: list[dict]
    summary: str
    priority: str = "normal"  # low, normal, high, urgent


# --- Escalation Detection ---

HUMAN_REQUEST_KEYWORDS = [
    "parler à quelqu'un", "un humain", "un conseiller", "un agent",
    "transfert", "responsable", "manager", "réclamation",
    "je veux porter plainte", "pas satisfait", "inacceptable"
]

SENSITIVE_KEYWORDS = [
    "remboursement", "avocat", "litige", "plainte",
    "arnaque", "fraude", "volé", "perdu mon colis"
]


def should_escalate(
    user_text: str,
    conversation_history: list[dict],
    gemini_response: str = "",
    stt_failures: int = 0
) -> tuple[bool, EscalationReason | None, str]:
    """
    Determine if the conversation should be escalated to a human.
    
    Returns: (should_escalate, reason, priority)
    """
    normalized = user_text.lower()
    
    # 1. Customer explicitly asks for a human → ALWAYS escalate
    if any(kw in normalized for kw in HUMAN_REQUEST_KEYWORDS):
        priority = "high" if any(kw in normalized for kw in ["réclamation", "responsable", "plainte"]) else "normal"
        return True, EscalationReason.CUSTOMER_REQUEST, priority
    
    # 2. Sensitive topic (complaints, legal) → escalate with high priority
    if any(kw in normalized for kw in SENSITIVE_KEYWORDS):
        return True, EscalationReason.SENSITIVE_TOPIC, "high"
    
    # 3. Too many turns without resolution (>8 user messages)
    user_turns = sum(1 for msg in conversation_history if msg.get("role") == "user")
    if user_turns >= 8:
        return True, EscalationReason.MAX_TURNS_REACHED, "normal"
    
    # 4. Repeated STT failures (caller frustrated)
    if stt_failures >= 3:
        return True, EscalationReason.REPEATED_MISUNDERSTANDING, "normal"
    
    # 5. Gemini response indicates uncertainty
    uncertainty_markers = [
        "je ne suis pas sûr", "je ne peux pas", "je ne sais pas",
        "contactez", "veuillez appeler", "impossible de"
    ]
    if gemini_response and any(marker in gemini_response.lower() for marker in uncertainty_markers):
        # Only escalate if it's happened twice
        uncertain_count = sum(
            1 for msg in conversation_history
            if msg.get("role") == "assistant" and
            any(m in msg.get("content", "").lower() for m in uncertainty_markers)
        )
        if uncertain_count >= 2:
            return True, EscalationReason.LOW_CONFIDENCE, "normal"
    
    return False, None, "normal"


# --- Conversation Summary (for Zendesk ticket) ---

def summarize_conversation(history: list[dict], order_number: str = "") -> str:
    """
    Create a human-readable summary of the conversation for the Zendesk agent.
    No LLM needed — just format the history cleanly.
    """
    lines = []
    
    if order_number:
        lines.append(f"📦 Numéro de commande : {order_number}")
        lines.append("")
    
    lines.append("📞 Transcription de l'appel :")
    lines.append("-" * 40)
    
    for msg in history:
        role = "🧑 Client" if msg["role"] == "user" else "🤖 Bot"
        timestamp = msg.get("timestamp", "")
        time_str = f" ({timestamp})" if timestamp else ""
        lines.append(f"{role}{time_str}: {msg['content']}")
    
    lines.append("-" * 40)
    
    # Extract key topics discussed
    user_messages = [msg["content"] for msg in history if msg["role"] == "user"]
    if user_messages:
        lines.append(f"\n📋 Messages client : {len(user_messages)}")
        lines.append(f"💬 Dernier message : {user_messages[-1]}")
    
    return "\n".join(lines)


# --- Zendesk API ---

async def create_zendesk_ticket(context: EscalationContext) -> dict | None:
    """
    Create a ticket in Zendesk with full conversation context.
    
    Zendesk API: POST /api/v2/tickets.json
    Auth: email/token basic auth
    """
    if not all([ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN]):
        logger.warning("Zendesk not configured — escalation logged but not sent")
        return None
    
    # Map our priority to Zendesk priority
    priority_map = {
        "low": "low",
        "normal": "normal",
        "high": "high",
        "urgent": "urgent"
    }
    
    # Reason descriptions for the ticket
    reason_descriptions = {
        EscalationReason.CUSTOMER_REQUEST: "Le client a demandé à parler à un conseiller",
        EscalationReason.LOW_CONFIDENCE: "Le chatbot n'a pas pu répondre avec certitude",
        EscalationReason.SENSITIVE_TOPIC: "Sujet sensible détecté (réclamation/litige)",
        EscalationReason.MAX_TURNS_REACHED: "Conversation trop longue sans résolution",
        EscalationReason.ORDER_ISSUE: "Problème de commande nécessitant une intervention",
        EscalationReason.REPEATED_MISUNDERSTANDING: "Incompréhensions répétées (problème audio)",
    }
    
    ticket_data = {
        "ticket": {
            "subject": f"[Callbot] Escalade — {reason_descriptions.get(context.reason, 'Raison inconnue')}",
            "comment": {
                "body": context.summary,
                "public": False  # Internal note, not visible to customer
            },
            "priority": priority_map.get(context.priority, "normal"),
            "tags": [
                "callbot",
                "escalation",
                f"reason_{context.reason.value}",
            ],
            "custom_fields": [
                {"id": "call_sid", "value": context.call_sid},
                {"id": "order_number", "value": context.order_number},
                {"id": "caller_phone", "value": context.caller_phone},
            ],
            "metadata": {
                "callbot_version": "2.0-gcp",
                "escalation_reason": context.reason.value,
                "conversation_turns": len(context.conversation_history),
                "timestamp": datetime.utcnow().isoformat(),
            }
        }
    }
    
    # If we have the caller's phone, create/find the requester
    if context.caller_phone:
        ticket_data["ticket"]["requester"] = {
            "name": f"Appelant {context.caller_phone[-4:]}",
            "phone": context.caller_phone,
        }
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{ZENDESK_BASE_URL}/tickets.json",
                json=ticket_data,
                auth=(f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN),
                headers={"Content-Type": "application/json"}
            )
        
        if response.status_code in (200, 201):
            ticket = response.json().get("ticket", {})
            ticket_id = ticket.get("id")
            
            logger.info("zendesk_ticket_created", extra={
                "json_fields": {
                    "ticket_id": ticket_id,
                    "call_sid": context.call_sid,
                    "reason": context.reason.value,
                    "priority": context.priority,
                    "order_number": context.order_number,
                }
            })
            
            return ticket
        else:
            logger.error(f"Zendesk API error: {response.status_code} — {response.text}")
            return None
    
    except Exception as e:
        logger.error(f"Zendesk connection error: {e}")
        return None


async def add_comment_to_ticket(ticket_id: int, comment: str, public: bool = False):
    """Add a follow-up comment to an existing ticket."""
    if not all([ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN]):
        return None
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.put(
                f"{ZENDESK_BASE_URL}/tickets/{ticket_id}.json",
                json={
                    "ticket": {
                        "comment": {
                            "body": comment,
                            "public": public
                        }
                    }
                },
                auth=(f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN),
            )
        return response.status_code in (200, 201)
    except Exception as e:
        logger.error(f"Zendesk comment error: {e}")
        return None


# --- Escalation orchestrator ---

async def escalate_call(
    call_sid: str,
    caller_phone: str,
    order_number: str,
    reason: EscalationReason,
    conversation_history: list[dict],
    priority: str = "normal"
) -> tuple[bool, str]:
    """
    Full escalation flow:
    1. Summarize conversation
    2. Create Zendesk ticket
    3. Return message to tell the caller
    
    Returns: (success, message_for_caller)
    """
    # Build summary
    summary = summarize_conversation(conversation_history, order_number)
    
    context = EscalationContext(
        call_sid=call_sid,
        caller_phone=caller_phone,
        order_number=order_number,
        reason=reason,
        conversation_history=conversation_history,
        summary=summary,
        priority=priority
    )
    
    # Create ticket
    ticket = await create_zendesk_ticket(context)
    
    if ticket:
        ticket_id = ticket.get("id", "")
        caller_message = (
            f"Je transfère votre demande à un conseiller qui vous rappellera très rapidement. "
            f"Votre numéro de dossier est le {ticket_id}. Merci de votre patience."
        )
        return True, caller_message
    else:
        # Zendesk failed — still give a good experience
        caller_message = (
            "Je transfère votre demande à notre équipe. "
            "Un conseiller vous rappellera dans les plus brefs délais."
        )
        return False, caller_message
