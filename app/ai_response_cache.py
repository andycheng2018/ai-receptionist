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

_CACHE: dict[str, tuple[float, tuple[str, dict[str, Any], dict[str, Any]]]] = {}


def cache_enabled() -> bool:
    return os.getenv("AI_RESPONSE_CACHE", "true").strip().lower() not in {"0", "false", "no", "off"}


def cache_ttl_seconds() -> int:
    try:
        return int(os.getenv("AI_RESPONSE_CACHE_TTL_SECONDS", "600"))
    except ValueError:
        return 600


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def make_cache_key(*, model: str, message: str, lead_snapshot: dict[str, Any], recent_transcript: list[dict[str, Any]], prompt_version: str) -> str:
    payload = {
        "prompt_version": prompt_version,
        "model": model,
        "message": " ".join(message.lower().split()),
        "lead": lead_snapshot,
        "recent_transcript": recent_transcript[-6:],
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


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
    reply, patch, debug = copy.deepcopy(value)
    debug["cache_hit"] = True
    debug["latency_ms"] = 0
    return reply, patch, debug


def set(key: str, value: tuple[str, dict[str, Any], dict[str, Any]]) -> None:
    if not cache_enabled():
        return
    _CACHE[key] = (time.time(), copy.deepcopy(value))


def stats() -> dict[str, Any]:
    return {"enabled": cache_enabled(), "entries": len(_CACHE), "ttl_seconds": cache_ttl_seconds()}


def clear() -> None:
    _CACHE.clear()
