"""
Response Cache — Avoid regenerating the same answers.
LRU cache for knowledge base + Gemini responses.
Reduces latency and Vertex AI calls.
"""
import time
import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass

logger = logging.getLogger("callbot")


@dataclass
class CacheEntry:
    response: str
    source: str
    created_at: float
    hit_count: int = 0


class ResponseCache:
    """
    In-memory LRU cache for callbot responses.
    
    - TTL: 1 hour (responses stay fresh)
    - Max size: 500 entries
    - Key: normalized question hash
    
    On Cloud Run, cache lives as long as the instance.
    With min-instances=1, cache stays warm.
    """
    
    def __init__(self, max_size: int = 500, ttl_seconds: int = 3600):
        self.max_size = max_size
        self.ttl = ttl_seconds
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0
    
    def _make_key(self, text: str) -> str:
        """Normalize and hash the question."""
        normalized = text.lower().strip()
        # Remove punctuation and extra spaces
        normalized = " ".join(normalized.split())
        return hashlib.md5(normalized.encode()).hexdigest()
    
    def get(self, question: str) -> CacheEntry | None:
        """Look up a cached response."""
        key = self._make_key(question)
        
        if key in self._cache:
            entry = self._cache[key]
            
            # Check TTL
            if time.time() - entry.created_at > self.ttl:
                del self._cache[key]
                self._misses += 1
                return None
            
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            entry.hit_count += 1
            self._hits += 1
            
            logger.info("cache_hit", extra={
                "json_fields": {
                    "question": question[:50],
                    "source": entry.source,
                    "hit_count": entry.hit_count
                }
            })
            
            return entry
        
        self._misses += 1
        return None
    
    def put(self, question: str, response: str, source: str):
        """Store a response in cache."""
        key = self._make_key(question)
        
        # Evict oldest if full
        if len(self._cache) >= self.max_size:
            self._cache.popitem(last=False)
        
        self._cache[key] = CacheEntry(
            response=response,
            source=source,
            created_at=time.time()
        )
    
    def stats(self) -> dict:
        """Cache performance metrics."""
        total = self._hits + self._misses
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{(self._hits / total * 100):.1f}%" if total > 0 else "N/A",
            "ttl_seconds": self.ttl,
        }
    
    def clear(self):
        """Flush the cache."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0


# Global cache instance
response_cache = ResponseCache()
