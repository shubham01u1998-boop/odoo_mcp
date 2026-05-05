from cache import TTL_LIST, TTL_TICKET, cache
from odoo_client import client

PRIORITY = {"0": "low", "1": "high"}

IDENTITY_FIELDS = ["id", "name", "stage_id", "priority", "user_ids", "tag_ids", "project_id"]
DETAIL_FIELDS = ["description", "child_ids", "date_deadline", "create_date", "write_date"]


def _build_envelope(record: dict, detail: bool = False) -> dict:
    project = client.flatten_many2one(record.get("project_id"))
    envelope: dict = {
        "id": record["id"],
        "title": record["name"],
        "stage": client.flatten_many2one(record.get("stage_id")),
        "priority": PRIORITY.get(str(record.get("priority", "0")), "low"),
        "assignees": client.flatten_many2many(record.get("user_ids", [])),
        "tags": client.flatten_many2many(record.get("tag_ids", [])),
        "project": project,
        "url": client.build_url(record["id"], project["id"] if project else None),
    }
    if detail:
        raw_desc = client.strip_html(record.get("description") or "")
        envelope["description"] = raw_desc[:500] + ("…" if len(raw_desc) > 500 else "")
        envelope["subtask_count"] = len(record.get("child_ids") or [])
        envelope["deadline"] = record.get("date_deadline") or None
        envelope["created_at"] = record.get("create_date") or None
        envelope["updated_at"] = record.get("write_date") or None
    return envelope


async def get_ticket(
    ticket_id: int,
    detail: bool = False,
    model: str = "project.task",
) -> dict:
    """Fetch a single task by ID. Set detail=true for description, subtask count, and dates."""
    key = f"ticket:{model}:{ticket_id}:{detail}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    fields = IDENTITY_FIELDS + (DETAIL_FIELDS if detail else [])
    records = await client._rpc(model, "read", [[ticket_id]], {"fields": fields})
    if not records:
        raise ValueError(f"Ticket {ticket_id} not found")
    result = _build_envelope(records[0], detail=detail)
    cache.set(key, result, TTL_TICKET)
    return result


async def list_tickets(
    project_id: int | None = None,
    stage: str | None = None,
    tag: str | None = None,
    assigned_to: int | None = None,
    priority: str | None = None,
    search: str | None = None,
    limit: int = 20,
    offset: int = 0,
    model: str = "project.task",
) -> dict:
    """List tasks with optional filters. Returns paginated identity envelopes (max 50 per call)."""
    limit = min(limit, 50)
    key = f"list:{model}:{project_id}:{stage}:{tag}:{assigned_to}:{priority}:{search}:{limit}:{offset}"
    hit = cache.get(key)
    if hit is not None:
        return hit

    domain: list = []
    if project_id is not None:
        domain.append(["project_id", "=", project_id])
    if stage:
        domain.append(["stage_id.name", "ilike", stage])
    if tag:
        domain.append(["tag_ids.name", "ilike", tag])
    if assigned_to is not None:
        domain.append(["user_ids", "in", [assigned_to]])
    if priority is not None:
        domain.append(["priority", "=", priority])
    if search:
        domain.append("|")
        domain.append(["name", "ilike", search])
        domain.append(["description", "ilike", search])

    records, total = await _search_and_count(model, domain, IDENTITY_FIELDS, limit, offset)
    tickets = [_build_envelope(r) for r in records]
    result = {
        "tickets": tickets,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(tickets) < total,
    }
    cache.set(key, result, TTL_LIST)
    return result


async def get_ticket_summary(
    ticket_ids: list[int],
    model: str = "project.task",
) -> list[dict]:
    """One-liner summary per ticket. Most token-efficient overview. Max 100 IDs."""
    if len(ticket_ids) > 100:
        raise ValueError("Max 100 IDs per call")
    fields = ["id", "name", "user_ids", "stage_id"]
    records = await client._rpc(model, "read", [ticket_ids], {"fields": fields})
    result = []
    for r in records:
        assignees = client.flatten_many2many(r.get("user_ids", []))
        stage = client.flatten_many2one(r.get("stage_id"))
        result.append({
            "id": r["id"],
            "title": r["name"],
            "assignee": assignees[0].get("name", str(assignees[0]["id"])) if assignees else "Unassigned",
            "stage": stage["name"] if stage else "Unknown",
        })
    return result


async def search_tickets(
    query: str,
    project_id: int | None = None,
    limit: int = 10,
    model: str = "project.task",
) -> dict:
    """Full-text search across task title and description."""
    domain: list = ["|", ["name", "ilike", query], ["description", "ilike", query]]
    if project_id is not None:
        domain = [["project_id", "=", project_id]] + domain
    records, total = await _search_and_count(model, domain, IDENTITY_FIELDS, limit, 0)
    tickets = [_build_envelope(r) for r in records]
    return {
        "tickets": tickets,
        "total": total,
        "limit": limit,
        "offset": 0,
        "has_more": len(tickets) < total,
    }


async def _search_and_count(
    model: str, domain: list, fields: list, limit: int, offset: int
) -> tuple[list, int]:
    import asyncio as _asyncio
    records_coro = client._rpc(
        model,
        "search_read",
        [domain],
        {"fields": fields, "limit": limit, "offset": offset},
    )
    count_coro = client._rpc(model, "search_count", [domain])
    records, total = await _asyncio.gather(records_coro, count_coro)
    return records, total
