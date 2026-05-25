"""
PURPOSE: Six read-only MCP tools for fetching and searching Odoo project tasks and attachments.
EXPORTS: get_ticket, list_tickets, get_ticket_summary, search_tickets, list_attachments, get_attachment
DEPENDS ON: cache.py (cache, TTL_TICKET, TTL_LIST), odoo_client.py (client), graph.py (graph)
PATTERNS: Graph-first reads — check graph before cache/RPC; shape responses with _build_envelope() or _envelope_from_graph(). _search_and_count() runs search_read + search_count in parallel.
DO NOT USE FOR: mutations — any write belongs in tools/write.py.
"""
from cache import TTL_LIST, TTL_TICKET, cache
from graph import graph
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


def _envelope_from_graph(ticket: dict, detail: bool = False) -> dict:
    """Convert a raw graph ticket dict to the same shape _build_envelope produces."""
    project = ticket.get("project_id")  # already {id, name} | None
    envelope = {
        "id": ticket["id"],
        "title": ticket["name"],
        "stage": ticket.get("stage_id"),         # already {id, name} | None
        "priority": PRIORITY.get(str(ticket.get("priority", "0")), "low"),
        "assignees": ticket.get("user_ids", []),  # already [{id, name}, ...]
        "tags": ticket.get("tag_ids", []),        # already [{id, name}, ...]
        "project": project,
        "url": client.build_url(ticket["id"], project["id"] if project else None),
    }
    if detail:
        raw_desc = client.strip_html(ticket.get("description") or "")
        envelope["description"] = raw_desc[:500] + ("…" if len(raw_desc) > 500 else "")
        envelope["subtask_count"] = len(ticket.get("child_ids") or [])
        envelope["deadline"] = ticket.get("date_deadline") or None
        envelope["created_at"] = ticket.get("create_date") or None
        envelope["updated_at"] = ticket.get("write_date") or None
    return envelope


def _find_in_graph(ticket_id: int) -> tuple[int, dict] | None:
    """Return (project_id, ticket_dict) for a ticket in any active project, or None."""
    for pid, sub in graph.projects.items():
        t = sub["tickets"].get(ticket_id)
        if t is not None:
            return (pid, t)
    return None


async def get_ticket(
    ticket_id: int,
    detail: bool = False,
    fresh: bool = False,
    model: str = "project.task",
) -> dict:
    """Fetch a single task by ID. Set detail=true for description, subtask count, and dates. Set fresh=true to bypass graph and re-fetch from Odoo."""
    if fresh:
        try:
            raw = await graph.refresh_ticket(ticket_id, model=model)
            return _envelope_from_graph(raw, detail=detail)
        except Exception:
            pass  # fall through to graph-first then RPC fallback

    # Try graph first
    found = _find_in_graph(ticket_id)
    if found is not None:
        _, raw = found
        if raw.get("stale"):
            # Auto-refresh stale tickets
            try:
                raw = await graph.refresh_ticket(ticket_id, model=model)
            except Exception:
                pass  # if refresh fails, serve stale
        return _envelope_from_graph(raw, detail=detail)

    # RPC fallback (existing path)
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
    fresh: bool = False,
    model: str = "project.task",
) -> dict:
    """List tasks with optional filters. Returns paginated identity envelopes (max 50 per call)."""
    limit = min(limit, 50)

    # Graph path: project known and active, not a forced refresh
    if project_id is not None and project_id in graph.projects and not fresh:
        filters: dict = {}
        if stage:
            stages = graph.projects[project_id]["stages"]
            matched = [s for s in stages.values() if stage.lower() in s["name"].lower()]
            if matched:
                filters["stage_id"] = matched[0]["id"]
            # if no match, fall through to RPC for accuracy
            else:
                return await _list_tickets_rpc(project_id, stage, tag, assigned_to, priority, search, limit, offset, model)
        if assigned_to is not None:
            filters["assignee_id"] = assigned_to
        if tag:
            matched_tags = [tid for tid, t in graph.tags.items() if tag.lower() in t.get("name", "").lower()]
            if matched_tags:
                filters["tag_id"] = matched_tags[0]
            else:
                # Tag not found in graph — fall through to RPC for accuracy
                return await _list_tickets_rpc(project_id, stage, tag, assigned_to, priority, search, limit, offset, model)
        if priority is not None:
            filters["priority"] = priority
        if search:
            filters["search"] = search

        all_tickets = graph.list_tickets(project_id, filters if filters else None)
        total = len(all_tickets)
        page = all_tickets[offset:offset + limit]
        tickets = [_envelope_from_graph(t) for t in page]
        return {
            "tickets": tickets,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(tickets) < total,
            "source": "graph",
        }

    return await _list_tickets_rpc(project_id, stage, tag, assigned_to, priority, search, limit, offset, model)


async def _list_tickets_rpc(
    project_id: int | None,
    stage: str | None,
    tag: str | None,
    assigned_to: int | None,
    priority: str | None,
    search: str | None,
    limit: int,
    offset: int,
    model: str,
) -> dict:
    """RPC fallback for list_tickets."""
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
    result: dict = {
        "tickets": tickets,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(tickets) < total,
    }
    if project_id is not None and project_id not in graph.projects:
        result["hint"] = "Project not in active graph; call add_project_to_graph for faster reads"
    cache.set(key, result, TTL_LIST)
    return result


async def get_ticket_summary(
    ticket_ids: list[int],
    model: str = "project.task",
) -> list[dict]:
    """One-liner summary per ticket. Most token-efficient overview. Max 100 IDs."""
    if len(ticket_ids) > 100:
        raise ValueError("Max 100 IDs per call")

    # Try graph: collect those found
    graph_results: dict[int, dict] = {}
    for tid in ticket_ids:
        found = _find_in_graph(tid)
        if found:
            _, raw = found
            assignees = raw.get("user_ids", [])
            stage = raw.get("stage_id")
            graph_results[tid] = {
                "id": raw["id"],
                "title": raw["name"],
                "assignee": assignees[0]["name"] if assignees else "Unassigned",
                "stage": stage["name"] if stage else "Unknown",
            }
    if len(graph_results) == len(ticket_ids):
        return [graph_results[tid] for tid in ticket_ids if tid in graph_results]

    # If any ticket not in graph, fall through to RPC for ALL (simpler, consistent)
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
    fresh: bool = False,
    model: str = "project.task",
) -> dict:
    """Full-text search across task title and description."""
    # Graph path when project is active and not forced refresh
    if project_id is not None and project_id in graph.projects and not fresh:
        raw_tickets = graph.search_tickets(project_id, query)
        total = len(raw_tickets)
        page = raw_tickets[:limit]
        return {
            "tickets": [_envelope_from_graph(t) for t in page],
            "total": total,
            "limit": limit,
            "offset": 0,
            "has_more": len(page) < total,
            "source": "graph",
        }

    # RPC fallback
    domain: list = ["|", ["name", "ilike", query], ["description", "ilike", query]]
    if project_id is not None:
        domain = [["project_id", "=", project_id]] + domain
    records, total = await _search_and_count(model, domain, IDENTITY_FIELDS, limit, 0)
    tickets = [_build_envelope(r) for r in records]
    result: dict = {
        "tickets": tickets,
        "total": total,
        "limit": limit,
        "offset": 0,
        "has_more": len(tickets) < total,
    }
    if project_id is not None and project_id not in graph.projects:
        result["hint"] = "Project not in active graph; call add_project_to_graph for faster reads"
    return result


async def list_attachments(
    ticket_id: int,
    model: str = "project.task",
) -> list[dict]:
    """List all files attached to a ticket. Returns [{id, filename, mimetype, size, created_at}]."""
    records = await client._rpc(
        "ir.attachment",
        "search_read",
        [[["res_model", "=", model], ["res_id", "=", ticket_id]]],
        {"fields": ["id", "name", "mimetype", "file_size", "create_date"]},
    )
    return [
        {
            "id": r["id"],
            "filename": r["name"],
            "mimetype": r["mimetype"],
            "size": r.get("file_size"),
            "created_at": r.get("create_date"),
        }
        for r in records
    ]


async def get_attachment(attachment_id: int) -> dict:
    """Fetch the decoded text content of an attachment by ID. Use list_attachments first to find the ID."""
    import base64
    records = await client._rpc(
        "ir.attachment",
        "read",
        [[attachment_id]],
        {"fields": ["id", "name", "datas", "mimetype", "file_size", "res_id"]},
    )
    if not records:
        raise ValueError(f"Attachment {attachment_id} not found")
    r = records[0]
    raw = base64.b64decode(r["datas"]).decode("utf-8", errors="replace") if r.get("datas") else ""
    return {
        "id": r["id"],
        "filename": r["name"],
        "mimetype": r["mimetype"],
        "size": r.get("file_size"),
        "ticket_id": r["res_id"],
        "content": raw,
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
