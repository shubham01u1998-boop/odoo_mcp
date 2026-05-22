"""
PURPOSE: Ten write/mutation MCP tools for creating, updating, and deleting Odoo tasks.
EXPORTS: create_project, create_stage, create_tag, create_ticket, bulk_create_stages, bulk_create_tickets, update_ticket, transition_stage, add_subtasks, add_comment, delete_ticket
DEPENDS ON: cache.py (cache), odoo_client.py (client), tools/read.py (get_ticket — imported lazily to avoid circular import)
PATTERNS: Call _rpc() then invalidate_prefix() on affected cache keys. Markdown descriptions auto-converted via client.md_to_html(). Many2many writes use Odoo command [(6, 0, id_list)].
DO NOT USE FOR: read-only queries — use tools/read.py instead.
"""
import base64
import mimetypes
from datetime import datetime

from cache import cache
from odoo_client import client

DEFAULT_STAGES = ["Backlog", "In Progress", "In Review", "Done"]


def _validate_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid deadline '{value}' — must be YYYY-MM-DD")


async def create_project(
    name: str,
    description: str | None = None,
    stages: list[str] | None = None,
) -> dict:
    """Create a new Odoo project. Auto-creates stages if not supplied. Returns project + stage IDs."""
    vals: dict = {"name": name}
    if description is not None:
        vals["description"] = description
    project_id: int = await client._rpc("project.project", "create", [vals])
    cache.invalidate_prefix("meta:projects")

    stage_names = stages if stages is not None else DEFAULT_STAGES
    created_stages = []
    for i, stage_name in enumerate(stage_names):
        stage_vals = {
            "name": stage_name,
            "project_ids": [(4, project_id)],
            "sequence": (i + 1) * 10,
        }
        stage_id: int = await client._rpc("project.task.type", "create", [stage_vals])
        created_stages.append({"id": stage_id, "name": stage_name, "sequence": (i + 1) * 10})
    cache.invalidate_prefix(f"meta:stages:{project_id}")

    return {"id": project_id, "name": name, "stages": created_stages}


async def create_stage(
    name: str,
    project_id: int,
    sequence: int = 10,
) -> dict:
    """Create a Kanban stage/sprint column and assign it to a project."""
    vals: dict = {
        "name": name,
        "project_ids": [(4, project_id)],
        "sequence": sequence,
    }
    new_id: int = await client._rpc("project.task.type", "create", [vals])
    cache.invalidate_prefix(f"meta:stages:{project_id}")
    records = await client._rpc(
        "project.task.type", "read", [[new_id]],
        {"fields": ["id", "name", "sequence"]},
    )
    return {"id": records[0]["id"], "name": records[0]["name"], "sequence": records[0]["sequence"]}


async def create_ticket(
    title: str,
    project_id: int,
    description: str | None = None,
    stage_id: int | None = None,
    assignee_ids: list[int] | None = None,
    tag_ids: list[int] | None = None,
    priority: str = "0",
    deadline: str | None = None,
    subtasks: list[str] | None = None,
    model: str = "project.task",
) -> dict:
    """Create a new task. Returns {id, title, stage, description}."""
    if deadline:
        _validate_date(deadline)
    vals: dict = {"name": title, "project_id": project_id, "priority": priority}
    if description is not None:
        vals["description"] = description if description.strip().startswith("<") else client.md_to_html(description)
    if stage_id is not None:
        vals["stage_id"] = stage_id
    if assignee_ids:
        vals["user_ids"] = [(6, 0, assignee_ids)]
    if tag_ids:
        vals["tag_ids"] = [(6, 0, tag_ids)]
    if deadline:
        vals["date_deadline"] = deadline

    new_id: int = await client._rpc(model, "create", [vals])
    cache.invalidate_prefix(f"list:{model}")
    records = await client._rpc(
        model, "read", [[new_id]],
        {"fields": ["id", "name", "stage_id", "description"]},
    )
    r = records[0]
    raw_desc = client.strip_html(r.get("description") or "")
    result = {
        "id": r["id"],
        "title": r["name"],
        "stage": client.flatten_many2one(r.get("stage_id")),
        "description": raw_desc[:200] + ("…" if len(raw_desc) > 200 else ""),
        "subtask_count": 0,
    }
    if subtasks:
        sub = await add_subtasks(r["id"], subtasks, model=model)
        result["subtask_count"] = sub["created"]
    return result


async def bulk_create_stages(
    stages: list[dict],
    project_id: int,
) -> dict:
    """Create multiple stages for a project. Each stage: {name, sequence?}.
    Stops at first failure. Returns {created, stages} or {created, failed_at, error, stage_ids}."""
    created = []
    for i, s in enumerate(stages):
        try:
            result = await create_stage(s["name"], project_id, s.get("sequence", (i + 1) * 10))
            created.append(result)
        except Exception as exc:
            return {
                "created": i,
                "failed_at": i,
                "error": str(exc),
                "stage_ids": [c["id"] for c in created],
            }
    return {"created": len(created), "stages": created}


async def create_tag(name: str) -> dict:
    """Create a new project tag. Returns {id, name}."""
    new_id: int = await client._rpc("project.tags", "create", [{"name": name}])
    records = await client._rpc(
        "project.tags", "read", [[new_id]], {"fields": ["id", "name"]}
    )
    return {"id": records[0]["id"], "name": records[0]["name"]}


async def add_subtasks(
    ticket_id: int,
    subtasks: list[str],
    model: str = "project.task",
) -> dict:
    """Add subtasks (child tasks) to an existing ticket. Returns {ticket_id, created, subtask_ids}."""
    records = await client._rpc(model, "read", [[ticket_id]], {"fields": ["project_id"]})
    if not records:
        raise ValueError(f"Ticket {ticket_id} not found")
    raw_project = records[0].get("project_id")
    if not raw_project:
        raise ValueError(f"Ticket {ticket_id} has no project — cannot create subtasks")
    project_id: int = raw_project[0]  # many2one returns [id, name]

    subtask_ids = []
    for name in subtasks:
        sub_id: int = await client._rpc(model, "create", [{"name": name, "project_id": project_id, "parent_id": ticket_id}])
        subtask_ids.append(sub_id)

    cache.invalidate_prefix(f"ticket:{model}:{ticket_id}:")
    return {"ticket_id": ticket_id, "created": len(subtask_ids), "subtask_ids": subtask_ids}


async def bulk_create_tickets(
    tickets: list[dict],
    project_id: int,
) -> dict:
    """Create multiple tickets. Each ticket: {title, stage_id?, description?, assignee_ids?, tag_ids?, priority?, deadline?, subtasks?}.
    Stops at first failure. Returns {created, tickets} or {created, failed_at, error, created_ids}."""
    created = []
    for i, t in enumerate(tickets):
        try:
            result = await create_ticket(
                title=t["title"],
                project_id=project_id,
                description=t.get("description"),
                stage_id=t.get("stage_id"),
                assignee_ids=t.get("assignee_ids"),
                tag_ids=t.get("tag_ids"),
                priority=t.get("priority", "0"),
                deadline=t.get("deadline"),
                subtasks=t.get("subtasks"),
            )
            created.append(result)
        except Exception as exc:
            return {
                "created": i,
                "failed_at": i,
                "error": str(exc),
                "created_ids": [r["id"] for r in created],
            }
    return {"created": len(created), "tickets": created}


async def update_ticket(
    ticket_id: int,
    title: str | None = None,
    description: str | None = None,
    stage_id: int | None = None,
    assignee_ids: list[int] | None = None,
    priority: str | None = None,
    deadline: str | None = None,
    model: str = "project.task",
) -> dict:
    """Patch-update a task. Only fields explicitly passed are changed in Odoo."""
    from tools.read import get_ticket
    if deadline is not None:
        _validate_date(deadline)
    vals: dict = {}
    if title is not None:
        vals["name"] = title
    if description is not None:
        vals["description"] = description if description.strip().startswith("<") else client.md_to_html(description)
    if stage_id is not None:
        vals["stage_id"] = stage_id
    if assignee_ids is not None:
        vals["user_ids"] = [(6, 0, assignee_ids)]
    if priority is not None:
        vals["priority"] = priority
    if deadline is not None:
        vals["date_deadline"] = deadline

    if not vals:
        raise ValueError("No fields to update — pass at least one parameter")

    await client._rpc(model, "write", [[ticket_id], vals])
    cache.invalidate_prefix(f"ticket:{model}:{ticket_id}:")
    cache.invalidate_prefix(f"list:{model}")
    return await get_ticket(ticket_id, detail=True, model=model)


async def delete_ticket(
    ticket_id: int,
    model: str = "project.task",
) -> dict:
    """Permanently delete a task by ID. Returns {ticket_id, deleted}."""
    await client._rpc(model, "unlink", [[ticket_id]])
    cache.invalidate_prefix(f"ticket:{model}:{ticket_id}:")
    cache.invalidate_prefix(f"list:{model}")
    return {"ticket_id": ticket_id, "deleted": True}


async def add_comment(
    ticket_id: int,
    body: str,
    model: str = "project.task",
) -> dict:
    """Post a public chatter message on a ticket. Notifies followers. Returns {ticket_id, message_id}."""
    message_id: int = await client._rpc(
        model, "message_post", [[ticket_id]],
        {"body": body, "message_type": "comment", "subtype_xmlid": "mail.mt_comment"},
    )
    return {"ticket_id": ticket_id, "message_id": message_id}


async def post_log_note(
    ticket_id: int,
    body: str,
    model: str = "project.task",
) -> dict:
    """Post an internal log note on a ticket. Visible to internal users only — does NOT notify followers. Returns {ticket_id, message_id}."""
    message_id: int = await client._rpc(
        model, "message_post", [[ticket_id]],
        {"body": body, "message_type": "comment", "subtype_xmlid": "mail.mt_note"},
    )
    return {"ticket_id": ticket_id, "message_id": message_id}


async def attach_file(
    ticket_id: int,
    filename: str,
    content: str,
    mimetype: str | None = None,
    overwrite: bool = False,
    model: str = "project.task",
) -> dict:
    """Attach a file to a ticket. content must be a UTF-8 text string (markdown, JSON, plain text).
    Set overwrite=True to replace an existing attachment with the same name in-place."""
    resolved_mimetype = mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

    if overwrite:
        existing = await client._rpc(
            "ir.attachment", "search_read",
            [[["res_model", "=", model], ["res_id", "=", ticket_id], ["name", "=", filename]]],
            {"fields": ["id"], "limit": 1},
        )
        if existing:
            att_id = existing[0]["id"]
            await client._rpc("ir.attachment", "write", [[att_id], {"datas": encoded, "mimetype": resolved_mimetype}])
            return {"attachment_id": att_id, "filename": filename, "ticket_id": ticket_id, "mimetype": resolved_mimetype, "replaced": True}

    att_id: int = await client._rpc("ir.attachment", "create", [{
        "name": filename,
        "datas": encoded,
        "res_model": model,
        "res_id": ticket_id,
        "mimetype": resolved_mimetype,
    }])
    return {"attachment_id": att_id, "filename": filename, "ticket_id": ticket_id, "mimetype": resolved_mimetype, "replaced": False}


async def transition_stage(
    ticket_id: int,
    stage_name: str,
    model: str = "project.task",
) -> dict:
    """Move a task to a stage by name. Raises if the name is not found or ambiguous."""
    from tools.read import get_ticket
    ticket = await get_ticket(ticket_id, model=model)
    project = ticket.get("project")
    if not project:
        raise ValueError(f"Ticket {ticket_id} has no project — cannot resolve stage")

    stages = await client._rpc(
        "project.task.type",
        "search_read",
        [[["name", "ilike", stage_name], ["project_ids", "in", [project["id"]]]]],
        {"fields": ["id", "name"]},
    )
    if not stages:
        raise ValueError(
            f"Stage '{stage_name}' not found in project '{project['name']}'"
        )
    if len(stages) > 1:
        names = ", ".join(s["name"] for s in stages)
        raise ValueError(f"Ambiguous stage name — matches: {names}")

    result = await update_ticket(ticket_id, stage_id=stages[0]["id"], model=model)
    stage_lower = stage_name.lower()
    if stage_lower == "done":
        result["context_hint"] = (
            f"Ticket moved to Done. Run /ticket-context {ticket_id} in Claude Code "
            "to generate and attach the handoff context file."
        )
    elif "progress" in stage_lower:
        result["context_hint"] = (
            f"Ticket is now In Progress. Run /ticket-context {ticket_id} in Claude Code "
            "to attach any design specs, API contracts, or planning docs before you start."
        )
    return result
