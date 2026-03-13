# 📞 Voice Callbot on GCP — Vertex AI + Cloud Run + Firestore

Production voice callbot migrated from OpenAI to Google Cloud Platform. 18 modules, 12 GCP services, self-improving via feedback loop.

## Migration: Before → After

| Component | Before (OpenAI) | After (GCP) |
|-----------|-----------------|-------------|
| **LLM** | OpenAI GPT-4o-mini | Vertex AI Gemini 2.0 Flash |
| **Auth** | API key in env | Application Default Credentials |
| **Sessions** | In-memory dict | Cloud Firestore (persistent) |
| **Logging** | print() | Cloud Logging (structured JSON) |
| **Hosting** | VPS + Flask | Cloud Run (serverless, auto-scale) |
| **Secrets** | .env file | Secret Manager |
| **Data protection** | None | Cloud DLP (scan before TTS) |
| **Events** | None | Pub/Sub (async analytics, CRM, alerts) |
| **Tracing** | None | OpenTelemetry → Cloud Trace |
| **Recordings** | None | Cloud Storage (GCS) |
| **Analytics** | None | BigQuery (trend analysis) |
| **CI/CD** | SSH + manual | GitHub Actions → Cloud Build |
| **Escalation** | Dead end | Zendesk + AI summary + Slack alert |
| **Sentiment** | None | Real-time frustration detection |
| **Caching** | None | Response LRU + TTS audio cache |
| **Languages** | French only | Auto-detect (fr/en/es/de) |
| **Learning** | Static | Feedback loop (Zendesk → KB auto-update) |
| **Latency** | ~800ms | ~200ms (streaming pipeline) |
| **Cost** | ~$0.15/1K calls | ~$0 (Gemini free tier) |

## Architecture

```
                         ┌──────────┐
                         │ Customer │
                         │ (Phone)  │
                         └────┬─────┘
                              │
                         ┌────▼─────┐
                         │  Twilio  │
                         └──┬────┬──┘
                  Webhook   │    │  Media Stream (WebSocket)
                            │    │
                    ┌───────▼────▼──────────────────────┐
                    │        Cloud Run (Flask)           │
                    │                                    │
                    │  ┌─────────────────────────────┐   │
                    │  │ Request Pipeline:            │   │
                    │  │                              │   │
                    │  │ 1. Sentiment analysis        │   │
                    │  │ 2. Escalation check          │   │
                    │  │ 3. TTS cache lookup          │   │
                    │  │ 4. Response cache lookup     │   │
                    │  │ 5. Knowledge base match      │   │
                    │  │ 6. Gemini streaming          │   │
                    │  │ 7. DLP scan                  │   │
                    │  │ 8. Cache store               │   │
                    │  │ 9. TTS generation            │   │
                    │  │ 10. Pub/Sub event            │   │
                    │  └─────────────────────────────┘   │
                    └──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬─────┘
                       │  │  │  │  │  │  │  │  │  │
         ┌─────────────┘  │  │  │  │  │  │  │  │  └──────────────┐
         ▼                ▼  │  ▼  │  ▼  │  ▼  │                 ▼
    ┌─────────┐  ┌──────────┐│┌────┐│┌────┐│┌───────┐  ┌──────────────┐
    │Vertex AI│  │Firestore ││|Pub/││|DLP ││|Cloud  │  │  BigQuery    │
    │(Gemini) │  │(sessions)││|Sub ││|    ││|Trace  │  │  (analytics) │
    └─────────┘  └──────────┘│└────┘│└────┘│└───────┘  └──────────────┘
                             │      │      │
                    ┌────────┘  ┌───┘  ┌───┘
                    ▼           ▼      ▼
               ┌────────┐ ┌────────┐ ┌───────────┐
               │Cloud   │ │Secret  │ │Cloud      │
               │Logging │ │Manager │ │Storage    │
               └────────┘ └────────┘ │(recordings│
                                     └───────────┘
                       │
              ┌────────▼──────────┐
              │   ElevenLabs TTS  │──→ Twilio (play audio)
              └───────────────────┘

                    ┌───────────────────────────────────┐
                    │      Feedback Loop (daily)        │
                    │                                   │
                    │  Zendesk (resolved tickets)       │
                    │      │                            │
                    │      ▼ Gemini classifies          │
                    │      │                            │
                    │      ├── Add to KB (auto)         │
                    │      ├── Prompt suggestion        │
                    │      └── Flag for review          │
                    │                                   │
                    │  → Bot gets smarter every week    │
                    └───────────────────────────────────┘
```

## 12 GCP Services

| # | Service | Purpose |
|---|---------|---------|
| 1 | **Cloud Run** | Serverless hosting (auto-scale 0→N) |
| 2 | **Vertex AI** | Gemini 2.0 Flash for responses |
| 3 | **Firestore** | Persistent sessions + call history |
| 4 | **Cloud Logging** | Structured JSON logs + alerts |
| 5 | **Cloud DLP** | Scan responses before TTS (mask PII) |
| 6 | **Pub/Sub** | Async events (analytics, CRM, Slack) |
| 7 | **Cloud Trace** | OpenTelemetry distributed tracing |
| 8 | **Secret Manager** | API keys (Twilio, ElevenLabs, Zendesk) |
| 9 | **Artifact Registry** | Docker images for Cloud Run |
| 10 | **Cloud Build** | CI/CD build step |
| 11 | **Cloud Storage** | Call recordings archival |
| 12 | **BigQuery** | Feedback loop trend analysis |

## Call Flow

```
1. Customer calls → Twilio webhook → /voice
2. Welcome TTS (pre-cached, 0ms) → "Entrez votre numéro de commande"
3. DTMF digits → /handle-order → order status via API
4. Free-form Q&A loop → /handle-speech:
   a. Sentiment analysis (rule-based, <1ms)
   b. Escalation check (frustration, keywords, turn count)
   c. TTS cache check (static phrases = 0ms)
   d. Response cache check (LRU 500 entries)
   e. Knowledge base fuzzy match ($0)
   f. Gemini streaming (first sentence in ~200ms)
   g. DLP scan (mask phone, email, IBAN before TTS)
   h. TTS generation (parallel with Gemini streaming)
   i. Pub/Sub event (async, non-blocking)
   j. Loop until hangup or escalation
5. Post-call: AI summary → Firestore, recording → GCS, SMS survey
```

## Streaming Pipeline (4x faster)

```
Without streaming:
  Gemini (500ms) ──────→ TTS (300ms) ──────→ Play
                    800ms silence

With streaming:
  Gemini stream ──→ Sentence 1 detected (200ms) ──→ TTS ──→ Play
                 ──→ Sentence 2 ──→ TTS ──→ Play (seamless)
                    200ms silence, then continuous
```

The `StreamingPipeline` runs Gemini and TTS in parallel threads, connected via sentence queues.

## Twilio Media Streams (WebSocket)

Beyond the webhook model — bidirectional audio via WebSocket:
- **Interrupt detection**: caller speaks while bot is talking → bot stops immediately
- **Voice Activity Detection**: energy-based silence detection (800ms threshold)
- **Sub-200ms latency**: no HTTP round-trips per turn

## Zendesk Escalation (6 triggers)

| Trigger | Priority | Detection |
|---------|----------|-----------|
| Customer requests human | High | Keywords: "parler à quelqu'un", "un conseiller" |
| Sensitive topic | High | "remboursement", "réclamation", "avocat" |
| 8+ turns unresolved | Normal | Turn counter |
| 3+ STT failures | Normal | Consecutive recognition failures |
| Frustration score ≥ 8 | Urgent | Real-time sentiment analysis |
| Bot uncertain 2x | Normal | Low-confidence response detection |

Ticket includes: full transcript, order number, caller phone, escalation reason, priority.

## Feedback Loop (self-improving bot)

```
Daily (Cloud Scheduler):
  1. Fetch Zendesk tickets resolved by humans (tag: callbot)
  2. Gemini classifies each: "Can the bot handle this next time?"
  3. Actions:
     - add_to_kb (confidence > 0.7) → auto-enrich data.json
     - improve_prompt → suggestion stored, reviewed weekly
     - flag_review → edge case, human decision needed
  4. BigQuery stores all events → track escalation rate over time
  5. Weekly Slack report: KB growth, escalation trends
```

**Result**: knowledge base grows automatically, escalation rate drops week over week.

## Sentiment Analysis

Two-layer detection:
- **Layer 1 (rules, <1ms)**: keyword scoring 0-10 ("inacceptable" = 5, "arnaque" = 7)
- **Layer 2 (Gemini)**: sarcasm, irony, passive aggression ("ah c'est formidable...")
- **Trend tracking**: rising frustration across turns → proactive escalation BEFORE client asks

## TTS Audio Cache (two-tier)

- **Static** (11 phrases pre-generated at boot): welcome, error, goodbye = **0ms**
- **Dynamic** (LRU 200 entries): first generation cached, reused for subsequent calls

## Multi-Language

Auto-detect language from first user sentence (keyword matching):
- Switch Twilio STT language
- Switch Gemini system prompt
- Switch ElevenLabs voice
- Supported: French 🇫🇷, English 🇬🇧, Spanish 🇪🇸, German 🇩🇪

## Post-Call Processing

| Feature | Service | Description |
|---------|---------|-------------|
| AI Summary | Vertex AI | Gemini generates: subject, resolution, action, sentiment, tags |
| Recording | Cloud Storage | Call audio stored in GCS for QA |
| Slack Alert | Webhook | Real-time notification on escalation |
| SMS Survey | Twilio | Post-call satisfaction (1-5 rating) |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/voice` | Twilio initial webhook |
| POST | `/handle-order` | Process DTMF order number |
| POST | `/handle-speech` | Process speech (full pipeline) |
| GET | `/health` | Health check |
| GET | `/stats` | Session stats + escalation rate |
| GET | `/analytics` | Daily call analytics |
| GET | `/analytics/performance` | 30-day performance summary |
| GET | `/analytics/unanswered` | Top unanswered questions (KB improvement) |
| GET | `/cache/stats` | Response cache metrics |
| POST | `/cache/clear` | Flush response cache |
| POST | `/feedback/process` | Trigger feedback loop (Cloud Scheduler) |
| GET | `/feedback/report` | Weekly feedback report |
| GET | `/feedback/stats` | KB size + auto-added entries |

## Deploy

```bash
# One command
./deploy.sh

# Or manually
gcloud run deploy voice-callbot \
    --source . \
    --region us-central1 \
    --allow-unauthenticated

# Set Twilio webhook: https://voice-callbot-xxx.run.app/voice
```

## Local Development

```bash
pip install -r requirements.txt
export GOOGLE_CLOUD_PROJECT=your-project
gcloud auth application-default login
python app.py
# Twilio: ngrok http 8080
```

## Tests

```bash
pytest tests/ -v
# 23 tests: sentiment, escalation, cache, summary, KB matching
```

## File Structure

```
├── app.py              # Main server (Flask + full pipeline)
├── streaming.py        # Gemini + TTS parallel streaming
├── media_stream.py     # Twilio Media Streams (WebSocket, interrupts)
├── sentiment.py        # Real-time frustration analysis
├── zendesk.py          # Zendesk escalation (6 triggers)
├── feedback_loop.py    # Zendesk → Gemini → KB auto-update
├── dlp_guard.py        # Cloud DLP data protection
├── cache.py            # Response cache (LRU 500)
├── tts_cache.py        # Audio cache (static + dynamic LRU)
├── analytics.py        # Call analytics + unanswered questions
├── post_call.py        # AI summary, recordings, Slack, SMS
├── multilang.py        # Multi-language auto-detection
├── pubsub_events.py    # Async Pub/Sub events
├── tracing.py          # OpenTelemetry → Cloud Trace
├── data.json          # Knowledge base
├── docs/
│   └── ARCHITECTURE.md # Detailed system architecture
├── tests/
│   └── test_callbot.py # 23 tests
├── .github/workflows/
│   └── deploy.yml      # CI/CD (test → build → deploy → smoke)
├── Dockerfile
├── deploy.sh
└── requirements.txt
```

## Cost Estimate (1000 calls/day)

| Service | Monthly Cost |
|---------|-------------|
| Cloud Run | ~$5 |
| Vertex AI Gemini | ~$2 (35% calls hit LLM) |
| Firestore | ~$1 |
| ElevenLabs TTS | ~$22 |
| Twilio | ~$50 |
| BigQuery | ~$0 (free tier) |
| **Total** | **~$80/month** |
