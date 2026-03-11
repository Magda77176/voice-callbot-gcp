# Architecture — Voice Callbot on GCP

## Vue d'ensemble

Callbot vocal de service client déployé sur Google Cloud Platform.
Gère des appels téléphoniques entrants avec IA conversationnelle, détection de sentiment,
escalade automatique vers Zendesk, et analytics temps réel.

## Architecture détaillée

```
                        ┌──────────────┐
                        │  Customer    │
                        │  (Phone)     │
                        └──────┬───────┘
                               │ PSTN
                        ┌──────▼───────┐
                        │   Twilio     │
                        │   Voice API  │
                        │   (STT+DTMF) │
                        └──────┬───────┘
                               │ Webhook HTTPS
                ┌──────────────▼──────────────┐
                │      Cloud Run              │
                │      (Flask + Gunicorn)      │
                │                              │
                │  ┌─────────────────────────┐ │
                │  │     Request Pipeline    │ │
                │  │                         │ │
                │  │  1. Sentiment Analysis  │ │
                │  │  2. Escalation Check    │ │
                │  │  3. Cache Lookup        │ │
                │  │  4. Knowledge Base      │ │
                │  │  5. Intent Detection    │ │
                │  │  6. Vertex AI Gemini    │ │
                │  │  7. DLP Scan            │ │
                │  │  8. Cache Store         │ │
                │  │  9. TTS Generation      │ │
                │  └─────────────────────────┘ │
                └───┬────┬────┬────┬────┬──────┘
                    │    │    │    │    │
         ┌──────────▼┐ ┌▼────▼┐ ┌▼────▼──────┐
         │ Vertex AI  │ │Fire- │ │ Cloud      │
         │ Gemini 2.0 │ │store │ │ Logging    │
         │ Flash      │ │      │ │ (JSON)     │
         └────────────┘ └──────┘ └────────────┘
                    │         │
         ┌──────────▼┐ ┌─────▼────────┐
         │ Cloud DLP  │ │ Pub/Sub      │
         │ (scan)     │ │ (events)     │
         └────────────┘ └──────┬───────┘
                               │
                    ┌──────────┼──────────┐
                    ▼          ▼          ▼
              ┌──────────┐ ┌────────┐ ┌────────┐
              │Analytics │ │  CRM   │ │ Slack  │
              │(BigQuery)│ │ Update │ │ Alert  │
              └──────────┘ └────────┘ └────────┘
                    │
         ┌──────────▼────────┐     ┌──────────────┐
         │   ElevenLabs      │     │   Zendesk    │
         │   (TTS - Claire)  │     │   (Tickets)  │
         └───────────────────┘     └──────────────┘
```

## Services GCP utilisés

| Service | Rôle | Coût |
|---------|------|------|
| **Cloud Run** | Hébergement serverless du callbot | Free tier (2M req/mois) |
| **Vertex AI** | Gemini 2.0 Flash pour réponses IA | Free tier (15 req/min) |
| **Firestore** | Sessions persistantes + historique | Free tier (1 GiB) |
| **Cloud DLP** | Scan des réponses avant TTS | Free tier (1 GiB/mois) |
| **Cloud Pub/Sub** | Events async post-appel | Free tier (10 GiB/mois) |
| **Cloud Logging** | Logs structurés JSON | Free tier (50 GiB/mois) |
| **Cloud Trace** | Tracing distribué (OpenTelemetry) | Free tier |
| **Secret Manager** | Stockage clés API (Twilio, 11L) | Free tier (6 versions) |

## Flux de données détaillé

### Appel entrant (synchrone)

```
1. Twilio POST /voice → Flask reçoit le webhook
2. TTS "Bienvenue" → ElevenLabs génère l'audio → Twilio joue au client
3. Client tape numéro commande (DTMF) → POST /handle-order
4. Lookup commande → API e-commerce → formatage status
5. TTS status → client entend le statut
6. Boucle Q&A → POST /handle-speech :
   a. Twilio STT → texte du client
   b. Sentiment analysis (rule-based, < 1ms)
   c. Escalation check → si oui : Zendesk ticket + fin appel
   d. Cache lookup → si hit : réponse instantanée
   e. KB fuzzy match → si match : réponse sans LLM
   f. Intent detection → delivery/return → API ou réponse type
   g. Vertex AI Gemini → réponse IA (fallback)
   h. DLP scan → masque données sensibles
   i. Cache store → sauvegarde pour prochains appels
   j. Pub/Sub → event "call.turn" (async)
   k. TTS → audio → Twilio → client
```

### Events asynchrones (Pub/Sub)

```
call.started  → Quand un nouvel appel commence
call.turn     → Après chaque échange (avec sentiment)
call.escalated → Quand un appel est escaladé
call.ended    → Quand l'appel se termine

Subscribers possibles :
  - Analytics → BigQuery (stats d'appels)
  - CRM → Mise à jour fiche client
  - Slack → Alerte équipe sur escalation
  - Survey → SMS post-appel satisfaction
```

## Stratégie de réponse (optimisation coût)

```
                    ┌─── Cache hit ──── $0 (instantané)
                    │
User parle ────────┼─── Knowledge Base ── $0 (fuzzy match, < 1ms)
                    │
                    ├─── Intent API ──── $0 (keyword detection)
                    │
                    └─── Vertex AI ──── $0 (free tier Gemini Flash)
                                  │
                                  └── DLP scan ── $0 (free tier)
```

Résultat : **~60-70% des questions répondues sans appeler le LLM** grâce au cache + KB.

## Sécurité

### Protection des données vocales
1. **DLP scan** sur chaque réponse avant TTS — masque numéros, emails, IBAN
2. **SafetySettings** Vertex AI — bloque contenu dangereux au niveau du modèle
3. **Firestore RLS** — sessions isolées par call_sid
4. **Secret Manager** — aucune clé en dur dans le code

### Escalade sécurisée
- Ticket Zendesk = note interne (pas visible du client)
- Transcription complète pour contexte humain
- Caller phone non stocké en clair dans les logs

## Observabilité

### Logs structurés (Cloud Logging)
Chaque interaction produit du JSON structuré :
```json
{
  "severity": "INFO",
  "message": "response_sent",
  "jsonPayload": {
    "call_sid": "CA1234",
    "source": "gemini",
    "sentiment": "frustrated",
    "frustration_score": 5,
    "latency_ms": 450
  }
}
```

### Traces (OpenTelemetry → Cloud Trace)
```
call.incoming
  ├── sentiment.analyze (1ms)
  ├── escalation.check (0ms)
  ├── cache.lookup (0ms — miss)
  ├── kb.fuzzy_match (1ms — no match)
  ├── gemini.generate (450ms)
  ├── dlp.scan (80ms)
  ├── cache.store (0ms)
  └── tts.generate (300ms)
```

### Alertes recommandées
- Taux d'escalade > 20% → problème KB ou modèle
- Latence Gemini > 5s → dégradation performance
- DLP findings > 0 → LLM leak des données sensibles
- Frustration moyenne > 5 → problème service client

## Tests

23 tests automatisés :
- **7 tests sentiment** : positif, neutre, frustré, angry, trend
- **7 tests escalation** : humain, sensible, max turns, STT, confiance
- **3 tests summary** : avec/sans commande, vide
- **6 tests cache** : miss, hit, case-insensitive, éviction, stats, clear

## Runbook

### Déploiement
```bash
./deploy.sh   # ou CI/CD via GitHub Actions
```

### Rollback
```bash
gcloud run services update-traffic voice-callbot \
  --to-revisions=voice-callbot-00001=100 --region=us-central1
```

### Debug un appel
```bash
# Cloud Logging — filtrer par call_sid
resource.type="cloud_run_revision"
jsonPayload.call_sid="CA1234567890"

# Cloud Trace — chercher la trace de l'appel
# Dashboard → Trace list → Filter by service "voice-callbot"
```

### Ajouter une question au KB
1. Consulter `/analytics/unanswered` → questions fréquentes non résolues
2. Ajouter dans `datas.json`
3. Push → CI/CD redéploie → cache se vide → prochains appels utilisent le KB

---

## Feedback Loop — Apprentissage continu

```
Zendesk (tickets résolus tag "callbot")
    │ Cloud Scheduler (daily)
    ▼
Feedback Processor (feedback_loop.py)
    │
    ├─ Gemini classifie : le bot peut-il gérer ça ?
    │    ├── add_to_kb      → enrichit datas.json automatiquement
    │    ├── improve_prompt  → suggestion stockée (Firestore) → revue hebdo
    │    ├── flag_review     → cas complexe → décision humaine
    │    └── no_action       → cas isolé, pas de pattern
    │
    ├─ BigQuery → trend analysis (taux escalade par semaine)
    └─ Rapport hebdo → Slack
```

**Résultat :** Le bot devient plus intelligent chaque semaine. Le taux d'escalade baisse en continu.

**Endpoints :**
- `POST /feedback/process` → déclenché quotidiennement par Cloud Scheduler
- `GET /feedback/report` → résumé hebdomadaire
- `GET /feedback/stats` → taille KB + entrées auto-ajoutées
