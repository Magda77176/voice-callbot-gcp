# 📞 Voice Callbot on GCP — Vertex AI + Cloud Run + Firestore

Production voice callbot migrated from OpenAI to Google Cloud Platform. Demonstrates real-world cloud migration: same business logic, new infrastructure.

## Migration: Before → After

| Component | Before (OpenAI) | After (GCP) |
|-----------|-----------------|-------------|
| **LLM** | OpenAI GPT-4o-mini | Vertex AI Gemini 2.0 Flash |
| **Auth** | API key in env | Application Default Credentials |
| **Sessions** | In-memory dict | Cloud Firestore (persistent) |
| **Logging** | Python logging | Cloud Logging (structured JSON) |
| **Hosting** | VPS + Flask | Cloud Run (serverless, auto-scale) |
| **Secrets** | .env file | Secret Manager |
| **Cost** | ~$0.15/1K calls (OpenAI) | ~$0 (Gemini free tier) |

## Architecture

```
                    ┌──────────────┐
                    │  Customer    │
                    │  (Phone)     │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   Twilio     │
                    │   (Voice)    │
                    └──────┬───────┘
                           │ Webhook
                    ┌──────▼───────────────┐
                    │   Cloud Run          │
                    │   (Flask + Gunicorn) │
                    └──┬────┬────┬────┬───┘
                       │    │    │    │
              ┌────────▼┐ ┌▼────▼┐ ┌▼──────────┐
              │ Vertex   │ │Fire- │ │ Cloud     │
              │ AI       │ │store │ │ Logging   │
              │ (Gemini) │ │      │ │           │
              └──────────┘ └──────┘ └───────────┘
                       │
              ┌────────▼──────────┐
              │   ElevenLabs      │
              │   (TTS - Claire)  │
              └───────────────────┘
```

## Call Flow

```
1. Customer calls → Twilio webhook → /voice
2. Welcome TTS → "Entrez votre numéro de commande"
3. DTMF digits → /handle-order
4. Order lookup via e-commerce API
5. Status spoken via ElevenLabs TTS
6. Free-form Q&A loop → /handle-speech
   a. Speech-to-Text (Twilio built-in)
   b. Knowledge base fuzzy match ($0)
   c. Intent detection (delivery/return) ($0)
   d. Vertex AI Gemini fallback (free tier)
   e. Text-to-Speech (ElevenLabs)
   f. Loop until hangup
```

## Response Strategy (cost optimization)

```
User speaks
  │
  ├─ Knowledge base match? → Use cached answer       [$0]
  │
  ├─ Delivery intent? → Call order API                [$0]
  │
  └─ General question → Vertex AI Gemini 2.0 Flash   [$0 free tier]
                                                       │
                                              Cloud Logging ← structured JSON
                                              Firestore ← session history
```

## GCP Services Used

| Service | Purpose | Free Tier |
|---------|---------|-----------|
| **Cloud Run** | Serverless hosting | 2M requests/month |
| **Vertex AI** | Gemini 2.0 Flash LLM | 15 req/min free |
| **Firestore** | Session persistence | 1 GiB storage free |
| **Cloud Logging** | Structured logs + monitoring | 50 GiB/month free |
| **Secret Manager** | API keys storage | 6 active versions free |

## Structured Logging

Every interaction generates structured JSON in Cloud Logging:

```json
{
  "severity": "INFO",
  "message": "gemini_call",
  "jsonPayload": {
    "call_sid": "CA1234...",
    "query": "Quand arrive ma commande ?",
    "response": "Votre commande est en cours...",
    "latency_ms": 450,
    "model": "gemini-2.0-flash-001",
    "source": "gemini"
  }
}
```

Query in Cloud Logging:
```
resource.type="cloud_run_revision"
jsonPayload.source="gemini"
jsonPayload.latency_ms > 2000
```

## Firestore Sessions

Each call creates a document in `callbot_sessions/{call_sid}`:

```json
{
  "order_number": "100456789",
  "history": [
    {"role": "user", "content": "Où est ma commande ?", "timestamp": "..."},
    {"role": "assistant", "content": "Votre commande est expédiée.", "timestamp": "..."}
  ],
  "updated_at": "2026-03-11T15:00:00Z"
}
```

Benefits vs in-memory:
- Survives Cloud Run cold starts
- Queryable (analytics)
- Automatic backup

## Deploy

```bash
# One command
./deploy.sh

# Or manually
gcloud run deploy voice-callbot \
    --source . \
    --region us-central1 \
    --project jarvis-v2-488311 \
    --allow-unauthenticated

# Configure Twilio
# Set voice webhook URL to: https://voice-callbot-xxx.run.app/voice
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/voice` | Twilio initial webhook |
| POST | `/handle-order` | Process DTMF order number |
| POST | `/handle-speech` | Process speech input |
| GET | `/health` | Health check |
| GET | `/stats` | Call statistics from Firestore |

## Local Development

```bash
pip install -r requirements.txt
export GOOGLE_CLOUD_PROJECT=your-project
gcloud auth application-default login
python app.py
# Use ngrok for Twilio webhook: ngrok http 8080
```

## File Structure

```
├── app.py              # Main server (Flask + Vertex AI + Firestore)
├── datas.json          # Knowledge base (Q&A pairs)
├── Dockerfile          # Cloud Run container
├── deploy.sh           # One-click deployment script
└── requirements.txt    # Python dependencies
```
