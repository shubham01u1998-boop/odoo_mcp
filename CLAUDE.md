# Odoo MCP Server — Navigation Guide

MCP server bridging Claude Code / Claude Desktop to Odoo 19 Enterprise via XML-RPC.
Exposes 15 tools for reading, creating, updating, and deleting project tasks.

---

## File Map

| File | Purpose | Read when… |
|---|---|---|
| `server.py` | FastMCP entry point; registers all 15 tools | Adding or removing a tool |
| `odoo_client.py` | XML-RPC transport + HTML/MD helpers | Changing auth, RPC behaviour, or conversion helpers |
| `cache.py` | Thread-safe TTL cache (in-memory, lost on restart) | Changing TTL values or cache behaviour |
| `tools/read.py` | 4 read tools: get_ticket, list_tickets, get_ticket_summary, search_tickets | Any read-side change |
| `tools/write.py` | 10 write tools: create/update/delete/bulk/transition/comment | Any write-side change |
| `tools/utils.py` | 1 metadata tool: list_metadata | Changing metadata queries |
| `tests/test_mcp.py` | 38 unit tests (fully mocked, no live Odoo) | After any tool change |

**Never need to read:** `venv/`, `.git/`, `migrate_descriptions.py` (one-off migration script, not part of the live server)

---

## Change Playbook

**Add a new tool**
1. Write `async def your_tool(...)` in `tools/read.py` or `tools/write.py`
2. Import it in `server.py` and append to the `_fn` list
3. Add tests in `tests/test_mcp.py`
4. Run `python scripts/gen_index.py` to update `docs/function_index.md`

**Update a tool's logic or signature**
→ Edit `tools/read.py` or `tools/write.py` only + update tests

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
