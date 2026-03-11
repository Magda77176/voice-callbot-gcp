#!/bin/bash
# Deploy voice-callbot to Cloud Run
# Prerequisites: gcloud auth configured, APIs enabled

set -e

PROJECT_ID="jarvis-v2-488311"
REGION="us-central1"
SERVICE_NAME="voice-callbot"

echo "🚀 Deploying $SERVICE_NAME to Cloud Run..."

# Enable required APIs
gcloud services enable \
    aiplatform.googleapis.com \
    firestore.googleapis.com \
    logging.googleapis.com \
    run.googleapis.com \
    --project=$PROJECT_ID --quiet

# Deploy
gcloud run deploy $SERVICE_NAME \
    --source . \
    --region $REGION \
    --project $PROJECT_ID \
    --allow-unauthenticated \
    --memory 512Mi \
    --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$REGION,VERTEX_MODEL=gemini-2.0-flash-001" \
    --set-secrets "TWILIO_ACCOUNT_SID=twilio-sid:latest,TWILIO_AUTH_TOKEN=twilio-token:latest,ELEVEN_LABS_API_KEY=elevenlabs-key:latest"

echo "✅ Deployed! Configure Twilio webhook to point to the Cloud Run URL + /voice"
