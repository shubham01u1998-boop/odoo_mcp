import json
import os
import secrets
from datetime import datetime, timezone, timedelta
from threading import Lock

THIRTY_DAYS = 30 * 24 * 3600


class TokenStore:
    def __init__(self, path: str):
        self._path = path
        self._lock = Lock()
        self._tokens: dict[str, dict] = self._load()

    def _load(self) -> dict:
        if os.path.exists(self._path):
            with open(self._path) as f:
                return json.load(f)
        return {}

    def _save(self):
        with open(self._path, "w") as f:
            json.dump(self._tokens, f, indent=2)

    def add(self, username: str, credential: str, expires_in: int = THIRTY_DAYS) -> str:
        token = secrets.token_hex(32)
        now = datetime.now(timezone.utc)
        with self._lock:
            self._tokens[token] = {
                "username": username,
                "api_key": credential,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
            }
            self._save()
        return token

    def get(self, token: str) -> dict | None:
        return self._tokens.get(token)

    def is_valid(self, token: str) -> bool:
        entry = self._tokens.get(token)
        if not entry:
            return False
        expires_at = entry.get("expires_at")
        if expires_at is None:
            return True  # backward compat: old tokens without expiry never expire
        return datetime.now(timezone.utc) < datetime.fromisoformat(expires_at)

    def all_tokens(self) -> dict[str, dict]:
        return dict(self._tokens)

    def revoke(self, token: str) -> bool:
        with self._lock:
            if token in self._tokens:
                del self._tokens[token]
                self._save()
                return True
        return False


# Singleton — path is odoo-mcp/tokens.json
token_store = TokenStore(os.path.join(os.path.dirname(__file__), "..", "tokens.json"))
