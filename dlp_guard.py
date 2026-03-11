"""
DLP Guardrails — Scan responses before sending to callers.
Prevents the LLM from leaking sensitive data (phone numbers, emails, 
credit cards, social security numbers) in voice responses.
Uses Google Cloud DLP API.
"""
import os
import logging
from dataclasses import dataclass
from google.cloud import dlp_v2

logger = logging.getLogger("callbot")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")


@dataclass
class DLPResult:
    is_clean: bool
    findings: list[dict]
    redacted_text: str


# Info types to detect (French-relevant)
INFO_TYPES = [
    {"name": "PHONE_NUMBER"},
    {"name": "EMAIL_ADDRESS"},
    {"name": "CREDIT_CARD_NUMBER"},
    {"name": "IBAN_CODE"},
    {"name": "FRANCE_NIR"},              # Numéro de sécurité sociale
    {"name": "FRANCE_CNI"},              # Carte nationale d'identité
    {"name": "FRANCE_PASSPORT"},
    {"name": "PERSON_NAME"},
    {"name": "STREET_ADDRESS"},
    {"name": "DATE_OF_BIRTH"},
]


def scan_response(text: str) -> DLPResult:
    """
    Scan a response text for sensitive data before sending to caller via TTS.
    
    Why: The LLM might hallucinate or repeat sensitive data from context.
    Example: "Votre numéro est 06 12 34 56 78" — we should NOT say this aloud.
    """
    try:
        dlp_client = dlp_v2.DlpServiceClient()
        
        response = dlp_client.inspect_content(
            request={
                "parent": f"projects/{PROJECT_ID}/locations/global",
                "item": {"value": text},
                "inspect_config": {
                    "info_types": INFO_TYPES,
                    "min_likelihood": dlp_v2.Likelihood.LIKELY,
                    "include_quote": True,
                },
            }
        )
        
        findings = []
        for finding in response.result.findings:
            findings.append({
                "type": finding.info_type.name,
                "quote": finding.quote,
                "likelihood": finding.likelihood.name,
            })
        
        if findings:
            logger.warning("dlp_findings", extra={
                "json_fields": {
                    "text_preview": text[:50],
                    "findings_count": len(findings),
                    "types": [f["type"] for f in findings],
                }
            })
            
            # Redact the sensitive data
            redacted = redact_text(text, findings)
            
            return DLPResult(
                is_clean=False,
                findings=findings,
                redacted_text=redacted
            )
        
        return DLPResult(is_clean=True, findings=[], redacted_text=text)
    
    except Exception as e:
        logger.error(f"DLP scan error: {e}")
        # On DLP failure, allow through but log
        return DLPResult(is_clean=True, findings=[], redacted_text=text)


def redact_text(text: str, findings: list[dict]) -> str:
    """
    Replace sensitive data with safe placeholders.
    "Votre numéro est 06 12 34 56 78" → "Votre numéro est [MASQUÉ]"
    """
    redacted = text
    for finding in sorted(findings, key=lambda f: len(f.get("quote", "")), reverse=True):
        quote = finding.get("quote", "")
        if quote and quote in redacted:
            type_name = finding["type"]
            placeholder_map = {
                "PHONE_NUMBER": "[numéro masqué]",
                "EMAIL_ADDRESS": "[email masqué]",
                "CREDIT_CARD_NUMBER": "[carte masquée]",
                "IBAN_CODE": "[IBAN masqué]",
                "FRANCE_NIR": "[numéro masqué]",
                "PERSON_NAME": "[nom masqué]",
                "STREET_ADDRESS": "[adresse masquée]",
            }
            placeholder = placeholder_map.get(type_name, "[donnée masquée]")
            redacted = redacted.replace(quote, placeholder)
    
    return redacted


def scan_and_sanitize(text: str) -> str:
    """
    Convenience function: scan and return safe text.
    If clean → return as-is.
    If sensitive data found → return redacted version.
    """
    result = scan_response(text)
    return result.redacted_text
