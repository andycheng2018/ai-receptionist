"""Tiny in-process cache for LLM receptionist calls.

This is intentionally conservative: the cache key includes the normalized
customer message plus the current lead state and recent transcript. That keeps
repeat/refresh tests fast without reusing an answer in the wrong context.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from typing import Any


# Not for permanent storage, just to speed up development and testing by avoiding repeated LLM calls.
_CACHE: dict[str, tuple[float, tuple[str, dict[str, Any], dict[str, Any]]]] = {}


# Cache is enabled unless specifcally turned off
def cache_enabled() -> bool:
    return os.getenv("AI_RESPONSE_CACHE", "true").strip().lower() not in {"0", "false", "no", "off"}

# Default time to live is 600 seconds, or 10 minutes.
def cache_ttl_seconds() -> int:
    try:
        return int(os.getenv("AI_RESPONSE_CACHE_TTL_SECONDS", "600"))
    except ValueError:
        return 600

# Converts Python data into JSON string so we can sort keys
def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))

# Creates a cache key based on the model, message, lead snapshot, recent transcript, and prompt version.
def make_cache_key(*, model: str, message: str, lead_snapshot: dict[str, Any], recent_transcript: list[dict[str, Any]], prompt_version: str) -> str:
    payload = {
        "prompt_version": prompt_version,
        "model": model,
        "message": " ".join(message.lower().split()),
        "lead": lead_snapshot,
        "recent_transcript": recent_transcript[-6:], # Only include the last 6 messages to keep the cache key size manageable
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest() # Turns payload into a stable JSON string, encodes it to bytes, and then computes the SHA-256 hash to get a fixed-length cache key.

# Read from cache if enabled and not expired. Returns None if not found or expired, otherwise returns the cached reply, patch, and debug info with cache_hit=True and latency_ms=0.
def get(key: str) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    if not cache_enabled():
        return None
    entry = _CACHE.get(key)
    if not entry:
        return None
    created_at, value = entry
    if time.time() - created_at > cache_ttl_seconds():
        _CACHE.pop(key, None)
        return None
    reply, patch, debug = copy.deepcopy(value) # Return a deep copy of the cached value to prevent accidental mutations from affecting the cache.
    debug["cache_hit"] = True # Marke this response as a cache hit in the debug info. (0 ms)
    debug["latency_ms"] = 0
    return reply, patch, debug 

# Saves new result into cache
def set(key: str, value: tuple[str, dict[str, Any], dict[str, Any]]) -> None:
    if not cache_enabled():
        return
    _CACHE[key] = (time.time(), copy.deepcopy(value))

# Returns cache stats including whether it's enabled, number of entries, and TTL in seconds.
def stats() -> dict[str, Any]:
    return {"enabled": cache_enabled(), "entries": len(_CACHE), "ttl_seconds": cache_ttl_seconds()}

# Delete all cache entries
def clear() -> None:
    _CACHE.clear()
