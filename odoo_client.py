"""
PURPOSE: XML-RPC transport layer and HTML/Markdown helpers for Odoo 19.
EXPORTS: client (singleton OdooClient), _request_creds (ContextVar for per-request credentials),
         methods: _rpc, check_project_access, md_to_html, strip_html, flatten_many2one,
         flatten_many2many, build_url
DEPENDS ON: stdlib (xmlrpc, asyncio, html, re, os, contextvars), markdown
PATTERNS: await client._rpc(model, method, args, kwargs) — always async; runs sync XML-RPC in
          thread pool via asyncio.to_thread(). In SSE/multi-user mode, BearerAuthMiddleware sets
          _request_creds before each tool call so each user's requests use their own Odoo account.
DO NOT USE FOR: caching — handle cache hits/misses in tools/ before calling _rpc().
"""
import asyncio
import html
import os
import re
import xmlrpc.client
from contextvars import ContextVar
from typing import Any

import markdown as _md

# Per-request credentials injected by BearerAuthMiddleware for multi-user SSE mode.
# Falls back to env-var credentials when not set (single-user / stdio mode).
_request_creds: ContextVar[dict | None] = ContextVar('odoo_request_creds', default=None)


class OdooClient:
    def __init__(self) -> None:
        self.url = os.environ["ODOO_URL"].rstrip("/")
        self.db = os.environ["ODOO_DB"]
        self._env_username = os.environ["ODOO_USERNAME"]
        self._env_api_key = os.environ["ODOO_API_KEY"]
        self._uid_cache: dict[str, int] = {}  # username -> uid

    def _get_creds(self) -> tuple[str, str]:
        ctx = _request_creds.get()
        if ctx:
            return ctx["username"], ctx["api_key"]
        return self._env_username, self._env_api_key

    def _connect_sync(self, username: str, api_key: str) -> int:
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        uid = common.authenticate(self.db, username, api_key, {})
        if not uid:
            raise ConnectionError(
                f"Odoo authentication failed for {username} — check credentials"
            )
        return uid

    def _rpc_sync(
        self, model: str, method: str, args: list, kwargs: dict | None = None
    ) -> Any:
        username, api_key = self._get_creds()
        if username not in self._uid_cache:
            self._uid_cache[username] = self._connect_sync(username, api_key)
        models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        return models.execute_kw(
            self.db, self._uid_cache[username], api_key, model, method, args, kwargs or {}
        )

    async def _rpc(
        self, model: str, method: str, args: list, kwargs: dict | None = None
    ) -> Any:
        return await asyncio.to_thread(self._rpc_sync, model, method, args, kwargs)

    async def check_project_access(self, project_id: int) -> None:
        """Raise PermissionError if the current user cannot read the target project."""
        count = await self._rpc(
            "project.project", "search_count", [[["id", "=", project_id]]]
        )
        if count == 0:
            username, _ = self._get_creds()
            raise PermissionError(
                f"User {username!r} has no access to project {project_id} "
                "— check Odoo project membership"
            )

    @staticmethod
    def md_to_html(text: str) -> str:
        """Convert plain text to Odoo 19 HTML. Only called for non-HTML input."""
        if not text or not text.strip():
            return '<p> </p>'
        paragraphs = text.split('\n\n')
        result = []
        for para in paragraphs:
            if para.strip():
                lines = para.strip().split('\n')
                result.append('<p>' + '<br/>'.join(lines) + '</p>')
        return ''.join(result) if result else '<p> </p>'

    @staticmethod
    def strip_html(text: str) -> str:
        if not text:
            return ""
        return html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()

    @staticmethod
    def flatten_many2one(field: Any) -> dict | None:
        if not field:
            return None
        return {"id": field[0], "name": field[1]}

    @staticmethod
    def flatten_many2many(fields: list) -> list[dict]:
        if not fields:
            return []
        if isinstance(fields[0], int):
            # Odoo read() returns many2many as plain integer IDs
            return [{"id": f} for f in fields]
        return [{"id": f[0], "name": f[1]} for f in fields]

    def build_url(self, record_id: int, project_id: int | None = None) -> str:
        if project_id:
            return f"{self.url}/odoo/project/{project_id}/task/{record_id}"
        return f"{self.url}/odoo/tasks/{record_id}"


client = OdooClient()
