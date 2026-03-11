# Formation Complète — Voice Callbot GCP

## Ce document

Tu as construit ce callbot. Ce doc te permet de répondre à N'IMPORTE QUELLE question en entretien. Chaque section = un concept que L'Oréal peut te demander.

---

## 1. LE PROJET — VUE D'ENSEMBLE

### Pitch 30 secondes

> "J'ai construit un callbot vocal pour un e-commerce — gestion des appels clients, suivi de commande, FAQ automatique. Le projet original tournait sur OpenAI + Flask sur un VPS. Je l'ai migré sur GCP : Vertex AI Gemini remplace OpenAI, Firestore remplace les sessions en mémoire, Cloud DLP scanne les réponses, Pub/Sub gère les events async, et le tout tourne sur Cloud Run en serverless. 10 services GCP intégrés, 23 tests, zéro coût grâce au free tier."

### Ce que ça fait concrètement

1. Un client appelle un numéro de téléphone
2. Twilio décroche et joue un message d'accueil (voix naturelle, ElevenLabs)
3. Le client tape son numéro de commande au clavier
4. Le bot récupère le statut via l'API e-commerce et le dit à voix haute
5. Le client peut ensuite poser des questions en langage naturel
6. Le bot répond en utilisant : cache → base de connaissances → Gemini
7. Si le client est frustré ou demande un humain → ticket Zendesk automatique

### Pourquoi ce projet est pertinent pour L'Oréal

- **Migration vers GCP** : exactement ce que L'Oréal fait avec ses outils internes
- **10 services GCP** : Cloud Run, Vertex AI, Firestore, DLP, Pub/Sub, Logging, Trace, Secret Manager, Artifact Registry, Cloud Build
- **Production-ready** : pas un tuto, un vrai callbot avec escalade, sécurité, monitoring
- **Coût optimisé** : 60-70% des réponses sans appeler le LLM

---

## 2. LA MIGRATION — AVANT / APRÈS

### Pourquoi migrer

L'ancienne version marchait, mais :
- **Pas scalable** : Flask sur un VPS, un process = un appel à la fois
- **Pas résilient** : si le VPS plante, tout tombe
- **Sessions en mémoire** : perdues à chaque redémarrage
- **Pas de monitoring** : aucune visibilité sur ce qui se passe
- **Dépendance OpenAI** : coût variable, pas de contrôle

### Ce qui a changé

| Composant | Avant | Après | Pourquoi |
|-----------|-------|-------|----------|
| LLM | OpenAI GPT-4o-mini | Vertex AI Gemini Flash | Gratuit, natif GCP, même qualité |
| Auth | Clé API en .env | ADC (Application Default Credentials) | Automatique sur Cloud Run, zero secret |
| Sessions | Dict Python en RAM | Firestore | Persistent, queryable, survit aux redémarrages |
| Logs | print() | Cloud Logging JSON structuré | Filtrable, alertable, dashboards |
| Hosting | VPS Flask | Cloud Run serverless | Auto-scale, pay-per-use, zero ops |
| Sécurité | Rien | DLP + SafetySettings | Scan données sensibles avant envoi |
| Events | Rien | Pub/Sub | Async : analytics, CRM, notifications |
| Monitoring | Rien | OpenTelemetry → Cloud Trace | Tracing distribué, latence par étape |
| CI/CD | Deploy manuel SSH | GitHub Actions | Test → Build → Deploy → Smoke test |
| Escalade | Aucune | Zendesk automatique | 6 déclencheurs, ticket avec transcription |

### Si on te demande : "Comment vous abordez une migration ?"

> "Je migre composant par composant, pas tout d'un coup. D'abord le LLM (OpenAI → Gemini), puis le stockage (mémoire → Firestore), puis le hosting (VPS → Cloud Run). À chaque étape, je vérifie que le callbot fonctionne toujours. La logique métier ne change pas — seule l'infrastructure évolue. Ça réduit le risque et permet de rollback à chaque étape."

---

## 3. CLOUD RUN — SERVERLESS

### Comment ça marche pour un callbot

Le callbot reçoit des webhooks de Twilio. Chaque appel = une requête HTTP. Cloud Run est parfait :
- Scale à 0 quand personne n'appelle (coût = $0)
- Scale up si 50 appels simultanés (auto)
- HTTPS + SSL automatique
- Dockerfile = recette du container

### Cold start et callbot

Le cold start Cloud Run (2-5s) serait un problème pour un callbot temps réel. Solutions :
- `--min-instances=1` → au moins 1 instance toujours chaude
- Le welcome message est pré-généré au démarrage
- Le premier appel de la journée a un léger délai, les suivants sont instantanés

### Le Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p static
ENV PORT=8080
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "120"]
```

Gunicorn (pas Flask dev server) avec 2 workers pour gérer les appels concurrents. Timeout 120s parce qu'un appel peut durer longtemps.

### Si on te demande : "Pourquoi Gunicorn et pas Uvicorn ?"

> "Flask est synchrone, donc Gunicorn avec ses workers pre-fork est le bon choix. Si le serveur était en FastAPI (async), j'utiliserais Uvicorn. Ici, chaque worker Gunicorn gère un appel Twilio. Avec 2 workers et le concurrency Cloud Run, on peut gérer facilement 50+ appels simultanés."

---

## 4. VERTEX AI GEMINI — LE LLM

### Comment on l'appelle

```python
import vertexai
from vertexai.generative_models import GenerativeModel

vertexai.init(project="jarvis-v2-488311", location="us-central1")
model = GenerativeModel("gemini-2.0-flash-001")

response = model.generate_content(prompt)
print(response.text)
```

### La différence clé avec OpenAI

Pas de clé API. Sur Cloud Run, les credentials sont automatiques via le service account du projet. C'est ça "Application Default Credentials" (ADC).

- En local : `gcloud auth application-default login`
- Sur Cloud Run : automatique
- Sur GKE : Workload Identity
- Partout : même code, zéro changement

### Le prompt système

```
Tu es l'assistante vocale d'un service client e-commerce.
- Maximum 2 phrases courtes
- Ton naturel, comme au téléphone
- Pas de markdown, pas de listes
- Si tu ne sais pas → dis-le
- JAMAIS d'informations médicales ou juridiques
```

Court, précis, contraignant. Un prompt vocal doit produire du texte "parlable" — pas de bullet points, pas de tableaux, pas de code.

### Le fallback

Si Gemini est down :

```python
try:
    response = model.generate_content(prompt)
except Exception:
    return "Je suis désolée, je ne peux pas répondre pour le moment."
```

Le callbot ne plante jamais. Le client entend toujours quelque chose.

### Si on te demande : "Comment vous gérez la latence du LLM dans un call ?"

> "La latence Gemini Flash est ~500ms. C'est acceptable pour un callbot car le client attend une réponse vocale, pas une réponse texte instantanée. En plus, 60-70% des questions sont résolues par le cache ou la base de connaissances, sans appeler le LLM. Pour les cas où le LLM est lent, on pourrait ajouter un 'message de transition' ('Un instant, je vérifie...') généré en TTS pendant que Gemini réfléchit."

---

## 5. FIRESTORE — SESSIONS PERSISTANTES

### Pourquoi pas Redis ou une DB SQL

- **Redis** : rapide mais pas persistant par défaut, infrastructure à gérer
- **PostgreSQL** : overkill pour des sessions de conversation
- **Firestore** : serverless, persistence garantie, requêtes par champ, free tier généreux

### Structure des documents

```
Collection: callbot_sessions
  Document: {call_sid}
    {
      "order_number": "100456789",
      "history": [
        {"role": "user", "content": "Où est ma commande ?", "timestamp": "..."},
        {"role": "assistant", "content": "En cours de livraison.", "timestamp": "..."}
      ],
      "stt_failures": 0,
      "updated_at": "2026-03-11T15:00:00Z"
    }
```

### Pourquoi c'est mieux que la RAM

1. **Survit aux cold starts** : Cloud Run peut kill une instance à tout moment
2. **Queryable** : "combien d'appels escaladés cette semaine ?"
3. **Analytics** : base pour le module analytics.py
4. **Backup** : Firestore a des exports automatiques

### Si on te demande : "Comment vous gérez la consistance avec Firestore ?"

> "Firestore est strongly consistent depuis 2021 (plus besoin de penser eventual consistency). Chaque écriture est confirmée avant de retourner. Pour les lectures concurrentes (rare dans un callbot — un appel = un document), les transactions Firestore garantissent l'isolation."

---

## 6. CLOUD DLP — PROTECTION DES DONNÉES

### Le problème

Un LLM peut halluciner ou répéter des données sensibles :
- "Votre numéro de sécurité sociale est 1 85 12 75 108 042 25"
- "J'ai trouvé votre email : jean.dupont@gmail.com"
- "Le numéro de carte enregistré est 4532 1234 5678 9012"

Sur un callbot **vocal**, c'est pire — l'info est dite à voix haute, impossible de "supprimer" un son.

### Notre implémentation

```python
from google.cloud import dlp_v2

# Types de données sensibles détectées
INFO_TYPES = [
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "CREDIT_CARD_NUMBER",
    "IBAN_CODE",
    "FRANCE_NIR",           # Numéro de sécu
    "FRANCE_CNI",           # Carte d'identité
    "PERSON_NAME",
    "STREET_ADDRESS",
]

# Scan
response = dlp_client.inspect_content(text)

# Si trouvé → masquer
"Votre numéro est 06 12 34 56 78" → "Votre numéro est [numéro masqué]"
```

### Pourquoi Cloud DLP plutôt qu'un regex

- Les regex ratent les formats variants (06.12.34.56.78, +33 6 12 34 56 78, 06-12-34-56-78)
- DLP détecte les IBAN français, les NIR, les CNI — des formats complexes
- DLP a un score de confiance (LIKELY, VERY_LIKELY) pour éviter les faux positifs

### Si on te demande : "La latence de DLP est-elle un problème ?"

> "DLP prend ~80ms par scan. C'est négligeable par rapport à la latence Gemini (500ms) et TTS (300ms). On scanne une seule fois, après Gemini et avant TTS. Le surcoût total est invisible pour le client."

---

## 7. PUB/SUB — EVENTS ASYNCHRONES

### Le concept

Pendant un appel, on ne veut pas ralentir la conversation pour écrire dans BigQuery ou envoyer un Slack. On publie un event dans Pub/Sub, et des workers séparés traitent en arrière-plan.

### Nos events

```
call.started    → Quand un appel commence
call.turn       → Après chaque échange (avec sentiment + source)
call.escalated  → Quand un appel est escaladé vers Zendesk
call.ended      → Quand l'appel se termine
```

### Les subscribers possibles

```
callbot-events (topic)
  ├── analytics-sub → Worker qui écrit dans BigQuery
  ├── crm-sub → Worker qui met à jour la fiche client
  ├── slack-sub → Worker qui alerte l'équipe sur les escalations
  └── survey-sub → Worker qui envoie un SMS de satisfaction post-appel
```

### Si on te demande : "Pourquoi Pub/Sub et pas juste écrire en DB ?"

> "Trois raisons : 1) Découplage — le callbot ne connaît pas les subscribers. On peut ajouter un worker Slack sans toucher au callbot. 2) Résilience — si BigQuery est down, les messages attendent dans Pub/Sub (7 jours). 3) Performance — zéro latence ajoutée à l'appel, les events sont publiés en fire-and-forget."

---

## 8. SENTIMENT ANALYSIS — DÉTECTION DE FRUSTRATION

### Pourquoi c'est crucial pour un callbot

Un chatbot texte, le client peut fermer la fenêtre. Un callbot vocal, le client est bloqué au téléphone. Détecter la frustration tôt = désamorcer AVANT l'explosion.

### Notre approche : deux couches

**Couche 1 : Règles (instant, $0)**
```python
FRUSTRATION_MARKERS = {
    3: ["je comprends pas", "encore", "toujours pas"],
    5: ["inacceptable", "ras le bol", "scandaleux"],
    7: ["arnaque", "avocat", "porter plainte"],
    9: ["putain", "merde", "bordel"],
}
```
Score 0-10. Résultat en < 1ms.

**Couche 2 : Gemini (nuancé, si ambiguïté)**
Pour le sarcasme, l'ironie, la frustration passive. "Ah c'est formidable..." = positif en mots, négatif en ton.

### Trend detection

On track la frustration tour par tour :
```
Tour 1 : "Où est ma commande ?" → score 0 (neutre)
Tour 2 : "Ça fait 2 semaines" → score 3 (agacé)
Tour 3 : "C'est inacceptable !" → score 5 (frustré, RISING)
→ Escalation proactive AVANT que le client demande
```

### Si on te demande : "Comment vous évitez les faux positifs ?"

> "Deux mécanismes : 1) Le score de confiance — en dessous de 0.6, on ne prend pas de décision. 2) Le trend — un seul mot négatif ne déclenche pas l'escalade. Il faut une tendance à la hausse (2+ messages avec score croissant) pour escalader proactivement. L'escalade immédiate ne se déclenche que sur des mots très explicites (score >= 8)."

---

## 9. ZENDESK ESCALATION — HANDOFF HUMAIN

### Le flow complet

```
1. Détection du besoin d'escalade (6 déclencheurs)
2. Résumé de la conversation (pas de LLM, juste formatage)
3. Création ticket Zendesk via API REST
   - Subject : "[Callbot] Escalade — raison"
   - Body : transcription complète
   - Priority : normal/high/urgent
   - Tags : callbot, escalation, reason_xxx
   - Requester : téléphone du client
4. Message au client : "Votre numéro de dossier est le XXX"
5. Fin d'appel gracieuse
```

### Les 6 déclencheurs

| # | Déclencheur | Priorité | Exemple |
|---|---|---|---|
| 1 | Client demande un humain | High | "Je veux parler à quelqu'un" |
| 2 | Sujet sensible | High | "remboursement", "avocat" |
| 3 | 8+ tours sans résolution | Normal | Conversation qui tourne en rond |
| 4 | 3+ échecs STT | Normal | Qualité audio pourrie |
| 5 | Frustration >= 8 | Urgent | Client en colère |
| 6 | Gemini incertain 2x | Normal | Bot dit "je ne sais pas" deux fois |

### Si on te demande : "Comment vous gérez le handoff si Zendesk est down ?"

> "Le ticket Zendesk est best-effort. Si l'API échoue, le client entend quand même 'Un conseiller vous rappellera'. L'escalation est loggée dans Cloud Logging et publiée dans Pub/Sub — un worker de retry peut re-tenter la création du ticket. Le client n'est jamais impacté par un échec technique interne."

---

## 10. CACHE — OPTIMISATION PERFORMANCE

### Le problème

80% des clients posent les mêmes 20 questions. Appeler Gemini à chaque fois = gaspillage.

### Notre solution : LRU cache

```
- 500 entrées max
- TTL 1 heure (réponses restent fraîches)
- Clé = hash MD5 de la question normalisée (lowercase, sans ponctuation)
- Case-insensitive : "Horaires ?" = "horaires ?" = cache hit
```

### Impact

```
Sans cache : 100% des questions → Gemini (500ms chacune)
Avec cache : ~35% → Gemini, ~30% → KB, ~35% → cache hit (0ms)
```

Latence moyenne divisée par 2. Coût Gemini divisé par 3.

### Si on te demande : "Le cache vit en mémoire, que se passe-t-il au redéploiement ?"

> "Le cache se reconstruit naturellement au fil des appels. Les premières requêtes après un redéploiement sont un peu plus lentes (cache miss), mais en 30 minutes le cache est de nouveau chaud. Pour du cache persistant, on pourrait utiliser Memorystore (Redis managé) — mais pour notre volume, l'in-memory suffit. Avec `--min-instances=1`, Cloud Run garde au moins une instance en vie, donc le cache survit entre les appels."

---

## 11. CI/CD — GITHUB ACTIONS

### Le pipeline

```
Push sur main
  → Job 1 : Tests (23 tests pytest)
  → Job 2 : Deploy (si tests OK)
       → Auth GCP (service account)
       → Deploy Cloud Run (gcloud run deploy --source .)
       → Smoke test (health check + cache stats)
```

### Si on te demande : "Comment vous gérez les secrets dans le pipeline ?"

> "Le service account key est stocké dans GitHub Secrets (GCP_SA_KEY). Les secrets runtime (Twilio, ElevenLabs) sont dans Secret Manager et montés comme variables d'environnement au démarrage du container. Aucune clé en dur dans le code, aucune clé dans les logs."

---

## 12. OPENTELEMETRY — TRACING

### Ce qu'on trace

```
call.incoming (durée totale de l'appel)
  ├── sentiment.analyze (1ms)
  ├── escalation.check (0ms)
  ├── cache.lookup (0ms)
  ├── kb.fuzzy_match (1ms)
  ├── gemini.generate (450ms)   ← le bottleneck
  ├── dlp.scan (80ms)
  ├── cache.store (0ms)
  └── tts.generate (300ms)
```

### En local vs en prod

```python
if ENVIRONMENT == "production":
    # Traces → Cloud Trace (dashboard GCP)
    exporter = CloudTraceSpanExporter()
else:
    # Traces → console (développement)
    exporter = ConsoleSpanExporter()
```

Même code, exporter différent selon l'environnement.

### Si on te demande : "Comment vous identifiez un problème de latence ?"

> "Chaque appel génère un trace ID. Dans Cloud Trace, je vois la cascade de spans avec les durées. Si un appel prend 10 secondes au lieu de 3, je vois immédiatement si c'est Gemini qui est lent, le TTS qui lag, ou le DLP qui timeout. Les attributs sur chaque span (call_sid, question, source) permettent de reproduire le problème."

---

## 13. ANALYTICS — DONNÉES D'UTILISATION

### Les endpoints

```
GET /analytics           → Stats du jour (appels, résolutions, escalades)
GET /analytics/performance → Résumé 30 jours
GET /analytics/unanswered → Questions que le bot ne sait pas répondre
```

### Le endpoint killer : `/analytics/unanswered`

Liste les questions fréquentes qui vont au LLM au lieu de la base de connaissances. Chaque semaine, on prend le top 10 et on les ajoute dans `datas.json`. Résultat : le bot s'améliore continuellement, et le coût Gemini baisse.

### Si on te demande : "Comment vous mesurez la performance du callbot ?"

> "4 métriques clés : 1) Taux de résolution (appels résolus sans escalade / total). 2) Taux d'escalade (< 20% = bon). 3) Nombre moyen de tours par appel (< 4 = bon). 4) Score de frustration moyen (< 3 = bon). Tout ça vient de Firestore via le module analytics.py. En prod, on pousserait ces métriques dans BigQuery via les events Pub/Sub pour des dashboards Looker."

---

## 14. QUESTIONS PIÈGES

### "Pourquoi Flask et pas FastAPI sur ce projet ?"

> "Le projet original était en Flask. Pour la migration GCP, j'ai gardé Flask pour montrer qu'on peut migrer l'infrastructure sans réécrire le code applicatif. C'est la réalité de la plupart des migrations : on ne réécrit pas tout. Mon autre projet (agent-pipeline) utilise FastAPI pour du natif async — je maîtrise les deux."

### "Comment vous gérez 100 appels simultanés ?"

> "Cloud Run scale automatiquement. Chaque instance gère 2 workers Gunicorn (2 appels simultanés). Si 100 appels arrivent, Cloud Run lance 50 instances. Avec le concurrency Cloud Run, on peut monter à 80 requêtes par instance. Le seul bottleneck serait Gemini (15 req/min free tier) — en prod, on prend un quota payant ou on augmente le cache pour réduire les appels LLM."

### "Le callbot parle quelle langue ?"

> "Français actuellement. Mais le changement est trivial : modifier le language Twilio STT (fr-FR → en-US), le prompt Gemini, et la voix ElevenLabs. Le code ne change pas. ElevenLabs multilingual v2 supporte 29 langues avec la même voix."

### "Comment vous ajoutez un nouveau canal (WhatsApp, web chat) ?"

> "La logique métier (sentiment, escalation, KB, Gemini, DLP) est dans des modules séparés. Seul app.py gère Twilio. Pour WhatsApp, j'ajoute un nouveau webhook /whatsapp qui appelle les mêmes modules. Pour le web, un endpoint WebSocket. L'architecture modulaire permet d'ajouter un canal en quelques heures."

### "Comment vous testez un callbot ?"

> "Trois niveaux : 1) Tests unitaires — sentiment, escalation, cache, summary (23 tests, lancés sur chaque push). 2) Tests d'intégration — appeler les endpoints Flask avec des payloads Twilio simulés. 3) Test vocal — appeler le vrai numéro Twilio et parler au bot. Les tests unitaires sont automatisés, le test vocal est manuel (mais on pourrait l'automatiser avec l'API Twilio test calls)."

### "Quel est le coût en production avec 1000 appels/jour ?"

> "Estimation :
> - Cloud Run : ~$5/mois (1000 req/jour, instances courtes)
> - Vertex AI Gemini : ~$2/mois (35% des appels × 3 turns × $0.0002)
> - Firestore : ~$1/mois (30K documents)
> - ElevenLabs : ~$22/mois (pro plan, 100K chars)
> - Twilio : ~$50/mois (1000 appels × $0.05/min × 1 min avg)
> - Total : ~$80/mois pour 1000 appels/jour. Sans le cache, Gemini serait 3x plus cher."

---

## 15. CE QUE TU PEUX DIRE EN ENTRETIEN

### Phrase d'accroche

> "J'ai pris un callbot vocal que j'avais construit pour un e-commerce et je l'ai migré intégralement sur GCP. 10 services Google Cloud intégrés : Cloud Run, Vertex AI, Firestore, DLP, Pub/Sub, Logging, Trace, Secret Manager, Artifact Registry, Cloud Build. Le bot détecte la frustration en temps réel, escalade vers Zendesk automatiquement, et s'améliore en continu grâce aux analytics. 23 tests, CI/CD, architecture documentée. Le code est sur GitHub."

### Montrer le repo

https://github.com/Magda77176/voice-callbot-gcp

### Les chiffres

- 10 services GCP
- 13 fichiers, ~2200 lignes de code
- 23 tests automatisés
- 6 déclencheurs d'escalade
- 60-70% des réponses sans LLM (cache + KB)
- $0 coût en développement (free tier GCP)
- ~$80/mois estimé pour 1000 appels/jour en prod
