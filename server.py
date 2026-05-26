"""
PURPOSE: MCP server entry point — bootstraps FastMCP and registers all 20 tools.
EXPORTS: none (run directly: `python server.py` or referenced in Claude MCP config)
DEPENDS ON: tools/read.py, tools/write.py, tools/utils.py
PATTERNS: To add a tool — import its function below and append to the _fn list.
DO NOT USE FOR: business logic — all logic lives in tools/.
"""
import json
import sys
import os

# Ensure the package root is on the path when launched by Claude Desktop
sys.path.insert(0, os.path.dirname(__file__))

from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import Route, Mount

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from tools.read import get_ticket, get_ticket_summary, list_tickets, search_tickets, list_attachments, get_attachment
from tools.write import (
    create_project, create_stage, create_tag, create_ticket,
    bulk_create_stages, bulk_create_tickets,
    update_ticket, transition_stage, add_subtasks, add_comment, post_log_note,
    delete_ticket, attach_file,
)
from tools.utils import list_metadata
from tools.graph_admin import add_project_to_graph, remove_project_from_graph, list_active_projects, refresh_project_graph, view_graph

mcp = FastMCP(
    "odoo-mcp",
    # Disable FastMCP's built-in DNS-rebinding guard — BearerAuthMiddleware is
    # our security layer, and the guard would reject any request whose Host header
    # isn't localhost (e.g. ngrok or any public domain).
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

for _fn in [
    get_ticket,
    list_tickets,
    get_ticket_summary,
    search_tickets,
    create_project,
    create_stage,
    create_tag,
    create_ticket,
    bulk_create_stages,
    bulk_create_tickets,
    update_ticket,
    transition_stage,
    add_subtasks,
    add_comment,
    post_log_note,
    delete_ticket,
    attach_file,
    list_metadata,
    list_attachments,
    get_attachment,
    add_project_to_graph,
    remove_project_from_graph,
    list_active_projects,
    refresh_project_graph,
    view_graph,
]:
    mcp.tool()(_fn)


class BearerAuthMiddleware:
    def __init__(self, app, token_map: dict[str, dict]):
        # token_map: {"<token>": {"username": "...", "api_key": "..."}, ...}
        self._app = app
        self._token_map = token_map
        self._sessions: dict[str, dict] = {}  # session_id -> creds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")

        # OAuth portal and discovery endpoints are public — no bearer token required
        if path.startswith("/oauth/") or path.startswith("/.well-known/"):
            await self._app(scope, receive, send)
            return

        # Streamable HTTP transport (MCP 2025-03-26): every request carries the token directly
        if path == "/mcp":
            token = self._extract_token(scope)
            creds = self._token_map.get(token)
            if creds is None:
                from auth.token_store import token_store
                if token_store.is_valid(token):
                    creds = token_store.get(token)
            if creds is None:
                await self._reject(scope, send)
                return
            from odoo_client import _request_creds
            _request_creds.set(creds)
            await self._app(scope, receive, send)
            return

        # Legacy SSE transport: auth on the initial GET /sse, then track by session_id
        if path == "/sse":
            token = self._extract_token(scope)
            creds = self._token_map.get(token)
            if creds is None:
                from auth.token_store import token_store
                if token_store.is_valid(token):
                    creds = token_store.get(token)
            if creds is None:
                await self._reject(scope, send)
                return
            # Capture the session_id FastMCP emits so we can route /messages/ calls
            captured_sid = [None]

            async def capturing_send(event):
                if captured_sid[0] is None and event["type"] == "http.response.body":
                    body = event.get("body", b"")
                    if b"session_id=" in body:
                        try:
                            for line in body.decode().splitlines():
                                if "session_id=" in line:
                                    sid = line.split("session_id=", 1)[1].split("&")[0].strip()
                                    if sid:
                                        self._sessions[sid] = creds
                                        captured_sid[0] = sid
                        except Exception:
                            pass
                await send(event)

            try:
                await self._app(scope, receive, capturing_send)
            finally:
                if captured_sid[0]:
                    self._sessions.pop(captured_sid[0], None)
            return

        if path.startswith("/messages/"):
            from urllib.parse import parse_qs
            qs = scope.get("query_string", b"").decode()
            params = parse_qs(qs)
            sid_list = params.get("session_id", [])
            if sid_list:
                creds = self._sessions.get(sid_list[0])
                if creds:
                    from odoo_client import _request_creds
                    _request_creds.set(creds)

        await self._app(scope, receive, send)

    def _extract_token(self, scope) -> str:
        auth = ""
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                auth = v.decode()
                break
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):]
        from urllib.parse import parse_qs
        qs = scope.get("query_string", b"").decode()
        params = parse_qs(qs)
        token_list = params.get("token", [])
        return token_list[0] if token_list else ""

    def _base_url(self, scope) -> str:
        """Reconstruct the public base URL, honouring proxy/ngrok forwarded headers."""
        headers = {k: v for k, v in scope.get("headers", [])}
        host = headers.get(b"x-forwarded-host", headers.get(b"host", b"")).decode()
        proto = headers.get(b"x-forwarded-proto", b"http").decode().split(",")[0].strip()
        return f"{proto}://{host}" if host else ""

    async def _reject(self, scope, send):
        body = b'{"error":"Unauthorized"}'
        base = self._base_url(scope)
        # MCP spec §3.1.1: WWW-Authenticate MUST include resource_metadata so
        # clients know where to find the OAuth protected-resource document.
        if base:
            rm = f"{base}/.well-known/oauth-protected-resource"
            www_auth = f'Bearer realm="Odoo MCP", resource_metadata="{rm}"'.encode()
        else:
            www_auth = b'Bearer realm="Odoo MCP"'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
                [b"www-authenticate", www_auth],
            ],
        })
        await send({"type": "http.response.body", "body": body})


if __name__ == "__main__":
    import logging
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        import asyncio
        import uvicorn

        raw_map = os.environ.get("MCP_TOKEN_MAP", "")
        if raw_map:
            token_map = json.loads(raw_map)
        else:
            # Backward-compat: single token uses env credentials
            single = os.environ.get("MCP_AUTH_TOKEN", "")
            if single:
                token_map = {single: {
                    "username": os.environ["ODOO_USERNAME"],
                    "api_key": os.environ["ODOO_API_KEY"],
                }}
            else:
                token_map = {}
                logging.warning("No MCP_TOKEN_MAP or MCP_AUTH_TOKEN — server is unauthenticated")

        from auth.oauth import (
            login_get, login_post, success_page, revoke_post,
            authorize_get, authorize_post, token_post, metadata_get,
            protected_resource_get, register_post,
        )
        # streamable_http_app() stores a session_manager with an anyio task group.
        # Starlette 1.x does not propagate sub-app lifespans, so we call
        # session_manager.run() explicitly in our outer lifespan.
        mcp_http_app = mcp.streamable_http_app()  # initialises mcp.session_manager

        @asynccontextmanager
        async def lifespan(app):
            async with mcp.session_manager.run():
                yield

        starlette_app = Starlette(
            lifespan=lifespan,
            routes=[
                Route("/.well-known/oauth-protected-resource", protected_resource_get, methods=["GET"]),
                Route("/.well-known/oauth-protected-resource/{path:path}", protected_resource_get, methods=["GET"]),
                Route("/.well-known/oauth-authorization-server", metadata_get, methods=["GET"]),
                Route("/oauth/authorize", authorize_get, methods=["GET"]),
                Route("/oauth/authorize", authorize_post, methods=["POST"]),
                Route("/oauth/token", token_post, methods=["POST"]),
                Route("/oauth/register", register_post, methods=["POST"]),
                Route("/oauth/login", login_get, methods=["GET"]),
                Route("/oauth/login", login_post, methods=["POST"]),
                Route("/oauth/success", success_page, methods=["GET"]),
                Route("/oauth/revoke", revoke_post, methods=["POST"]),
                Mount("/", mcp_http_app),
            ],
        )
        from starlette.middleware.cors import CORSMiddleware
        app = CORSMiddleware(
            BearerAuthMiddleware(starlette_app, token_map),
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["authorization", "content-type"],
            expose_headers=["www-authenticate"],
        )
        config = uvicorn.Config(
            app,
            host=os.environ.get("FASTMCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("FASTMCP_PORT", "8000")),
            log_level="info",
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
        asyncio.run(uvicorn.Server(config).serve())
    else:
        mcp.run(transport=transport)
