import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
import xmlrpc.client
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth.token_store import token_store

# ---------------------------------------------------------------------------
# In-memory auth code store — codes expire in 5 minutes, never persisted
# ---------------------------------------------------------------------------
_auth_codes: dict[str, dict] = {}
_AUTH_CODE_TTL = 300  # seconds

# ---------------------------------------------------------------------------
# In-memory OAuth client registry (RFC 7591) — ephemeral, not persisted
# ---------------------------------------------------------------------------
_clients: dict[str, dict] = {}


def _url() -> str:
    return os.environ["ODOO_URL"].rstrip("/")


def _db() -> str:
    return os.environ["ODOO_DB"]


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------

_AUTHORIZE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Odoo MCP &mdash; Sign In</title>
  <style>
    *{{box-sizing:border-box}}
    body{{font-family:sans-serif;background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
    .card{{background:#fff;padding:2rem;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.1);width:360px}}
    h2{{color:#714B67;margin:0 0 .5rem}}
    p{{color:#555;font-size:.9rem;margin:0 0 1.5rem}}
    label{{display:block;font-size:.85rem;color:#333;margin-bottom:.25rem}}
    input[type=email],input[type=password]{{width:100%;padding:.65rem;border:1px solid #ccc;border-radius:4px;margin-bottom:1rem;font-size:1rem}}
    button{{width:100%;background:#714B67;color:#fff;padding:.75rem;border:none;border-radius:4px;font-size:1rem;cursor:pointer}}
    button:hover{{background:#5a3a54}}
    .error{{color:#c0392b;font-size:.9rem;margin-bottom:1rem}}
  </style>
</head>
<body>
  <div class="card">
    <h2>&#128272; Sign in to Odoo MCP</h2>
    <p>Enter your Odoo credentials to connect Claude.</p>
    {error}
    <form method="post" action="/oauth/authorize">
      {hidden}
      <label>Odoo Email</label>
      <input type="email" name="username" placeholder="you@company.com" required autofocus>
      <label>Password</label>
      <input type="password" name="password" required>
      <button type="submit">Sign in &#8594;</button>
    </form>
  </div>
</body>
</html>"""

_LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Odoo MCP Login</title>
  <style>
    *{{box-sizing:border-box}}
    body{{font-family:sans-serif;background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
    .card{{background:#fff;padding:2rem;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.1);width:360px}}
    h2{{color:#714B67;margin:0 0 .5rem}}
    p{{color:#555;font-size:.9rem;margin:0 0 1.5rem}}
    label{{display:block;font-size:.85rem;color:#333;margin-bottom:.25rem}}
    input{{width:100%;padding:.65rem;border:1px solid #ccc;border-radius:4px;margin-bottom:1rem;font-size:1rem}}
    button{{width:100%;background:#714B67;color:#fff;padding:.75rem;border:none;border-radius:4px;font-size:1rem;cursor:pointer}}
    button:hover{{background:#5a3a54}}
    .error{{color:#c0392b;font-size:.9rem;margin-bottom:1rem}}
  </style>
</head>
<body>
  <div class="card">
    <h2>&#128272; Odoo MCP Login</h2>
    <p>Sign in with your Odoo account. You'll receive a bearer token to connect Claude.</p>
    {error}
    <form method="post" action="/oauth/login">
      <label>Odoo Email</label>
      <input type="email" name="username" placeholder="you@company.com" required autofocus>
      <label>Password</label>
      <input type="password" name="password" required>
      <button type="submit">Sign in with Odoo &#8594;</button>
    </form>
  </div>
</body>
</html>"""

_SUCCESS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Your MCP Token</title>
  <style>
    *{{box-sizing:border-box}}
    body{{font-family:sans-serif;background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
    .card{{background:#fff;padding:2rem;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.1);width:540px}}
    h2{{color:#714B67;margin:0 0 .5rem}}
    h3{{color:#333;margin:1.25rem 0 .25rem}}
    p{{color:#555;font-size:.9rem;margin:0 0 .5rem}}
    .token-box{{background:#f0e8ee;border:1px solid #714B67;border-radius:4px;padding:.75rem;font-family:monospace;font-size:.85rem;word-break:break-all;margin:.5rem 0}}
    button{{background:#714B67;color:#fff;padding:.5rem 1rem;border:none;border-radius:4px;cursor:pointer;font-size:.9rem}}
    button:hover{{background:#5a3a54}}
    pre{{background:#1e1e1e;color:#d4d4d4;padding:1rem;border-radius:4px;font-size:.78rem;overflow-x:auto;margin:.5rem 0 0;white-space:pre-wrap;word-break:break-all}}
    .warn{{color:#c0392b;font-size:.82rem;margin-top:1rem;padding:.5rem;border:1px solid #c0392b;border-radius:4px;background:#fdf2f2}}
    .revoke-btn{{background:#c0392b}}
    .revoke-btn:hover{{background:#a93226}}
  </style>
</head>
<body>
  <div class="card">
    <h2>&#10003; Token issued</h2>
    <p>Copy your bearer token and add it to Claude Desktop or use it as a query parameter:</p>
    <div class="token-box" id="tok">{token}</div>
    <button onclick="navigator.clipboard.writeText(document.getElementById('tok').innerText).then(()=>this.textContent='Copied!')">&#128203; Copy token</button>

    <h3>claude.ai web</h3>
    <p>Paste this URL into the MCP server field:</p>
    <pre>{base_url}sse?token={token}</pre>

    <h3>Claude Desktop &mdash; claude_desktop_config.json</h3>
    <pre>{{
  "mcpServers": {{
    "odoo": {{
      "url": "{base_url}sse",
      "headers": {{"Authorization": "Bearer {token}"}}
    }}
  }}
}}</pre>

    <div class="warn">&#9888; Keep this token secret &mdash; it gives full MCP access as your Odoo user. Do not share it.</div>

    <h3>Revoke token</h3>
    <form method="post" action="/oauth/revoke">
      <input type="hidden" name="token" value="{token}">
      <button class="revoke-btn" onclick="return confirm('Revoke this token? You will need to log in again.')">Revoke token</button>
    </form>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# OAuth 2.0 Protected Resource Metadata (RFC 9449 / MCP spec requirement)
# ---------------------------------------------------------------------------

async def protected_resource_get(request: Request) -> JSONResponse:
    base = str(request.base_url).rstrip("/")
    # RFC 9449 path-based discovery: /.well-known/oauth-protected-resource/{resource-path}
    # e.g. GET /.well-known/oauth-protected-resource/mcp → resource = base/mcp
    path_suffix = request.path_params.get("path", "")
    resource = f"{base}/{path_suffix}" if path_suffix else base
    return JSONResponse({
        "resource": resource,
        "authorization_servers": [base],
    })


# ---------------------------------------------------------------------------
# OAuth 2.0 Authorization Server discovery (RFC 8414)
# ---------------------------------------------------------------------------

async def metadata_get(request: Request) -> JSONResponse:
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [],
        "response_modes_supported": ["query"],
    })


# ---------------------------------------------------------------------------
# RFC 7591 Dynamic Client Registration
# ---------------------------------------------------------------------------

async def register_post(request: Request) -> JSONResponse:
    """Issues a client_id to any requester — no pre-registration required."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    client_id = secrets.token_urlsafe(16)
    entry = {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": body.get("redirect_uris", []),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }
    _clients[client_id] = entry
    return JSONResponse(entry, status_code=201)


# ---------------------------------------------------------------------------
# OAuth 2.0 Authorization Code + PKCE endpoints
# ---------------------------------------------------------------------------

async def authorize_get(request: Request) -> HTMLResponse | RedirectResponse:
    # Without OAuth params, fall through to the manual login portal
    if not request.query_params.get("response_type"):
        return RedirectResponse("/oauth/login", status_code=302)

    error = request.query_params.get("error", "")
    error_html = f'<p class="error">{error}</p>' if error else ""

    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{request.query_params.get(k, "")}">'
        for k in ("redirect_uri", "state", "code_challenge", "code_challenge_method", "client_id")
    )
    return HTMLResponse(_AUTHORIZE_HTML.format(error=error_html, hidden=hidden))


async def authorize_post(request: Request) -> RedirectResponse:
    form = await request.form()
    username              = str(form.get("username", "")).strip()
    password              = str(form.get("password", "")).strip()
    redirect_uri          = str(form.get("redirect_uri", ""))
    state                 = str(form.get("state", ""))
    code_challenge        = str(form.get("code_challenge", ""))
    code_challenge_method = str(form.get("code_challenge_method", "S256"))
    client_id             = str(form.get("client_id", ""))

    if not username or not password:
        params = _rebuild_authorize_params(form)
        return RedirectResponse(
            f"/oauth/authorize?error=Email+and+password+required&{params}", status_code=303
        )

    def _validate():
        common = xmlrpc.client.ServerProxy(f"{_url()}/xmlrpc/2/common")
        return common.authenticate(_db(), username, password, {})

    try:
        uid = await asyncio.to_thread(_validate)
    except Exception:
        uid = 0

    if not uid:
        params = _rebuild_authorize_params(form)
        return RedirectResponse(
            f"/oauth/authorize?error=Invalid+Odoo+credentials&{params}", status_code=303
        )

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "username": username,
        "credential": password,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "expires_at": time.time() + _AUTH_CODE_TTL,
    }

    qs = urlencode({"code": code, "state": state})
    return RedirectResponse(f"{redirect_uri}?{qs}", status_code=302)


async def token_post(request: Request) -> JSONResponse:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        get = lambda k: body.get(k, "")
    else:
        form = await request.form()
        get = lambda k: str(form.get(k, ""))

    grant_type    = get("grant_type")
    code          = get("code")
    code_verifier = get("code_verifier")

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    entry = _auth_codes.get(code)
    if not entry:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "Unknown or expired code"},
            status_code=400,
        )

    if time.time() > entry["expires_at"]:
        del _auth_codes[code]
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "Code expired"},
            status_code=400,
        )

    if entry["code_challenge_method"] == "S256":
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if not hmac.compare_digest(computed, entry["code_challenge"]):
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                status_code=400,
            )

    del _auth_codes[code]

    access_token = token_store.add(entry["username"], entry["credential"])
    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 30 * 24 * 3600,
    })


def _rebuild_authorize_params(form) -> str:
    return urlencode({
        k: form.get(k, "")
        for k in ("redirect_uri", "state", "code_challenge", "code_challenge_method", "client_id")
    })


# ---------------------------------------------------------------------------
# Manual login portal (backward compat — direct browser visit)
# ---------------------------------------------------------------------------

async def login_get(request: Request) -> HTMLResponse:
    error = request.query_params.get("error", "")
    error_html = f'<p class="error">{error}</p>' if error else ""
    return HTMLResponse(_LOGIN_HTML.format(error=error_html))


async def login_post(request: Request) -> RedirectResponse:
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", "")).strip()

    if not username or not password:
        return RedirectResponse("/oauth/login?error=Email+and+password+required", status_code=303)

    def _validate():
        common = xmlrpc.client.ServerProxy(f"{_url()}/xmlrpc/2/common")
        return common.authenticate(_db(), username, password, {})

    try:
        uid = await asyncio.to_thread(_validate)
    except Exception:
        uid = 0

    if not uid:
        return RedirectResponse("/oauth/login?error=Invalid+Odoo+credentials", status_code=303)

    token = token_store.add(username, password)
    return RedirectResponse(f"/oauth/success?token={token}", status_code=303)


async def success_page(request: Request) -> HTMLResponse:
    token = request.query_params.get("token", "")
    base_url = str(request.base_url)
    return HTMLResponse(_SUCCESS_HTML.format(token=token, base_url=base_url))


async def revoke_post(request: Request) -> RedirectResponse:
    form = await request.form()
    token = str(form.get("token", ""))
    token_store.revoke(token)
    return RedirectResponse("/oauth/login?error=Token+revoked+successfully", status_code=303)
