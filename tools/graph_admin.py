"""
PURPOSE: 5 admin MCP tools for managing the in-memory graph (Phase 5).
EXPORTS: add_project_to_graph, remove_project_from_graph, list_active_projects,
         refresh_project_graph, view_graph
DEPENDS ON: graph.py (singleton `graph`)
PATTERNS: All functions are async and return dict or str. Errors return {"error": str}.
"""
from datetime import datetime, timezone

from graph import graph


async def add_project_to_graph(project_id: int) -> dict:
    """Add a project to the active graph by hydrating it from Odoo. Idempotent — calling twice re-hydrates. Returns {project_id, name, ticket_count, hydration_ms}."""
    try:
        return await graph.hydrate_project(project_id)
    except Exception as exc:
        return {"error": str(exc)}


async def remove_project_from_graph(project_id: int) -> dict:
    """Remove a project from the active graph and free memory. Subsequent reads of this project fall through to RPC. Returns {project_id, removed}."""
    with graph._lock:
        was_present = graph.projects.pop(project_id, None) is not None
        (graph._graph_directory / "projects" / f"{project_id}.json").unlink(missing_ok=True)
    return {"project_id": project_id, "removed": was_present}


async def list_active_projects() -> list:
    """List all projects currently in the active graph. Returns [{project_id, name, ticket_count, last_synced_at_iso}]."""
    with graph._lock:
        snapshots = list(graph.projects.values())
    result = []
    for sub in snapshots:
        last_synced_iso = datetime.fromtimestamp(sub["last_synced_at"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result.append({
            "project_id": sub["project_id"],
            "name": sub["project_name"],
            "ticket_count": len(sub["tickets"]),
            "last_synced_at_iso": last_synced_iso,
        })
    result.sort(key=lambda x: x["project_id"])
    return result


async def refresh_project_graph(project_id: int) -> dict:
    """Delta sync a project already in the active graph. Queries Odoo for tickets changed since last hydration, patches graph, detects deletions. Returns {project_id, updated_count, removed_count}."""
    try:
        return await graph.refresh_project(project_id)
    except Exception as exc:
        return {"error": str(exc)}


async def view_graph(project_id: int, format: str = "tree") -> object:  # noqa: A002
    """Render the current graph snapshot for a project.
    format="tree"    -> ASCII mind-map grouped by stage with inline counts.
    format="mermaid" -> Mermaid graph TD diagram (renders in Claude Desktop / GitHub).
    format="json"    -> Returns {"graph_file_path": "<abs path>"} — use Claude Code's Read tool to inspect the file directly.
    """
    output_format = format  # rename locally to avoid using the shadowed builtin
    if output_format not in {"tree", "mermaid", "json"}:
        raise ValueError(f"format must be one of 'tree', 'mermaid', 'json'; got {output_format!r}")

    with graph._lock:
        if project_id not in graph.projects:
            raise ValueError(f"Project {project_id} is not in the active graph; call add_project_to_graph first")
        sub = graph.projects[project_id]

    if output_format == "json":
        path = (graph._graph_directory / "projects" / f"{project_id}.json").resolve()
        return {"graph_file_path": str(path)}
    elif output_format == "tree":
        return _render_tree(sub)
    else:
        return _render_mermaid(sub)


# ---------------------------------------------------------------------------
# Tree renderer
# ---------------------------------------------------------------------------

def _render_tree(sub: dict) -> str:
    project_id = sub["project_id"]
    project_name = sub["project_name"]
    last_synced_iso = datetime.fromtimestamp(sub["last_synced_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    stages = sub["stages"]
    tickets = sub["tickets"]

    lines = []
    header = f"Project: {project_name} (#{project_id})     last_synced: {last_synced_iso}"
    lines.append(header)

    # Sort stages by sequence
    sorted_stages = sorted(stages.values(), key=lambda s: s.get("sequence", 0))

    # Build stage -> tickets map (top-level tickets only: parent_id is None)
    stage_tickets: dict[int, list] = {}
    for stage in sorted_stages:
        sid = stage["id"]
        stage_tickets[sid] = sorted(
            [t for t in tickets.values() if t.get("stage_id") and t["stage_id"]["id"] == sid and t.get("parent_id") is None],
            key=lambda t: t["id"]
        )

    # Also collect orphan top-level tickets (stage_id is None)
    orphan_tickets = sorted(
        [t for t in tickets.values() if t.get("stage_id") is None and t.get("parent_id") is None],
        key=lambda t: t["id"]
    )

    all_stages_list = list(sorted_stages)
    total_stages = len(all_stages_list)

    for stage_idx, stage in enumerate(all_stages_list):
        is_last_stage = (stage_idx == total_stages - 1) and (not orphan_tickets)
        stage_prefix = "└──" if is_last_stage else "├──"
        stage_continuation = "    " if is_last_stage else "│   "

        stage_ticket_list = stage_tickets[stage["id"]]
        ticket_count = len([t for t in tickets.values() if t.get("stage_id") and t["stage_id"]["id"] == stage["id"]])
        count_word = "ticket" if ticket_count == 1 else "tickets"
        lines.append(f"{stage_prefix} {stage['name']} ({ticket_count} {count_word})")

        total_stage_tickets = len(stage_ticket_list)
        for t_idx, ticket in enumerate(stage_ticket_list):
            is_last_ticket = (t_idx == total_stage_tickets - 1)
            ticket_prefix = f"{stage_continuation}└──" if is_last_ticket else f"{stage_continuation}├──"
            ticket_continuation = f"{stage_continuation}    " if is_last_ticket else f"{stage_continuation}│   "

            ticket_line = _format_ticket_line(ticket)
            lines.append(f"{ticket_prefix} {ticket_line}")

            # Subtasks (children)
            child_ids = ticket.get("child_ids", [])
            if child_ids:
                child_tickets = sorted(
                    [tickets[cid] for cid in child_ids if cid in tickets],
                    key=lambda t: t["id"]
                )
                total_children = len(child_tickets)
                for c_idx, child in enumerate(child_tickets):
                    is_last_child = (c_idx == total_children - 1)
                    child_prefix = f"{ticket_continuation}└──" if is_last_child else f"{ticket_continuation}├──"
                    child_line = _format_ticket_line(child)
                    lines.append(f"{child_prefix} {child_line}")

    # Orphan tickets
    for t_idx, ticket in enumerate(orphan_tickets):
        is_last_ticket = (t_idx == len(orphan_tickets) - 1)
        ticket_prefix = "└──" if is_last_ticket else "├──"
        ticket_line = _format_ticket_line(ticket)
        lines.append(f"{ticket_prefix} {ticket_line}")

    return "\n".join(lines)


def _format_ticket_line(ticket: dict) -> str:
    """Format a single ticket line for tree rendering."""
    tid = ticket["id"]
    name = ticket.get("name", "")
    if len(name) > 40:
        name = name[:40]

    parts = [f"#{tid}  {name}"]

    if ticket.get("priority") == "1":
        parts.append("★")

    user_ids = ticket.get("user_ids", [])
    assignee = user_ids[0]["name"] if user_ids else "Unassigned"
    parts.append(f"[{assignee}]")

    attachment_count = ticket.get("attachment_count", 0)
    comment_count = ticket.get("comment_count", 0)
    log_note_count = ticket.get("log_note_count", 0)

    glyphs = []
    if attachment_count > 0:
        glyphs.append(f"\U0001f4ce{attachment_count}")  # 📎
    if comment_count > 0:
        glyphs.append(f"\U0001f4ac{comment_count}")    # 💬
    if log_note_count > 0:
        glyphs.append(f"\U0001f4dd{log_note_count}")   # 📝

    if glyphs:
        parts.extend(glyphs)

    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Mermaid renderer
# ---------------------------------------------------------------------------

def _sanitize_mermaid_label(text: str) -> str:
    """Replace double quotes with single quotes for safe Mermaid labels."""
    return text.replace('"', "'")


def _render_mermaid(sub: dict) -> str:
    project_id = sub["project_id"]
    project_name = sub["project_name"]
    stages = sub["stages"]
    tickets = sub["tickets"]

    lines = ["graph TD"]

    # Project node
    safe_name = _sanitize_mermaid_label(project_name)
    lines.append(f'  P{project_id}["Project: {safe_name}"]')

    # Sort stages by sequence
    sorted_stages = sorted(stages.values(), key=lambda s: s.get("sequence", 0))

    for stage in sorted_stages:
        sid = stage["id"]
        safe_stage_name = _sanitize_mermaid_label(stage["name"])
        lines.append(f'  P{project_id} --> S{sid}["{safe_stage_name}"]')

        # Tickets in this stage (top-level only)
        stage_tickets = sorted(
            [t for t in tickets.values() if t.get("stage_id") and t["stage_id"]["id"] == sid and t.get("parent_id") is None],
            key=lambda t: t["id"]
        )

        for ticket in stage_tickets:
            tid = ticket["id"]
            ticket_label = _build_mermaid_ticket_label(ticket)
            lines.append(f'  S{sid} --> T{tid}["{ticket_label}"]')

            # Subtasks
            child_ids = ticket.get("child_ids", [])
            if child_ids:
                child_tickets = sorted(
                    [tickets[cid] for cid in child_ids if cid in tickets],
                    key=lambda t: t["id"]
                )
                for child in child_tickets:
                    cid = child["id"]
                    child_label = _build_mermaid_ticket_label(child)
                    lines.append(f'  T{tid} -.subtask.-> T{cid}["{child_label}"]')

    return "\n".join(lines)


def _build_mermaid_ticket_label(ticket: dict) -> str:
    """Build a Mermaid node label for a ticket."""
    tid = ticket["id"]
    name = ticket.get("name", "")
    if len(name) > 35:
        name = name[:35]
    name = _sanitize_mermaid_label(name)

    user_ids = ticket.get("user_ids", [])
    assignee = user_ids[0]["name"] if user_ids else "Unassigned"
    assignee = _sanitize_mermaid_label(assignee)

    priority_mark = "★ " if ticket.get("priority") == "1" else ""

    attachment_count = ticket.get("attachment_count", 0)
    comment_count = ticket.get("comment_count", 0)
    log_note_count = ticket.get("log_note_count", 0)

    glyphs = []
    if attachment_count > 0:
        glyphs.append(f"\U0001f4ce{attachment_count}")  # 📎
    if comment_count > 0:
        glyphs.append(f"\U0001f4ac{comment_count}")    # 💬
    if log_note_count > 0:
        glyphs.append(f"\U0001f4dd{log_note_count}")   # 📝

    glyph_str = " " + " ".join(glyphs) if glyphs else ""

    return f"#{tid} {name}<br/>{priority_mark}{assignee}{glyph_str}"
