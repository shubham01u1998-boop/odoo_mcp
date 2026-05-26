"""
End-to-end test that simulates the full claude.ai → MCP OAuth connection flow:

  1. POST /mcp (no token)          → 401 + WWW-Authenticate with resource_metadata
  2. GET  /.well-known/oauth-protected-resource/mcp → discovers auth server
  3. GET  /.well-known/oauth-authorization-server  → discovers endpoints
  4. POST /oauth/register           → client_id issued (RFC 7591)
  5. GET  /oauth/authorize?...      → login form rendered
  6. POST /oauth/authorize (creds)  → auth code issued, redirect
  7. POST /oauth/token              → access token issued (PKCE verified)
  8. POST /mcp (Bearer token)       → 200, request forwarded to MCP app
"""
import base64
import hashlib
import secrets
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth.oauth as oauth_mod
import auth.token_store as ts_mod
from auth.token_store import TokenStore


def _make_verifier():
    verifier = secrets.token_urlsafe(43)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@pytest.fixture()
def e2e_client(tmp_path):
    """Full middleware stack with a fresh token store and a mock /mcp backend."""
    from server import BearerAuthMiddleware

    store = TokenStore(str(tmp_path / "tokens.json"))
    oauth_mod.token_store = store

    original_store = ts_mod.token_store
    ts_mod.token_store = store

    async def _mcp_ok(request):
        return JSONResponse({"ok": True, "mcp": "connected"})

    starlette_app = Starlette(routes=[
        Route("/.well-known/oauth-protected-resource", oauth_mod.protected_resource_get, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/{path:path}", oauth_mod.protected_resource_get, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", oauth_mod.metadata_get, methods=["GET"]),
        Route("/oauth/register", oauth_mod.register_post, methods=["POST"]),
        Route("/oauth/authorize", oauth_mod.authorize_get, methods=["GET"]),
        Route("/oauth/authorize", oauth_mod.authorize_post, methods=["POST"]),
        Route("/oauth/token", oauth_mod.token_post, methods=["POST"]),
        Route("/mcp", _mcp_ok, methods=["GET", "POST"]),
        Route("/sse", _mcp_ok, methods=["GET"]),
    ])

    app = CORSMiddleware(
        BearerAuthMiddleware(starlette_app, {}),   # empty static token_map
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["authorization", "content-type"],
        expose_headers=["www-authenticate"],
    )
    client = TestClient(app, raise_server_exceptions=False)

    yield client, store

    ts_mod.token_store = original_store


def test_full_claude_ai_oauth_connection_flow(e2e_client):
    client, store = e2e_client
    ORIGIN = "https://claude.ai"
    REDIRECT = "https://claude.ai/api/mcp/auth_callback"

    # ── Step 1: POST /mcp without token → 401 with resource_metadata ──────────
    r1 = client.post("/mcp", headers={"Origin": ORIGIN, "Content-Type": "application/json"})
    assert r1.status_code == 401
    www_auth = r1.headers.get("www-authenticate", "")
    assert "resource_metadata" in www_auth
    assert "access-control-allow-origin" in r1.headers   # CORS on 401

    # ── Step 2: Path-based protected-resource discovery ────────────────────────
    r2 = client.get("/.well-known/oauth-protected-resource/mcp", headers={"Origin": ORIGIN})
    assert r2.status_code == 200
    meta = r2.json()
    assert meta["resource"].endswith("/mcp")
    auth_server = meta["authorization_servers"][0]

    # ── Step 3: Auth server metadata ──────────────────────────────────────────
    r3 = client.get("/.well-known/oauth-authorization-server", headers={"Origin": ORIGIN})
    assert r3.status_code == 200
    as_meta = r3.json()
    assert "registration_endpoint" in as_meta
    assert "S256" in as_meta["code_challenge_methods_supported"]

    # ── Step 4: Dynamic client registration ───────────────────────────────────
    r4 = client.post("/oauth/register", json={
        "redirect_uris": [REDIRECT],
        "client_name": "claude",
    }, headers={"Origin": ORIGIN})
    assert r4.status_code == 201
    client_id = r4.json()["client_id"]
    assert client_id

    # ── Step 5: GET /oauth/authorize → login form ─────────────────────────────
    verifier, challenge = _make_verifier()
    r5 = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "state": "test-state-xyz",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    assert r5.status_code == 200
    assert 'name="username"' in r5.text
    assert challenge in r5.text   # hidden PKCE field rendered

    # ── Step 6: POST /oauth/authorize with Odoo creds → auth code ─────────────
    with patch("auth.oauth.asyncio.to_thread", return_value=42):   # uid=42 = valid
        r6 = client.post("/oauth/authorize", data={
            "username": "alice@example.com",
            "password": "correcthorse",
            "redirect_uri": REDIRECT,
            "state": "test-state-xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "client_id": client_id,
        }, follow_redirects=False)
    assert r6.status_code == 302
    location = r6.headers["location"]
    assert location.startswith(REDIRECT)
    assert "state=test-state-xyz" in location
    code = parse_qs(urlparse(location).query)["code"][0]

    # ── Step 7: POST /oauth/token → access token ──────────────────────────────
    r7 = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT,
        "client_id": client_id,
    })
    assert r7.status_code == 200
    token_data = r7.json()
    assert token_data["token_type"] == "Bearer"
    access_token = token_data["access_token"]
    assert store.is_valid(access_token)

    # ── Step 8: POST /mcp with Bearer token → 200 (connected!) ───────────────
    r8 = client.post("/mcp", headers={
        "Authorization": f"Bearer {access_token}",
        "Origin": ORIGIN,
        "Content-Type": "application/json",
    })
    assert r8.status_code == 200, f"Expected 200 after OAuth, got {r8.status_code}: {r8.text}"
    assert r8.json() == {"ok": True, "mcp": "connected"}
