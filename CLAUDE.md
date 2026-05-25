# Odoo MCP Server — Navigation Guide

MCP server bridging Claude Code / Claude Desktop to Odoo 19 Enterprise via XML-RPC.
Exposes 25 tools for reading, creating, updating, deleting, annotating, and graph-managing project tasks.

---

## File Map

| File | Purpose | Read when… |
|---|---|---|
| `server.py` | FastMCP entry point; registers all tools | Adding or removing a tool |
| `odoo_client.py` | XML-RPC transport + HTML/MD helpers | Changing auth, RPC behaviour, or conversion helpers |
| `cache.py` | Thread-safe TTL cache (in-memory, lost on restart) | Changing TTL values or cache behaviour |
| `graph.py` | In-memory project graph cache with disk persistence | Changing graph structure, hydration, or patch logic |
| `tools/read.py` | 6 read tools: get_ticket, list_tickets, get_ticket_summary, search_tickets, list_attachments, get_attachment | Any read-side change |
| `tools/write.py` | 11 write tools: create/update/delete/bulk/transition/comment/log-note | Any write-side change |
| `tools/utils.py` | 1 metadata tool: list_metadata | Changing metadata queries |
| `tools/graph_admin.py` | 5 graph admin tools: add/remove/list/refresh project graph, view graph | Changing graph admin behaviour or adding new graph tools |
| `tests/test_mcp.py` | 110 unit tests (fully mocked, no live Odoo) | After any tool change |

**Never need to read:** `venv/`, `.git/`, `.odoo-mcp-graph/`

---

## Graph Cache

The graph cache (`graph.py`) stores a **ProjectSubgraph** per active project in memory, giving O(1) reads with no RPC round-trips. Graphs also survive MCP restarts via disk snapshots.

### How it works

- **Reads** check the graph first. If the project is in the graph, data is returned immediately. If not, the tool falls through to a live RPC call and returns a `hint` suggesting you call `add_project_to_graph`.
- **Writes** update Odoo first, then patch the in-memory graph via `apply_write(event)` (best-effort — a graph patch failure never blocks the write).
- **Disk snapshots** are stored at `<repo>/.odoo-mcp-graph/projects/<id>.json` (gitignored, written atomically). The graph is reloaded from disk automatically on MCP startup.

### Session startup pattern

At the start of any session where you'll work with a project, hydrate it into the graph:

```python
add_project_to_graph(project_id=42)   # ~1-2 s, 9 RPCs, idempotent
```

After that, `get_ticket`, `list_tickets`, `search_tickets`, and `list_metadata` all serve from the graph with no further RPC calls.

### When to use `fresh=True`

Read tools (`get_ticket`, `list_tickets`, `search_tickets`, `list_metadata`) accept a `fresh: bool = False` parameter. Pass `fresh=True` only when you are about to make an important decision and need a guaranteed fresh snapshot from Odoo — not for routine reads (it costs an RPC).

```python
get_ticket(ticket_id=123, fresh=True)   # bypasses graph, fetches from Odoo
```

### Handling external edits (colleagues editing via Odoo UI)

If someone else edits tickets via the Odoo web UI, the in-memory graph may be stale. Options:

- `get_ticket(id, fresh=True)` — re-fetch a single ticket from Odoo.
- `refresh_project_graph(project_id)` — delta sync: fetches all tickets changed since last hydration and detects deletions. Cheaper than a full re-hydration.

### Graph admin tools (in `tools/graph_admin.py`)

| Tool | What it does |
|---|---|
| `add_project_to_graph(project_id)` | Hydrates a project from Odoo (9 RPCs, ~1-2 s). Idempotent. |
| `remove_project_from_graph(project_id)` | Drops project from graph and frees memory. |
| `list_active_projects()` | Lists projects currently in the graph with ticket counts. |
| `refresh_project_graph(project_id)` | Delta sync: fetches changed tickets, detects deletions. |
| `view_graph(project_id, format)` | Renders graph in `"tree"` (ASCII), `"mermaid"`, or `"json"` (returns file path). |

---

## Change Playbook

**Add a new tool**
1. Write `async def your_tool(...)` in `tools/read.py` or `tools/write.py`
2. Import it in `server.py` and append to the `_fn` list
3. Add tests in `tests/test_mcp.py`
4. Run `python scripts/gen_index.py` to update `docs/function_index.md`

**Add a new graph admin tool**
1. Write `async def your_tool(...)` in `tools/graph_admin.py`
2. Import it in `server.py` and append to the `_fn` list
3. Add tests in `tests/test_mcp.py`
4. Run `python scripts/gen_index.py` to update `docs/function_index.md`

**Update a tool's logic or signature**
→ Edit `tools/read.py`, `tools/write.py`, or `tools/graph_admin.py` only + update tests

**Change graph hydration or patch logic**
→ Edit `graph.py` — `hydrate_project()`, `refresh_project()`, `apply_write()`

**Change a TTL value** (e.g. ticket cache from 60s → 90s)
→ Edit the constant in `cache.py` only — `TTL_TICKET`, `TTL_LIST`, `TTL_META`, `TTL_USERS`

**Change RPC auth or transport**
→ Edit `odoo_client.py` only — `_connect_sync()` or `_rpc_sync()`

**Add an HTML/Markdown conversion helper**
→ Edit `odoo_client.py` — static methods on `OdooClient`

**Scaffold a full sprint (project + stages + tickets)**
→ Use `create_project` (auto-creates 4 default stages), then `bulk_create_tickets`

---

## Key Patterns

**Graph-first read**
```python
# Serve from graph if project is loaded; fall back to RPC otherwise
with graph._lock:
    sub = graph.projects.get(project_id)
if sub is not None:
    ticket = sub["tickets"].get(ticket_id)
    if ticket:
        return _envelope_from_graph(ticket)
# Graph miss — fall through to RPC
result = await client._rpc("project.task", "search_read", [domain], {"fields": fields})
```

**Async RPC call**
```python
result = await client._rpc("project.task", "search_read", [domain], {"fields": fields, "limit": 50})
```

**Cache read-through**
```python
key = f"ticket:{model}:{ticket_id}:{detail}"
hit = cache.get(key)
if hit is not None:
    return hit
# ... fetch from Odoo ...
cache.set(key, result, TTL_TICKET)
```

**Invalidate cache on write**
```python
cache.invalidate_prefix(f"ticket:{model}:{ticket_id}:")
cache.invalidate_prefix(f"list:{model}")
```

**Odoo domain filter**
```python
domain = [["project_id", "=", 42], ["stage_id.name", "ilike", "Done"]]
```

**Many2many write (replace all)**
```python
vals["user_ids"] = [(6, 0, [user_id_1, user_id_2])]
```

**Markdown → Odoo HTML**
```python
html_body = client.md_to_html(plain_text)   # only call for non-HTML input
```

**Flatten Odoo relational fields**
```python
client.flatten_many2one(record["stage_id"])    # [id, name] → {id, name}
client.flatten_many2many(record["user_ids"])   # [[id, name], ...] → [{id, name}, ...]
```

---

## Conventions

- All tool entry points are `async def`; internal helpers may be sync
- Tools return `dict` or `list[dict]`; never raise to the caller — return `{"error": "..."}` for user-facing errors
- Descriptions: auto-detect HTML (`text.strip().startswith("<")`); otherwise convert via `client.md_to_html()`
- Odoo models used: `project.task`, `project.project`, `project.task.type`, `project.tags`, `res.users`
- Cache key prefixes: `ticket:`, `list:`, `meta:`

## Cache TTLs

| Constant | Value | Used for |
|---|---|---|
| `TTL_TICKET` | 60 s | Individual ticket reads |
| `TTL_LIST` | 60 s | List / search results |
| `TTL_META` | 600 s | Projects, stages, tags |
| `TTL_USERS` | 300 s | User directory |

---

## Running & Testing

```bash
# Run server (Claude Code)
python server.py

# Run all tests (no live Odoo needed)
pytest tests/test_mcp.py -v

# Regenerate function index after tool changes
python scripts/gen_index.py
```

Function reference: [`docs/function_index.md`](docs/function_index.md)
