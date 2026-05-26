"""Tests for OAuth login portal (auth/oauth.py and auth/token_store.py)."""
import sys
import os
import pytest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth.oauth as oauth_mod
from auth.token_store import TokenStore

VALID_USER = "alice@example.com"
VALID_PASS = "correcthorse"


def _make_client(store: TokenStore) -> TestClient:
    oauth_mod.token_store = store
    app = Starlette(routes=[
        Route("/oauth/login", oauth_mod.login_get, methods=["GET"]),
        Route("/oauth/login", oauth_mod.login_post, methods=["POST"]),
        Route("/oauth/success", oauth_mod.success_page, methods=["GET"]),
        Route("/oauth/revoke", oauth_mod.revoke_post, methods=["POST"]),
    ])
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def store(tmp_path):
    return TokenStore(str(tmp_path / "tokens.json"))


@pytest.fixture()
def client(store):
    return _make_client(store)


# --- Login page ---

def test_login_page_loads(client):
    r = client.get("/oauth/login")
    assert r.status_code == 200
    assert "Sign in with your Odoo account" in r.text
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text


def test_login_page_shows_error_from_query(client):
    r = client.get("/oauth/login?error=Something+went+wrong")
    assert r.status_code == 200
    assert "Something went wrong" in r.text


# --- POST login: validation ---

def test_empty_form_redirects_with_error(client):
    r = client.post("/oauth/login", data={"username": "", "password": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_invalid_creds_redirects_with_error(client):
    with patch("auth.oauth.asyncio.to_thread", return_value=0):
        r = client.post(
            "/oauth/login",
            data={"username": VALID_USER, "password": "wrong"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "error=Invalid" in r.headers["location"]


def test_odoo_exception_treated_as_invalid(client):
    with patch("auth.oauth.asyncio.to_thread", side_effect=Exception("timeout")):
        r = client.post(
            "/oauth/login",
            data={"username": VALID_USER, "password": "pass"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "error=Invalid" in r.headers["location"]


# --- POST login: success ---

def test_valid_creds_issues_token_and_redirects(client, store):
    with patch("auth.oauth.asyncio.to_thread", return_value=42):
        r = client.post(
            "/oauth/login",
            data={"username": VALID_USER, "password": VALID_PASS},
            follow_redirects=False,
        )
    assert r.status_code == 303
    location = r.headers["location"]
    assert "/oauth/success?token=" in location
    token = location.split("token=")[1]
    creds = store.get(token)
    assert creds is not None
    assert creds["username"] == VALID_USER
    assert creds["api_key"] == VALID_PASS


# --- Success page ---

def test_success_page_shows_token(client):
    r = client.get("/oauth/success?token=abc123")
    assert r.status_code == 200
    assert "abc123" in r.text
    assert "sse?token=abc123" in r.text


def test_success_page_shows_claude_desktop_snippet(client):
    r = client.get("/oauth/success?token=mytoken")
    assert "Authorization" in r.text
    assert "Bearer mytoken" in r.text


# --- Revoke ---

def test_revoke_removes_token_and_redirects(client, store):
    token = store.add(VALID_USER, VALID_PASS)
    assert store.get(token) is not None
    r = client.post("/oauth/revoke", data={"token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert store.get(token) is None
    assert "revoked" in r.headers["location"].lower()


def test_revoke_unknown_token_still_redirects(client):
    r = client.post("/oauth/revoke", data={"token": "nonexistent"}, follow_redirects=False)
    assert r.status_code == 303


# --- TokenStore ---

def test_token_store_persists_to_disk(tmp_path):
    path = str(tmp_path / "tokens.json")
    s = TokenStore(path)
    t = s.add("user@x.com", "pass")
    assert os.path.exists(path)
    s2 = TokenStore(path)
    assert s2.get(t) is not None
    assert s2.get(t)["username"] == "user@x.com"


def test_token_store_revoke(tmp_path):
    s = TokenStore(str(tmp_path / "tokens.json"))
    t = s.add("u@x.com", "p")
    assert s.revoke(t) is True
    assert s.get(t) is None
    assert s.revoke(t) is False  # already gone


def test_token_store_all_tokens(tmp_path):
    s = TokenStore(str(tmp_path / "tokens.json"))
    t1 = s.add("a@x.com", "p1")
    t2 = s.add("b@x.com", "p2")
    all_t = s.all_tokens()
    assert t1 in all_t
    assert t2 in all_t
