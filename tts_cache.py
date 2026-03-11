"""
TTS Audio Cache — Pre-generate and cache common audio responses.
Eliminates TTS latency for frequent phrases (welcome, error, goodbye).

Strategy:
1. Static phrases → pre-generated at startup (0ms at runtime)
2. Frequent responses → LRU cache after first generation
3. Unique responses → generate on-the-fly
"""
import os
import hashlib
import logging
import time
from collections import OrderedDict
from pathlib import Path

from elevenlabs import save
from elevenlabs.client import ElevenLabs

logger = logging.getLogger("callbot")

ELEVEN_LABS_API_KEY = os.getenv("ELEVEN_LABS_API_KEY")
VOICE_ID = os.getenv("ELEVENLABS_VOICE", "Claire")
CACHE_DIR = os.path.join("static", "tts_cache")


# --- Static phrases (pre-generated at boot) ---

STATIC_PHRASES = {
    "welcome": "Bonjour, bienvenue. Veuillez entrer votre numéro de commande suivi de la touche dièse.",
    "error_stt": "Je n'ai pas compris. Pouvez-vous répéter s'il vous plaît ?",
    "error_order": "Le numéro de commande n'est pas valide. Veuillez réessayer.",
    "followup": "Avez-vous une autre question ?",
    "goodbye": "Merci de votre appel. Au revoir et bonne journée.",
    "escalation": "Je transfère votre demande à un conseiller qui vous rappellera très rapidement.",
    "hold": "Un instant, je vérifie pour vous.",
    "order_not_found": "Je n'ai pas trouvé cette commande dans notre système.",
    "processing": "En cours de préparation.",
    "shipped": "Votre commande a été expédiée.",
    "delivered": "Votre commande a été livrée.",
}


class TTSCache:
    """
    Two-tier TTS cache:
    - Tier 1: Static files (pre-generated, instant)
    - Tier 2: LRU cache (generated on first request, cached for reuse)
    """
    
    def __init__(self, max_dynamic_entries: int = 200):
        self.static_files: dict[str, str] = {}     # phrase_key → filename
        self.dynamic_cache: OrderedDict[str, str] = OrderedDict()  # text_hash → filename
        self.max_dynamic = max_dynamic_entries
        self.client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY) if ELEVEN_LABS_API_KEY else None
        
        # Metrics
        self.hits_static = 0
        self.hits_dynamic = 0
        self.misses = 0
        
        # Ensure cache directory exists
        os.makedirs(CACHE_DIR, exist_ok=True)
    
    def _text_hash(self, text: str) -> str:
        """Hash text for cache key."""
        return hashlib.md5(text.lower().strip().encode()).hexdigest()[:12]
    
    def _generate(self, text: str, filename: str) -> str | None:
        """Generate audio and save to cache directory."""
        if not self.client:
            return None
        
        try:
            filepath = os.path.join(CACHE_DIR, filename)
            
            # Skip if already exists on disk
            if os.path.exists(filepath):
                return filepath
            
            audio = self.client.generate(
                text=text,
                voice=VOICE_ID,
                model="eleven_multilingual_v2",
            )
            if audio:
                save(audio, filepath)
                return filepath
        except Exception as e:
            logger.error(f"TTS cache generation error: {e}")
        return None
    
    def warmup(self):
        """
        Pre-generate all static phrases at startup.
        Called once when the server boots — no latency during calls.
        """
        logger.info("tts_cache_warmup_start", extra={
            "json_fields": {"phrases": len(STATIC_PHRASES)}
        })
        
        t0 = time.time()
        generated = 0
        
        for key, text in STATIC_PHRASES.items():
            filename = f"static_{key}.mp3"
            filepath = self._generate(text, filename)
            if filepath:
                self.static_files[key] = filename
                generated += 1
        
        warmup_ms = int((time.time() - t0) * 1000)
        
        logger.info("tts_cache_warmup_done", extra={
            "json_fields": {
                "generated": generated,
                "total": len(STATIC_PHRASES),
                "warmup_ms": warmup_ms,
            }
        })
    
    def get_static(self, key: str) -> str | None:
        """Get a pre-generated static audio file. Returns URL path."""
        if key in self.static_files:
            self.hits_static += 1
            return f"/static/tts_cache/{self.static_files[key]}"
        return None
    
    def get_or_generate(self, text: str) -> str | None:
        """
        Get cached audio or generate new.
        
        1. Check dynamic cache → hit = instant
        2. Miss → generate → cache → return
        """
        text_hash = self._text_hash(text)
        
        # Check dynamic cache
        if text_hash in self.dynamic_cache:
            self.dynamic_cache.move_to_end(text_hash)
            self.hits_dynamic += 1
            filename = self.dynamic_cache[text_hash]
            return f"/static/tts_cache/{filename}"
        
        # Cache miss → generate
        self.misses += 1
        filename = f"dyn_{text_hash}.mp3"
        filepath = self._generate(text, filename)
        
        if filepath:
            # Store in dynamic cache
            if len(self.dynamic_cache) >= self.max_dynamic:
                # Evict oldest
                old_key, old_file = self.dynamic_cache.popitem(last=False)
                old_path = os.path.join(CACHE_DIR, old_file)
                if os.path.exists(old_path):
                    os.remove(old_path)
            
            self.dynamic_cache[text_hash] = filename
            return f"/static/tts_cache/{filename}"
        
        return None
    
    def stats(self) -> dict:
        """Cache performance metrics."""
        total = self.hits_static + self.hits_dynamic + self.misses
        return {
            "static_phrases": len(self.static_files),
            "dynamic_cached": len(self.dynamic_cache),
            "hits_static": self.hits_static,
            "hits_dynamic": self.hits_dynamic,
            "misses": self.misses,
            "hit_rate": f"{((self.hits_static + self.hits_dynamic) / total * 100):.1f}%" if total > 0 else "N/A",
        }


# Global instance
tts_cache = TTSCache()
