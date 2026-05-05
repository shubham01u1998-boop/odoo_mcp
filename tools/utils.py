from cache import TTL_META, TTL_USERS, cache
from odoo_client import client

VALID_RESOURCES = {"projects", "stages", "users", "tags"}


async def list_metadata(
    resource: str,
    project_id: int | None = None,
) -> list[dict]:
    """List Odoo metadata. resource: projects | stages | users | tags. project_id filters stages."""
    if resource not in VALID_RESOURCES:
        raise ValueError(f"resource must be one of: {', '.join(sorted(VALID_RESOURCES))}")

    if resource == "projects":
        key = "meta:projects"
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
        key = f"meta:stages:{project_id}"
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
        key = "meta:users"
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
    key = f"meta:tags:{project_id}"
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
