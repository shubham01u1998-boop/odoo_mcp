"""
PURPOSE: Thread-safe in-memory TTL cache shared across all tools.
EXPORTS: cache (singleton CacheLayer), TTL_TICKET, TTL_LIST, TTL_META, TTL_USERS
DEPENDS ON: stdlib only (threading, time)
PATTERNS: cache.get(key) → cache.set(key, val, TTL_X) | cache.invalidate_prefix("ticket:")
DO NOT USE FOR: persistent storage — cache is in-memory and resets on server restart.
"""
import threading
import time
from typing import Any

TTL_TICKET = 60
TTL_LIST = 60
TTL_META = 600
TTL_USERS = 300


class CacheLayer:
    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() < expires_at:
                return value
            del self._store[key]
            return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            self._store[key] = (value, time.time() + ttl)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        with self._lock:
            keys = [k for k in self._store if k.startswith(prefix)]
            for k in keys:
                del self._store[k]


cache = CacheLayer()
