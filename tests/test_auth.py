"""
Tests for BearerAuthMiddleware in server.py.
Middleware is tested in isolation using a tiny mock Starlette app.
"""
import sys
import os
import pytest
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

# Add package root so server.py is importable
sys.path.insert(0, str(Path(__file__).parent.parent))


TOKEN_A = "token_for_user_alice"
TOKEN_B = "token_for_user_bob"

TOKEN_MAP = {
    TOKEN_A: {"username": "alice@example.com", "api_key": "key_alice"},
    TOKEN_B: {"username": "bob@example.com", "api_key": "key_bob"},
}


def _make_client(token_map: dict) -> TestClient:
    # Lazy import: delay until test execution so load_dotenv in server.py
    # does not run during collection and overwrite test_mcp.py's env vars.
    from server import BearerAuthMiddleware

    async def _ok(request):
        return JSONResponse({"ok": True})

    mock_app = Starlette(routes=[Route("/sse", _ok), Route("/messages/", _ok, methods=["GET", "POST"])])
    return TestClient(BearerAuthMiddleware(mock_app, token_map), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    return _make_client(TOKEN_MAP)


# --- No auth / wrong token ---

def test_no_auth_returns_401(client):
    r = client.get("/sse")
    assert r.status_code == 401
    assert r.json() == {"error": "Unauthorized"}


def test_wrong_header_token_returns_401(client):
    r = client.get("/sse", headers={"Authorization": "Bearer wrongtoken"})
    assert r.status_code == 401


def test_wrong_query_token_returns_401(client):
    r = client.get("/sse?token=wrongtoken")
    assert r.status_code == 401


# --- Valid tokens ---

def test_user_a_header_token_passes(client):
    r = client.get("/sse", headers={"Authorization": f"Bearer {TOKEN_A}"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_user_b_header_token_passes(client):
    r = client.get("/sse", headers={"Authorization": f"Bearer {TOKEN_B}"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_user_a_query_token_passes(client):
    r = client.get(f"/sse?token={TOKEN_A}")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_user_b_query_token_passes(client):
    r = client.get(f"/sse?token={TOKEN_B}")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# --- /messages/ implicit security ---

def test_messages_endpoint_passes_without_token(client):
    """/messages/ carries a session_id (random UUID) — auth is implicit via the SSE handshake."""
    r = client.post("/messages/?session_id=some-uuid")
    assert r.status_code == 200


# --- Empty token_map: no-auth mode ---

def test_empty_token_map_still_rejects_unauthenticated_mcp():
    """Without a token map or store token, MCP endpoints return 401 (OAuth flow starts)."""
    c = _make_client({})
    r = c.get("/sse")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Bearer")


# --- OAuth-issued token (from token_store) flows through middleware ---

def test_token_store_token_passes_mcp_middleware():
    """Token issued via OAuth and stored in token_store is accepted even with empty token_map."""
    import tempfile
    import auth.token_store as ts_mod
    from auth.token_store import TokenStore

    with tempfile.TemporaryDirectory() as tmpdir:
        fresh_store = TokenStore(f"{tmpdir}/tokens.json")
        token = fresh_store.add("alice@example.com", "password123")

        original = ts_mod.token_store
        ts_mod.token_store = fresh_store
        try:
            c = _make_client({})  # no static tokens — must use token_store
            r = c.get("/sse", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200
        finally:
            ts_mod.token_store = original


def test_expired_token_store_token_is_rejected():
    """Expired OAuth tokens must not bypass the middleware."""
    import tempfile
    import auth.token_store as ts_mod
    from auth.token_store import TokenStore

    with tempfile.TemporaryDirectory() as tmpdir:
        fresh_store = TokenStore(f"{tmpdir}/tokens.json")
        token = fresh_store.add("alice@example.com", "password123")
        fresh_store._tokens[token]["expires_at"] = "2000-01-01T00:00:00+00:00"

        original = ts_mod.token_store
        ts_mod.token_store = fresh_store
        try:
            c = _make_client({})
            r = c.get("/sse", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401
        finally:
            ts_mod.token_store = original


# --- Token from one user does not bleed into another user's token ---

def test_tokens_are_distinct():
    """Token A and Token B are separate — neither is a prefix/substring of the other."""
    assert TOKEN_A not in TOKEN_B
    assert TOKEN_B not in TOKEN_A
    # Both must be valid
    c = _make_client(TOKEN_MAP)
    assert c.get(f"/sse?token={TOKEN_A}").status_code == 200
    assert c.get(f"/sse?token={TOKEN_B}").status_code == 200
    assert c.get("/sse?token=token_for_user").status_code == 401  # partial match rejected
