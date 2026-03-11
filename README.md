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
| **Escalation** | None (dead end) | Zendesk ticket + full transcript |
| **Sentiment** | None | Real-time frustration scoring |
| **Caching** | None | LRU cache (500 entries, 1h TTL) |
| **Analytics** | None | Firestore aggregations + API |
| **Tracing** | None | OpenTelemetry → Cloud Trace |
| **Tests** | None | 23 tests (pytest) |
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
   b. ⚡ Escalation check (before responding)
   c. Knowledge base fuzzy match ($0)
   d. Intent detection (delivery/return) ($0)
   e. Vertex AI Gemini fallback (free tier)
   f. ⚡ Post-response escalation check
   g. Text-to-Speech (ElevenLabs)
   h. Loop until hangup or escalation
```

## Zendesk Escalation

The callbot automatically detects when a human agent is needed and creates a Zendesk ticket with full conversation context.

### Escalation Triggers

| Trigger | Priority | Example |
|---------|----------|---------|
| Customer asks for human | High | "Je veux parler à quelqu'un" |
| Sensitive topic | High | "remboursement", "réclamation", "avocat" |
| Max turns reached (8+) | Normal | Long unresolved conversation |
| Repeated STT failures (3+) | Normal | Bad audio quality |
| Low confidence (2+ uncertain) | Normal | Bot says "je ne sais pas" twice |

### What happens on escalation

```
Escalation detected
  → Summarize conversation (no LLM, just formatting)
  → Create Zendesk ticket with:
     - Full transcript
     - Order number
     - Caller phone
     - Escalation reason
     - Priority level
  → Tell caller: "Je transfère votre demande. Votre numéro de dossier est le XXX."
  → End call gracefully
```

### Zendesk Ticket Format

```
Subject: [Callbot] Escalade — Le client a demandé à parler à un conseiller
Priority: High
Tags: callbot, escalation, reason_customer_request

Body:
📦 Numéro de commande : 100456789

📞 Transcription de l'appel :
----------------------------------------
🧑 Client: Où est ma commande ?
🤖 Bot: Votre commande est en cours de livraison.
🧑 Client: Ça fait 2 semaines ! Je veux parler à quelqu'un.
----------------------------------------

📋 Messages client : 2
💬 Dernier message : Je veux parler à quelqu'un.
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
| **Zendesk API** | Ticket creation on escalation | Included in Zendesk plan |

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

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/voice` | Twilio initial webhook |
| POST | `/handle-order` | Process DTMF order number |
| POST | `/handle-speech` | Process speech + sentiment + escalation |
| GET | `/health` | Health check (model, project) |
| GET | `/stats` | Session stats + escalation rate |
| GET | `/analytics` | Daily call analytics (add `?date=YYYY-MM-DD`) |
| GET | `/analytics/performance` | 30-day performance summary |
| GET | `/analytics/unanswered` | Top questions for KB improvement |
| GET | `/cache/stats` | Cache hit rate and performance |
| POST | `/cache/clear` | Flush response cache |

## Response Pipeline

```
User speaks
  │
  ├─ Sentiment analysis (instant, rule-based)
  │    └─ Frustration >= 8? → URGENT escalation to Zendesk
  │
  ├─ Escalation check (customer request, sensitive topic, max turns)
  │    └─ Escalate? → Create Zendesk ticket → End call gracefully
  │
  ├─ Cache lookup (LRU, case-insensitive)
  │    └─ Cache hit? → Return cached response (skip LLM)
  │
  ├─ Knowledge base fuzzy match ($0)
  │    └─ Match? → Return KB answer
  │
  ├─ Intent detection (delivery/return keywords, $0)
  │    └─ Delivery? → Fetch order API
  │
  └─ Vertex AI Gemini (free tier)
       └─ Post-response confidence check → maybe escalate
       └─ Cache response for future calls
```

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
├── zendesk.py          # Zendesk escalation (detection + ticket creation)
├── sentiment.py        # Real-time frustration analysis (rules + Gemini)
├── analytics.py        # Call analytics and reporting from Firestore
├── cache.py            # LRU response cache (reduce Gemini calls)
├── tracing.py          # OpenTelemetry → Cloud Trace
├── datas.json          # Knowledge base (Q&A pairs)
├── tests/
│   └── test_callbot.py # 23 tests (sentiment, escalation, cache)
├── Dockerfile          # Cloud Run container
├── deploy.sh           # One-click deployment script
└── requirements.txt    # Python dependencies
```

## Advanced Features

### Streaming Pipeline (`streaming.py`)
LLM + TTS run in parallel. Gemini streams tokens → sentence splitter → TTS generates audio for first sentence while Gemini continues. Perceived latency: ~200ms instead of ~800ms.

### Twilio Media Streams (`media_stream.py`)
Bidirectional WebSocket for raw audio. Enables:
- **Interrupt detection** — caller speaks while bot is talking → bot stops
- **Voice Activity Detection** — detect silence for end-of-speech
- **Sub-200ms latency** — no HTTP round-trips

### TTS Audio Cache (`tts_cache.py`)
Two-tier caching:
- **Static phrases** — pre-generated at startup (welcome, error, goodbye = 0ms)
- **Dynamic cache** — LRU 200 entries, generated once then cached

### Post-Call Processing (`post_call.py`)
- **AI Summary** — Gemini summarizes the call for CRM (subject, resolution, tags)
- **Cloud Storage** — call recordings stored in GCS
- **Slack alerts** — real-time notification on escalation
- **SMS survey** — post-call satisfaction via Twilio SMS

### Feedback Loop (`feedback_loop.py`)
Closed-loop learning from Zendesk tickets:
1. Fetch resolved callbot tickets daily (Cloud Scheduler)
2. Gemini classifies: can the bot handle this next time?
3. Auto-actions: add to KB / suggest prompt fix / flag for review
4. BigQuery trend analysis (escalation rate over time)
5. Weekly report (Slack/email)

Result: knowledge base grows automatically, escalation rate drops week over week.

### Multi-Language (`multilang.py`)
Auto-detect language from first sentence. Switch: Twilio STT + Gemini prompt + ElevenLabs voice.
Supported: French, English, Spanish, German.

## Complete File Structure

```
├── app.py              # Main server + Twilio webhooks
├── streaming.py        # Gemini + TTS parallel streaming pipeline
├── media_stream.py     # Twilio Media Streams (bidirectional WebSocket)
├── sentiment.py        # Real-time frustration analysis
├── zendesk.py          # Zendesk escalation (6 triggers)
├── dlp_guard.py        # Cloud DLP data protection
├── cache.py            # Response text cache (LRU)
├── tts_cache.py        # Audio file cache (static + dynamic)
├── analytics.py        # Call analytics from Firestore
├── post_call.py        # Summary, recording, Slack, SMS survey
├── multilang.py        # Multi-language auto-detection
├── feedback_loop.py    # Zendesk → Gemini → KB auto-update (closed loop)
├── pubsub_events.py    # Async event publishing
├── tracing.py          # OpenTelemetry → Cloud Trace
├── datas.json          # Knowledge base
├── docs/
│   └── ARCHITECTURE.md # Full system architecture
├── tests/
│   └── test_callbot.py # 23 tests
├── .github/
│   └── workflows/
│       └── deploy.yml  # CI/CD
├── Dockerfile
├── deploy.sh
└── requirements.txt
```
