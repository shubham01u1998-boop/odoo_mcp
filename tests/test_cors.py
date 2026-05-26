"""
Tests for CORS headers and WWW-Authenticate resource_metadata.

These cover the requirements for claude.ai web OAuth to work:
1. WWW-Authenticate on 401 MUST include resource_metadata (MCP spec §3.1.1)
2. Discovery and OAuth endpoints MUST return CORS headers so the browser can read them
3. CORS preflight (OPTIONS) on the token endpoint must succeed
"""
import sys
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth.oauth as oauth_mod

CLAUDE_ORIGIN = "https://claude.ai"


def _make_client():
    """Build full middleware stack: CORSMiddleware → BearerAuthMiddleware → Starlette app."""
    from server import BearerAuthMiddleware

    async def _ok(request):
        return JSONResponse({"ok": True})

    starlette_app = Starlette(routes=[
        Route("/.well-known/oauth-protected-resource", oauth_mod.protected_resource_get, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/{path:path}", oauth_mod.protected_resource_get, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", oauth_mod.metadata_get, methods=["GET"]),
        Route("/oauth/token", oauth_mod.token_post, methods=["POST"]),
        Route("/oauth/authorize", oauth_mod.authorize_get, methods=["GET"]),
        Route("/oauth/authorize", oauth_mod.authorize_post, methods=["POST"]),
        Route("/oauth/register", oauth_mod.register_post, methods=["POST"]),
        Route("/mcp", _ok, methods=["GET", "POST"]),
        Route("/sse", _ok, methods=["GET"]),
    ])
    bearer_app = BearerAuthMiddleware(starlette_app, {})
    cors_app = CORSMiddleware(
        bearer_app,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["authorization", "content-type"],
        expose_headers=["www-authenticate"],
    )
    return TestClient(cors_app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    return _make_client()


# ---------------------------------------------------------------------------
# WWW-Authenticate must include resource_metadata (MCP spec §3.1.1)
# ---------------------------------------------------------------------------

def test_mcp_401_www_authenticate_includes_resource_metadata(client):
    """MCP spec requires resource_metadata in WWW-Authenticate so clients find the discovery URL."""
    r = client.post("/mcp", headers={"Origin": CLAUDE_ORIGIN})
    assert r.status_code == 401
    www_auth = r.headers.get("www-authenticate", "")
    assert "resource_metadata" in www_auth, (
        f"WWW-Authenticate missing resource_metadata: {www_auth!r}"
    )


def test_sse_401_www_authenticate_includes_resource_metadata(client):
    r = client.get("/sse", headers={"Origin": CLAUDE_ORIGIN})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


def test_resource_metadata_url_points_to_discovery_endpoint(client):
    """resource_metadata value must be the /.well-known/oauth-protected-resource URL."""
    r = client.post("/mcp", headers={"Origin": CLAUDE_ORIGIN})
    www_auth = r.headers.get("www-authenticate", "")
    assert "/.well-known/oauth-protected-resource" in www_auth


# ---------------------------------------------------------------------------
# CORS headers on discovery endpoints
# ---------------------------------------------------------------------------

def test_protected_resource_discovery_has_cors_header(client):
    r = client.get(
        "/.well-known/oauth-protected-resource/mcp",
        headers={"Origin": CLAUDE_ORIGIN},
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers


def test_auth_server_discovery_has_cors_header(client):
    r = client.get(
        "/.well-known/oauth-authorization-server",
        headers={"Origin": CLAUDE_ORIGIN},
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers


# ---------------------------------------------------------------------------
# CORS on 401 responses — browser must be able to READ the 401 body/headers
# ---------------------------------------------------------------------------

def test_mcp_401_has_cors_header(client):
    """Without CORS on the 401, the browser sees status 0 and can't read WWW-Authenticate."""
    r = client.post("/mcp", headers={"Origin": CLAUDE_ORIGIN})
    assert r.status_code == 401
    assert "access-control-allow-origin" in r.headers


def test_sse_401_has_cors_header(client):
    r = client.get("/sse", headers={"Origin": CLAUDE_ORIGIN})
    assert r.status_code == 401
    assert "access-control-allow-origin" in r.headers


# ---------------------------------------------------------------------------
# CORS preflight (OPTIONS) for token endpoint
# ---------------------------------------------------------------------------

def test_token_endpoint_cors_preflight(client):
    """Claude.ai JS does a preflight before POSTing to /oauth/token."""
    r = client.options(
        "/oauth/token",
        headers={
            "Origin": CLAUDE_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers
    assert "access-control-allow-methods" in r.headers


def test_authorize_endpoint_cors_preflight(client):
    r = client.options(
        "/oauth/authorize",
        headers={
            "Origin": CLAUDE_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers


def test_register_endpoint_cors_preflight(client):
    """claude.ai may preflight /oauth/register before POSTing client metadata."""
    r = client.options(
        "/oauth/register",
        headers={
            "Origin": CLAUDE_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers
