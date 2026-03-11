"""
Voice Callbot on GCP — Twilio + Vertex AI Gemini + ElevenLabs + Cloud Run
Production callbot migrated from OpenAI to Google Cloud Platform.

Migration: OpenAI GPT-4o-mini → Vertex AI Gemini 2.0 Flash
           Flask local → Cloud Run (serverless)
           print/logging → Cloud Logging (structured)
           In-memory sessions → Firestore (persistent)
"""
import os
import json
import time
import string
import logging
from datetime import datetime
from difflib import get_close_matches

from flask import Flask, request, send_from_directory, session, jsonify
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient
from elevenlabs import save
from elevenlabs.client import ElevenLabs

import vertexai
from vertexai.generative_models import GenerativeModel, SafetySetting, HarmCategory, HarmBlockThreshold
from google.cloud import firestore, logging as cloud_logging
from google.cloud.logging.handlers import CloudLoggingHandler

from zendesk import should_escalate, escalate_call, EscalationReason

# --- GCP Config ---
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
MODEL_NAME = os.getenv("VERTEX_MODEL", "gemini-2.0-flash-001")

# --- Twilio Config ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

# --- ElevenLabs Config ---
ELEVEN_LABS_API_KEY = os.getenv("ELEVEN_LABS_API_KEY")
VOICE_ID = os.getenv("ELEVENLABS_VOICE", "Claire")

# --- E-commerce Config ---
ECOMMERCE_API_TOKEN = os.getenv("ECOMMERCE_API_TOKEN")
ECOMMERCE_SUBDOMAIN = os.getenv("ECOMMERCE_SUBDOMAIN", "myshop")

# --- Init GCP services ---
vertexai.init(project=PROJECT_ID, location=LOCATION)

gemini_model = GenerativeModel(
    MODEL_NAME,
    safety_settings=[
        SafetySetting(
            category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            threshold=HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
        ),
    ],
)

# Firestore for persistent sessions
db = firestore.Client(project=PROJECT_ID)

# Cloud Logging
logging_client = cloud_logging.Client(project=PROJECT_ID)
cloud_handler = CloudLoggingHandler(logging_client, name="voice-callbot")
logger = logging.getLogger("callbot")
logger.setLevel(logging.INFO)
logger.addHandler(cloud_handler)

# Twilio + ElevenLabs clients
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

# Flask app
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me")


# ============================================================
# KNOWLEDGE BASE
# ============================================================

try:
    with open("datas.json", "r") as f:
        knowledge_base = json.load(f)
except FileNotFoundError:
    knowledge_base = []
    logger.warning("Knowledge base 'datas.json' not found")


def normalize_text(text: str) -> str:
    return text.translate(str.maketrans("", "", string.punctuation)).lower()


def find_best_match(user_query: str) -> str | None:
    """Fuzzy match against knowledge base. Cost: $0."""
    if not knowledge_base:
        return None
    normalized = normalize_text(user_query)
    questions = [normalize_text(item.get("question", "")) for item in knowledge_base]
    matches = get_close_matches(normalized, questions, n=1, cutoff=0.6)
    if matches:
        idx = questions.index(matches[0])
        return knowledge_base[idx].get("answer")
    return None


# ============================================================
# VERTEX AI GEMINI (replaces OpenAI)
# ============================================================

SYSTEM_PROMPT = """Tu es l'assistante vocale d'un service client e-commerce.
Règles strictes :
- Réponds en maximum 2 phrases courtes
- Ton naturel et professionnel (comme au téléphone)
- Pas de markdown, pas de listes, pas d'emojis
- Si tu ne sais pas → dis-le honnêtement
- Ne donne JAMAIS d'informations médicales ou juridiques"""


def get_gemini_response(user_query: str, conversation_history: list[dict]) -> str:
    """
    Call Vertex AI Gemini instead of OpenAI.
    
    Migration notes:
    - openai.ChatCompletion.create() → model.generate_content()
    - GPT-4o-mini → Gemini 2.0 Flash (faster, free tier)
    - API key auth → Application Default Credentials (automatic on Cloud Run)
    """
    # Build prompt with conversation context
    context_parts = [SYSTEM_PROMPT + "\n\nHistorique de la conversation :"]
    for msg in conversation_history[-10:]:
        role = "Client" if msg["role"] == "user" else "Assistant"
        context_parts.append(f"{role}: {msg['content']}")
    context_parts.append(f"Client: {user_query}")
    context_parts.append("Assistant:")
    
    full_prompt = "\n".join(context_parts)
    
    try:
        t0 = time.time()
        response = gemini_model.generate_content(
            full_prompt,
            generation_config={
                "max_output_tokens": 150,
                "temperature": 0.7,
            }
        )
        latency = int((time.time() - t0) * 1000)
        
        answer = response.text.strip()
        
        # Structured logging to Cloud Logging
        logger.info("gemini_call", extra={
            "json_fields": {
                "query": user_query,
                "response": answer[:100],
                "latency_ms": latency,
                "model": MODEL_NAME,
                "tokens": getattr(response, "usage_metadata", {})
            }
        })
        
        return answer
    
    except Exception as e:
        logger.error(f"Gemini error: {e}", extra={
            "json_fields": {"error": str(e), "query": user_query}
        })
        return "Je suis désolée, je ne peux pas répondre pour le moment."


# ============================================================
# FIRESTORE SESSIONS (replaces in-memory dict)
# ============================================================

def get_session_history(call_sid: str) -> list[dict]:
    """Load conversation history from Firestore."""
    doc = db.collection("callbot_sessions").document(call_sid).get()
    if doc.exists:
        return doc.to_dict().get("history", [])
    return []


def save_session_history(call_sid: str, history: list[dict], order_number: str = ""):
    """Save conversation to Firestore."""
    db.collection("callbot_sessions").document(call_sid).set({
        "history": history,
        "order_number": order_number,
        "updated_at": datetime.utcnow().isoformat(),
    }, merge=True)


def add_to_session(call_sid: str, role: str, content: str):
    """Append a message to the session history."""
    history = get_session_history(call_sid)
    history.append({"role": role, "content": content, "timestamp": datetime.utcnow().isoformat()})
    save_session_history(call_sid, history)
    return history


# ============================================================
# TEXT-TO-SPEECH (ElevenLabs — unchanged)
# ============================================================

def speak(text: str) -> str | None:
    """Generate audio via ElevenLabs. Returns filename or None."""
    if not text or not text.strip():
        return None
    
    filename = f"{datetime.now().timestamp()}.mp3"
    try:
        audio = elevenlabs_client.generate(
            text=text,
            voice=VOICE_ID,
            model="eleven_multilingual_v2"
        )
        if not audio:
            return None
        
        audio_path = os.path.join("static", filename)
        save(audio, audio_path)
        
        logger.info("tts_generated", extra={
            "json_fields": {"text_length": len(text), "filename": filename}
        })
        return filename
    except Exception as e:
        logger.error(f"TTS error: {e}")
        return None


# ============================================================
# INTENT DETECTION
# ============================================================

DELIVERY_KEYWORDS = [
    "où est ma commande", "statut de ma commande", "suivi",
    "quand va arriver", "où en est", "date de livraison",
    "combien de temps", "colis", "livraison"
]

RETURN_KEYWORDS = [
    "retourner", "rembourser", "remboursement", "retour",
    "échange", "défectueux", "problème avec"
]


def detect_intent(text: str) -> str:
    """Simple keyword-based intent detection. Cost: $0."""
    normalized = normalize_text(text)
    
    if any(kw in normalized for kw in DELIVERY_KEYWORDS):
        return "delivery_status"
    if any(kw in normalized for kw in RETURN_KEYWORDS):
        return "return_request"
    return "general"


# ============================================================
# E-COMMERCE API
# ============================================================

def get_order_details(order_number: str) -> dict | None:
    """Fetch order from e-commerce API."""
    import requests
    try:
        url = f"https://{ECOMMERCE_SUBDOMAIN}.com/api/orders/{order_number}"
        headers = {"Authorization": f"Bearer {ECOMMERCE_API_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        logger.error(f"Order API error: {e}")
        return None


def format_order_status(order: dict) -> str:
    if not order:
        return "Impossible de récupérer le statut de votre commande."
    status_map = {
        "processing": "en cours de préparation",
        "shipped": "expédiée",
        "delivered": "livrée",
        "pending": "en attente de validation",
        "cancelled": "annulée",
    }
    status = status_map.get(order.get("status", ""), order.get("status", "inconnu"))
    msg = f"Votre commande est {status}."
    tracking = order.get("tracking_number", "")
    carrier = order.get("carrier", "")
    if tracking and carrier:
        msg += f" Le numéro de suivi {carrier} est {tracking}."
    return msg


# ============================================================
# TWILIO VOICE ROUTES
# ============================================================

@app.route("/voice", methods=["POST"])
def voice():
    """Initial webhook — greet and collect order number."""
    response = VoiceResponse()
    call_sid = request.values.get("CallSid", "unknown")
    
    logger.info("call_started", extra={"json_fields": {"call_sid": call_sid}})
    
    gather = Gather(
        input="dtmf",
        action="/handle-order",
        method="POST",
        timeout=10,
        finish_on_key="#"
    )
    
    welcome = speak("Bonjour, bienvenue. Veuillez entrer votre numéro de commande suivi de la touche dièse.")
    if welcome:
        gather.play(f"/static/{welcome}")
    else:
        gather.say("Bonjour, veuillez entrer votre numéro de commande.", language="fr-FR")
    
    response.append(gather)
    response.say("Je n'ai pas reçu de réponse. Au revoir.", language="fr-FR")
    return str(response)


@app.route("/handle-order", methods=["POST"])
def handle_order():
    """Process order number via DTMF."""
    response = VoiceResponse()
    digits = request.values.get("Digits", "")
    call_sid = request.values.get("CallSid", "unknown")
    
    if not digits.startswith("100"):
        error = speak("Le numéro de commande n'est pas valide. Veuillez réessayer.")
        if error:
            response.play(f"/static/{error}")
        response.redirect("/voice")
        return str(response)
    
    # Save order in Firestore
    save_session_history(call_sid, [], order_number=digits)
    
    logger.info("order_lookup", extra={
        "json_fields": {"call_sid": call_sid, "order_number": digits}
    })
    
    order = get_order_details(digits)
    status_text = format_order_status(order) if order else f"Je n'ai pas trouvé la commande numéro {digits}."
    
    # Speak and continue
    gather = Gather(input="speech", action="/handle-speech", method="POST", language="fr-FR", timeout=5)
    
    status_audio = speak(status_text)
    if status_audio:
        gather.play(f"/static/{status_audio}")
    else:
        gather.say(status_text, language="fr-FR")
    
    followup = speak("Avez-vous une autre question ?")
    if followup:
        gather.play(f"/static/{followup}")
    
    response.append(gather)
    return str(response)


@app.route("/handle-speech", methods=["POST"])
def handle_speech():
    """Handle free-form speech — KB → intent → Gemini fallback."""
    response = VoiceResponse()
    speech = request.values.get("SpeechResult", "")
    call_sid = request.values.get("CallSid", "unknown")
    
    if not speech:
        # Track STT failures for escalation logic
        call_sid_tmp = request.values.get("CallSid", "unknown")
        doc_tmp = db.collection("callbot_sessions").document(call_sid_tmp).get()
        failures = (doc_tmp.to_dict() or {}).get("stt_failures", 0) + 1 if doc_tmp.exists else 1
        db.collection("callbot_sessions").document(call_sid_tmp).set(
            {"stt_failures": failures}, merge=True
        )
        
        error = speak("Je n'ai pas compris. Pouvez-vous répéter ?")
        if error:
            response.play(f"/static/{error}")
        response.redirect("/voice")
        return str(response)
    
    # Log user input
    history = add_to_session(call_sid, "user", speech)
    caller_phone = request.values.get("Caller", "")
    
    logger.info("speech_received", extra={
        "json_fields": {"call_sid": call_sid, "text": speech}
    })
    
    # --- Check if escalation needed BEFORE responding ---
    doc = db.collection("callbot_sessions").document(call_sid).get()
    session_data = doc.to_dict() if doc.exists else {}
    order_num = session_data.get("order_number", "")
    stt_failures = session_data.get("stt_failures", 0)
    
    needs_escalation, escalation_reason, priority = should_escalate(
        speech, history, gemini_response="", stt_failures=stt_failures
    )
    
    if needs_escalation:
        import asyncio
        loop = asyncio.new_event_loop()
        success, escalation_msg = loop.run_until_complete(
            escalate_call(call_sid, caller_phone, order_num, escalation_reason, history, priority)
        )
        loop.close()
        
        logger.info("escalation_triggered", extra={
            "json_fields": {
                "call_sid": call_sid,
                "reason": escalation_reason.value,
                "priority": priority,
                "zendesk_success": success,
            }
        })
        
        # Speak the escalation message and hang up
        audio = speak(escalation_msg)
        if audio:
            response.play(f"/static/{audio}")
        else:
            response.say(escalation_msg, language="fr-FR")
        response.say("Au revoir et bonne journée.", language="fr-FR")
        return str(response)
    
    # --- Normal response strategy: KB → intent → Gemini ---
    kb_answer = find_best_match(speech)
    
    if kb_answer:
        ai_response = kb_answer
        source = "knowledge_base"
    else:
        intent = detect_intent(speech)
        
        if intent == "delivery_status":
            if order_num:
                order = get_order_details(order_num)
                ai_response = format_order_status(order) if order else "Je ne trouve pas votre commande."
            else:
                ai_response = "Pouvez-vous me donner votre numéro de commande ?"
            source = "order_api"
        else:
            # Fallback to Vertex AI Gemini
            ai_response = get_gemini_response(speech, history)
            source = "gemini"
            
            # Check if Gemini response itself triggers escalation
            needs_esc2, reason2, prio2 = should_escalate(
                speech, history, gemini_response=ai_response, stt_failures=stt_failures
            )
            if needs_esc2:
                import asyncio
                loop = asyncio.new_event_loop()
                success, escalation_msg = loop.run_until_complete(
                    escalate_call(call_sid, caller_phone, order_num, reason2, history, prio2)
                )
                loop.close()
                ai_response = escalation_msg
                source = "escalation"
    
    # Log response
    add_to_session(call_sid, "assistant", ai_response)
    
    logger.info("response_sent", extra={
        "json_fields": {
            "call_sid": call_sid,
            "source": source,
            "response": ai_response[:100]
        }
    })
    
    # Speak and continue loop
    gather = Gather(input="speech", action="/handle-speech", method="POST", language="fr-FR", timeout=5)
    
    audio = speak(ai_response)
    if audio:
        gather.play(f"/static/{audio}")
    else:
        gather.say(ai_response, language="fr-FR")
    
    response.append(gather)
    return str(response)


# ============================================================
# API ENDPOINTS (monitoring + admin)
# ============================================================

@app.route("/health")
def health():
    """Health check for Cloud Run."""
    return jsonify({
        "status": "healthy",
        "model": MODEL_NAME,
        "project": PROJECT_ID,
    })


@app.route("/stats")
def stats():
    """Call statistics from Firestore."""
    sessions_ref = db.collection("callbot_sessions").order_by(
        "updated_at", direction=firestore.Query.DESCENDING
    ).limit(100).stream()
    
    total = 0
    today = 0
    escalated = 0
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    
    for s in sessions_ref:
        total += 1
        data = s.to_dict()
        if data.get("updated_at", "").startswith(today_str):
            today += 1
        if data.get("escalated"):
            escalated += 1
    
    return jsonify({
        "total_sessions": total,
        "today_sessions": today,
        "escalated_sessions": escalated,
        "escalation_rate": f"{(escalated/total*100):.1f}%" if total > 0 else "0%",
    })


@app.route("/static/<filename>")
def serve_static(filename):
    return send_from_directory("static", filename)


if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)
