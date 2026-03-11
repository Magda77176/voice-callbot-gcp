"""
Multi-Language Support — Auto-detect and switch language mid-call.

Detection: first user sentence → detect language → switch everything:
- Twilio STT language
- Gemini system prompt language
- ElevenLabs voice + language
- Knowledge base (language-specific)
"""
import os
import re
import logging

logger = logging.getLogger("callbot")

# Supported languages with their configs
LANGUAGE_CONFIGS = {
    "fr": {
        "name": "Français",
        "twilio_lang": "fr-FR",
        "elevenlabs_voice": "Claire",
        "welcome": "Bonjour, bienvenue. Veuillez entrer votre numéro de commande suivi de la touche dièse.",
        "error_stt": "Je n'ai pas compris. Pouvez-vous répéter ?",
        "goodbye": "Merci de votre appel. Au revoir.",
        "system_prompt_suffix": "Réponds en français, ton naturel et professionnel.",
    },
    "en": {
        "name": "English",
        "twilio_lang": "en-US",
        "elevenlabs_voice": "Rachel",
        "welcome": "Hello, welcome. Please enter your order number followed by the hash key.",
        "error_stt": "I didn't catch that. Could you repeat please?",
        "goodbye": "Thank you for calling. Goodbye.",
        "system_prompt_suffix": "Reply in English, natural and professional tone.",
    },
    "es": {
        "name": "Español",
        "twilio_lang": "es-ES",
        "elevenlabs_voice": "Lucia",
        "welcome": "Hola, bienvenido. Por favor ingrese su número de pedido seguido de la tecla almohadilla.",
        "error_stt": "No he entendido. ¿Puede repetir por favor?",
        "goodbye": "Gracias por llamar. Adiós.",
        "system_prompt_suffix": "Responde en español, tono natural y profesional.",
    },
    "de": {
        "name": "Deutsch",
        "twilio_lang": "de-DE",
        "elevenlabs_voice": "Lena",
        "welcome": "Hallo, willkommen. Bitte geben Sie Ihre Bestellnummer gefolgt von der Raute-Taste ein.",
        "error_stt": "Ich habe das nicht verstanden. Können Sie das bitte wiederholen?",
        "goodbye": "Vielen Dank für Ihren Anruf. Auf Wiederhören.",
        "system_prompt_suffix": "Antworte auf Deutsch, natürlich und professionell.",
    },
}

# Common words for language detection
LANGUAGE_INDICATORS = {
    "fr": ["bonjour", "merci", "commande", "livraison", "quand", "comment", "où", "est", "mon", "ma", "je", "veux", "besoin"],
    "en": ["hello", "hi", "order", "delivery", "when", "how", "where", "my", "want", "need", "please", "thank"],
    "es": ["hola", "gracias", "pedido", "entrega", "cuando", "cómo", "donde", "mi", "quiero", "necesito", "por favor"],
    "de": ["hallo", "danke", "bestellung", "lieferung", "wann", "wie", "wo", "mein", "brauche", "möchte", "bitte"],
}


def detect_language(text: str) -> str:
    """
    Detect language from user text using keyword matching.
    Returns language code (fr, en, es, de).
    Default: fr (French).
    """
    normalized = text.lower()
    scores = {}
    
    for lang, keywords in LANGUAGE_INDICATORS.items():
        score = sum(1 for kw in keywords if kw in normalized)
        scores[lang] = score
    
    if not scores or max(scores.values()) == 0:
        return "fr"  # Default French
    
    detected = max(scores, key=scores.get)
    confidence = scores[detected] / max(len(normalized.split()), 1)
    
    logger.info("language_detected", extra={
        "json_fields": {
            "text": text[:50],
            "detected": detected,
            "scores": scores,
        }
    })
    
    return detected


def get_language_config(lang_code: str) -> dict:
    """Get the full config for a language."""
    return LANGUAGE_CONFIGS.get(lang_code, LANGUAGE_CONFIGS["fr"])
