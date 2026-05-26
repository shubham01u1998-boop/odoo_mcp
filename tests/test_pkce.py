"""Tests for OAuth 2.0 Authorization Code + PKCE flow."""
import base64
import hashlib
import secrets
import sys
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth.oauth as oauth_mod
from auth.token_store import TokenStore

VALID_USER = "alice@example.com"
VALID_PASS = "correcthorse"


def _make_verifier():
    verifier = secrets.token_urlsafe(43)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _make_app(store: TokenStore) -> TestClient:
    oauth_mod.token_store = store
    app = Starlette(routes=[
        Route("/.well-known/oauth-protected-resource", oauth_mod.protected_resource_get, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/{path:path}", oauth_mod.protected_resource_get, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", oauth_mod.metadata_get, methods=["GET"]),
        Route("/oauth/authorize", oauth_mod.authorize_get, methods=["GET"]),
        Route("/oauth/authorize", oauth_mod.authorize_post, methods=["POST"]),
        Route("/oauth/token", oauth_mod.token_post, methods=["POST"]),
        Route("/oauth/register", oauth_mod.register_post, methods=["POST"]),
    ])
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def store(tmp_path):
    return TokenStore(str(tmp_path / "tokens.json"))


@pytest.fixture()
def client(store):
    return _make_app(store)


# --- Discovery endpoint ---

def test_metadata_endpoint_returns_required_fields(client):
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    data = r.json()
    assert "authorization_endpoint" in data
    assert "token_endpoint" in data
    assert "S256" in data["code_challenge_methods_supported"]
    assert "code" in data["response_types_supported"]


def test_metadata_endpoints_use_request_base_url(client):
    r = client.get("/.well-known/oauth-authorization-server")
    data = r.json()
    assert data["authorization_endpoint"].endswith("/oauth/authorize")
    assert data["token_endpoint"].endswith("/oauth/token")


def test_protected_resource_endpoint(client):
    r = client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    data = r.json()
    assert "resource" in data
    assert "authorization_servers" in data
    assert len(data["authorization_servers"]) == 1
    # authorization server is the same server as the resource
    assert data["authorization_servers"][0] == data["resource"]


def test_protected_resource_path_based_discovery(client):
    """RFC 9449 path-based discovery: /.well-known/oauth-protected-resource/mcp"""
    r = client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    data = r.json()
    assert "resource" in data
    assert data["resource"].endswith("/mcp")
    assert "authorization_servers" in data
    assert len(data["authorization_servers"]) == 1
    # auth server is the base URL (no /mcp suffix)
    assert not data["authorization_servers"][0].endswith("/mcp")


# --- authorize GET ---

def test_authorize_get_shows_form_with_hidden_fields(client):
    _, challenge = _make_verifier()
    r = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": "claude",
        "redirect_uri": "http://localhost:9999/cb",
        "state": "xyz",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    assert r.status_code == 200
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text
    assert challenge in r.text  # hidden field present


def test_authorize_get_without_params_redirects_to_login(client):
    r = client.get("/oauth/authorize", follow_redirects=False)
    assert r.status_code == 302
    assert "/oauth/login" in r.headers["location"]


# --- Full PKCE flow ---

def test_full_pkce_authorization_code_flow(client, store):
    verifier, challenge = _make_verifier()
    redirect_uri = "http://localhost:9999/cb"

    # Step 1: submit login form
    with patch("auth.oauth.asyncio.to_thread", return_value=42):
        r = client.post("/oauth/authorize", data={
            "username": VALID_USER,
            "password": VALID_PASS,
            "redirect_uri": redirect_uri,
            "state": "mystate",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "client_id": "claude",
        }, follow_redirects=False)

    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith(redirect_uri)
    assert "state=mystate" in location
    code = parse_qs(urlparse(location).query)["code"][0]

    # Step 2: exchange code for token
    r2 = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "client_id": "claude",
    })
    assert r2.status_code == 200
    data = r2.json()
    assert data["token_type"] == "Bearer"
    assert "access_token" in data
    assert data["expires_in"] == 30 * 24 * 3600

    # Token is in store and marked valid
    assert store.is_valid(data["access_token"])
    entry = store.get(data["access_token"])
    assert entry["username"] == VALID_USER


def test_code_is_single_use(client):
    verifier, challenge = _make_verifier()
    with patch("auth.oauth.asyncio.to_thread", return_value=42):
        r = client.post("/oauth/authorize", data={
            "username": VALID_USER, "password": VALID_PASS,
            "redirect_uri": "http://localhost:9999/cb", "state": "s",
            "code_challenge": challenge, "code_challenge_method": "S256", "client_id": "c",
        }, follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]

    payload = {
        "grant_type": "authorization_code", "code": code,
        "code_verifier": verifier, "redirect_uri": "http://localhost:9999/cb", "client_id": "c",
    }
    r1 = client.post("/oauth/token", data=payload)
    assert r1.status_code == 200

    r2 = client.post("/oauth/token", data=payload)  # second use of same code
    assert r2.status_code == 400


# --- Error cases ---

def test_wrong_verifier_returns_400(client):
    verifier, challenge = _make_verifier()
    with patch("auth.oauth.asyncio.to_thread", return_value=42):
        r = client.post("/oauth/authorize", data={
            "username": VALID_USER, "password": VALID_PASS,
            "redirect_uri": "http://localhost:9999/cb", "state": "s",
            "code_challenge": challenge, "code_challenge_method": "S256", "client_id": "c",
        }, follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]

    r2 = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": "wrong_verifier_string",
        "redirect_uri": "http://localhost:9999/cb",
        "client_id": "c",
    })
    assert r2.status_code == 400
    assert "PKCE" in r2.json()["error_description"]


def test_expired_code_returns_400(client):
    verifier, challenge = _make_verifier()
    with patch("auth.oauth.asyncio.to_thread", return_value=42):
        r = client.post("/oauth/authorize", data={
            "username": VALID_USER, "password": VALID_PASS,
            "redirect_uri": "http://localhost:9999/cb", "state": "s",
            "code_challenge": challenge, "code_challenge_method": "S256", "client_id": "c",
        }, follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    oauth_mod._auth_codes[code]["expires_at"] = time.time() - 1

    r2 = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "code_verifier": verifier, "redirect_uri": "http://localhost:9999/cb", "client_id": "c",
    })
    assert r2.status_code == 400
    assert "expired" in r2.json()["error_description"].lower()


def test_invalid_credentials_redirects_back_with_error(client):
    _, challenge = _make_verifier()
    with patch("auth.oauth.asyncio.to_thread", return_value=0):
        r = client.post("/oauth/authorize", data={
            "username": VALID_USER, "password": "wrongpass",
            "redirect_uri": "http://localhost:9999/cb", "state": "s",
            "code_challenge": challenge, "code_challenge_method": "S256", "client_id": "c",
        }, follow_redirects=False)
    assert r.status_code == 303
    assert "error=Invalid" in r.headers["location"]
    assert "code=" not in r.headers["location"]


def test_unsupported_grant_type_returns_400(client):
    r = client.post("/oauth/token", data={"grant_type": "client_credentials"})
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"


def test_unknown_code_returns_400(client):
    r = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": "nonexistent_code",
        "code_verifier": "anything",
    })
    assert r.status_code == 400


# --- TokenStore expiry ---

def test_token_expires_after_specified_seconds(tmp_path):
    store = TokenStore(str(tmp_path / "t.json"))
    t = store.add("u@x.com", "p", expires_in=3600)
    assert store.is_valid(t)
    store._tokens[t]["expires_at"] = "2000-01-01T00:00:00+00:00"
    assert not store.is_valid(t)


def test_old_token_without_expires_at_is_valid(tmp_path):
    store = TokenStore(str(tmp_path / "t.json"))
    store._tokens["legacy"] = {"username": "u", "api_key": "p"}  # no expires_at key
    assert store.is_valid("legacy")


def test_token_store_includes_expires_at_in_new_tokens(tmp_path):
    store = TokenStore(str(tmp_path / "t.json"))
    t = store.add("u@x.com", "p")
    assert "expires_at" in store.get(t)


# --- Dynamic Client Registration (RFC 7591) ---

def test_registration_endpoint_issues_client_id(client):
    r = client.post("/oauth/register", json={
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "client_name": "claude",
    })
    assert r.status_code == 201
    data = r.json()
    assert "client_id" in data
    assert data["redirect_uris"] == ["https://claude.ai/api/mcp/auth_callback"]
    assert data["token_endpoint_auth_method"] == "none"
    assert data["grant_types"] == ["authorization_code"]


def test_metadata_includes_registration_endpoint(client):
    r = client.get("/.well-known/oauth-authorization-server")
    data = r.json()
    assert "registration_endpoint" in data
    assert data["registration_endpoint"].endswith("/oauth/register")
    assert "scopes_supported" in data
    assert "response_modes_supported" in data
    assert "query" in data["response_modes_supported"]


def test_registration_accepts_empty_body(client):
    """Clients that POST with no body should still get a client_id."""
    r = client.post("/oauth/register")
    assert r.status_code == 201
    assert "client_id" in r.json()


def test_each_registration_gets_unique_client_id(client):
    r1 = client.post("/oauth/register", json={"redirect_uris": ["https://a.example/cb"]})
    r2 = client.post("/oauth/register", json={"redirect_uris": ["https://b.example/cb"]})
    assert r1.json()["client_id"] != r2.json()["client_id"]
