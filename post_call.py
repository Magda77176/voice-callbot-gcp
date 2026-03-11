"""
Post-Call Processing — Summary, CRM update, satisfaction survey.
Runs async after the call ends (via Pub/Sub or direct call).
"""
import os
import json
import logging
import time
from datetime import datetime

import vertexai
from vertexai.generative_models import GenerativeModel
from google.cloud import firestore, storage

logger = logging.getLogger("callbot")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
MODEL_NAME = os.getenv("VERTEX_MODEL", "gemini-2.0-flash-001")
BUCKET_NAME = os.getenv("GCS_RECORDINGS_BUCKET", f"{PROJECT_ID}-call-recordings")

db = firestore.Client(project=PROJECT_ID)


# ============================================================
# CALL SUMMARY (Gemini)
# ============================================================

SUMMARY_PROMPT = """Résume cet appel de service client en 3-5 lignes.

Transcription :
{transcript}

Format du résumé :
- Objet de l'appel : (en 1 phrase)
- Résolution : (résolu par bot / escaladé / abandonné)
- Action requise : (aucune / rappeler client / traiter réclamation / etc.)
- Sentiment client : (satisfait / neutre / frustré / en colère)
- Tags : (livraison, retour, paiement, réclamation, info produit, etc.)

Réponds UNIQUEMENT en JSON :
{{"subject": "...", "resolution": "...", "action_required": "...", "sentiment": "...", "tags": ["..."]}}"""


async def generate_call_summary(call_sid: str) -> dict | None:
    """
    Generate an AI summary of the call for CRM integration.
    Called async after the call ends.
    """
    # Load conversation from Firestore
    doc = db.collection("callbot_sessions").document(call_sid).get()
    if not doc.exists:
        return None
    
    data = doc.to_dict()
    history = data.get("history", [])
    
    if not history:
        return None
    
    # Build transcript
    transcript_lines = []
    for msg in history:
        role = "Client" if msg["role"] == "user" else "Bot"
        transcript_lines.append(f"{role}: {msg['content']}")
    transcript = "\n".join(transcript_lines)
    
    # Call Gemini
    try:
        model = GenerativeModel(MODEL_NAME)
        response = model.generate_content(
            SUMMARY_PROMPT.format(transcript=transcript),
            generation_config={"max_output_tokens": 200, "temperature": 0.1}
        )
        
        import re
        json_match = re.search(r'\{[^}]+\}', response.text)
        if json_match:
            summary = json.loads(json_match.group())
            
            # Store summary in Firestore
            db.collection("callbot_sessions").document(call_sid).set(
                {"summary": summary, "summarized_at": datetime.utcnow().isoformat()},
                merge=True
            )
            
            logger.info("call_summary_generated", extra={
                "json_fields": {
                    "call_sid": call_sid,
                    "subject": summary.get("subject", ""),
                    "resolution": summary.get("resolution", ""),
                    "tags": summary.get("tags", []),
                }
            })
            
            return summary
    
    except Exception as e:
        logger.error(f"Summary generation error: {e}")
    
    return None


# ============================================================
# CALL RECORDING STORAGE (Cloud Storage)
# ============================================================

def store_recording(call_sid: str, audio_url: str):
    """
    Download Twilio call recording and store in Cloud Storage.
    Useful for: quality assurance, dispute resolution, training.
    
    Recordings are stored with metadata for easy retrieval.
    """
    try:
        import requests
        
        # Download from Twilio
        response = requests.get(audio_url, timeout=30)
        if response.status_code != 200:
            logger.error(f"Recording download failed: {response.status_code}")
            return None
        
        # Upload to Cloud Storage
        client = storage.Client(project=PROJECT_ID)
        bucket = client.bucket(BUCKET_NAME)
        
        date_prefix = datetime.utcnow().strftime("%Y/%m/%d")
        blob_name = f"recordings/{date_prefix}/{call_sid}.wav"
        blob = bucket.blob(blob_name)
        
        blob.upload_from_string(
            response.content,
            content_type="audio/wav"
        )
        
        # Set metadata
        blob.metadata = {
            "call_sid": call_sid,
            "recorded_at": datetime.utcnow().isoformat(),
        }
        blob.patch()
        
        gcs_uri = f"gs://{BUCKET_NAME}/{blob_name}"
        
        # Update Firestore with recording location
        db.collection("callbot_sessions").document(call_sid).set(
            {"recording_uri": gcs_uri}, merge=True
        )
        
        logger.info("recording_stored", extra={
            "json_fields": {"call_sid": call_sid, "gcs_uri": gcs_uri}
        })
        
        return gcs_uri
    
    except Exception as e:
        logger.error(f"Recording storage error: {e}")
        return None


# ============================================================
# SLACK NOTIFICATION (on escalation)
# ============================================================

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")


async def notify_slack_escalation(call_sid: str, reason: str, 
                                   priority: str, summary: str):
    """
    Send a Slack notification when a call is escalated.
    Real-time alert for the support team.
    """
    if not SLACK_WEBHOOK_URL:
        return
    
    import httpx
    
    priority_emoji = {"low": "🟢", "normal": "🟡", "high": "🟠", "urgent": "🔴"}
    emoji = priority_emoji.get(priority, "⚪")
    
    message = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} Escalade Callbot — {priority.upper()}"
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Call SID:*\n`{call_sid}`"},
                    {"type": "mrkdwn", "text": f"*Raison:*\n{reason}"},
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Résumé:*\n{summary[:300]}"
                }
            },
        ]
    }
    
    try:
        async with httpx.AsyncClient() as client:
            await client.post(SLACK_WEBHOOK_URL, json=message, timeout=5)
        
        logger.info("slack_notification_sent", extra={
            "json_fields": {"call_sid": call_sid, "priority": priority}
        })
    except Exception as e:
        logger.error(f"Slack notification error: {e}")


# ============================================================
# POST-CALL SURVEY (SMS via Twilio)
# ============================================================

def send_satisfaction_survey(caller_phone: str, call_sid: str):
    """
    Send a post-call satisfaction SMS via Twilio.
    "Sur une échelle de 1 à 5, comment évaluez-vous votre expérience ?"
    """
    from twilio.rest import Client as TwilioClient
    
    TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_PHONE = os.getenv("TWILIO_PHONE_NUMBER")
    
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_PHONE, caller_phone]):
        return
    
    try:
        client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        
        message = client.messages.create(
            body=(
                "Merci de votre appel ! "
                "Sur une échelle de 1 à 5, comment évaluez-vous votre expérience ? "
                "Répondez simplement avec un chiffre."
            ),
            from_=TWILIO_PHONE,
            to=caller_phone,
        )
        
        # Store survey sent status
        db.collection("callbot_sessions").document(call_sid).set(
            {"survey_sent": True, "survey_message_sid": message.sid},
            merge=True
        )
        
        logger.info("survey_sent", extra={
            "json_fields": {
                "call_sid": call_sid,
                "phone": caller_phone[-4:],  # Last 4 digits only
            }
        })
    except Exception as e:
        logger.error(f"Survey SMS error: {e}")
