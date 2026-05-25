"""
PURPOSE: Single MCP tool for listing Odoo reference data (projects, stages, users, tags).
EXPORTS: list_metadata
DEPENDS ON: cache.py (cache, TTL_META, TTL_USERS), odoo_client.py (client), graph.py (graph)
PATTERNS: Graph-first for stages/users/tags when available; long TTLs (TTL_META=600s, TTL_USERS=300s) for RPC fallback. Cache key includes project_id for stage/tag filtering.
DO NOT USE FOR: task-level data — use tools/read.py for ticket queries.
"""
from cache import TTL_META, TTL_USERS, cache
from graph import graph
from odoo_client import client

VALID_RESOURCES = {"projects", "stages", "users", "tags"}


async def list_metadata(
    resource: str,
    project_id: int | None = None,
    fresh: bool = False,
) -> list[dict]:
    """List Odoo metadata. resource: projects | stages | users | tags. project_id filters stages."""
    if resource not in VALID_RESOURCES:
        raise ValueError(f"resource must be one of: {', '.join(sorted(VALID_RESOURCES))}")

    if resource == "projects":
        # Always RPC for projects — graph only stores active subgraphs, not the full project list
        key = "meta:projects"
        if not fresh:
            hit = cache.get(key)
            if hit is not None:
                return hit
        records = await client._rpc(
            "project.project", "search_read",
            [[["active", "=", True]]], {"fields": ["id", "name"], "order": "name asc"},
        )
        result = [{"id": r["id"], "name": r["name"]} for r in records]
        cache.set(key, result, TTL_META)
        return result

    if resource == "stages":
        # Graph-first when project is active and not forced refresh
        if not fresh and project_id is not None:
            graph_stages = graph.list_metadata("stages", project_id)
            if graph_stages is not None:
                return graph_stages
        key = f"meta:stages:{project_id}"
        if not fresh:
            hit = cache.get(key)
            if hit is not None:
                return hit
        domain = [["project_ids", "in", [project_id]]] if project_id else []
        records = await client._rpc(
            "project.task.type", "search_read",
            [domain], {"fields": ["id", "name", "sequence"], "order": "sequence asc"},
        )
        result = [{"id": r["id"], "name": r["name"], "sequence": r["sequence"]} for r in records]
        cache.set(key, result, TTL_META)
        return result

    if resource == "users":
        # Graph-first when graph.users is populated and not forced refresh
        if not fresh and graph.users:
            return list(graph.users.values())
        key = "meta:users"
        if not fresh:
            hit = cache.get(key)
            if hit is not None:
                return hit
        records = await client._rpc(
            "res.users", "search_read",
            [[["active", "=", True], ["share", "=", False]]],
            {"fields": ["id", "name", "login"], "order": "name asc"},
        )
        result = [{"id": r["id"], "name": r["name"], "login": r["login"]} for r in records]
        cache.set(key, result, TTL_USERS)
        return result

    # resource == "tags"
    # Graph-first when graph.tags is populated and not forced refresh
    if not fresh and graph.tags:
        return list(graph.tags.values())
    key = f"meta:tags:{project_id}"
    if not fresh:
        hit = cache.get(key)
        if hit is not None:
            return hit
    records = await client._rpc(
        "project.tags", "search_read",
        [[]], {"fields": ["id", "name"], "order": "name asc"},
    )
    result = [{"id": r["id"], "name": r["name"]} for r in records]
    cache.set(key, result, TTL_META)
    return result
