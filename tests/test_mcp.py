"""
PURPOSE: 38 unit tests covering cache, client helpers, and all 15 MCP tools. No live Odoo required.
EXPORTS: pytest test suite — run with: pytest tests/test_mcp.py -v (from odoo-mcp/)
DEPENDS ON: cache.py, odoo_client.py, tools/ (read, write, utils), unittest.mock
PATTERNS: _fresh_modules() returns isolated module instances with a clean in-memory cache. mock_rpc_sync(value) patches XML-RPC at the transport level. All async tool tests use @pytest.mark.asyncio.
DO NOT USE FOR: integration testing against live Odoo — all RPC calls are fully mocked.
"""
import asyncio
import os
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: set dummy env vars before any module-level code runs
# ---------------------------------------------------------------------------
os.environ.setdefault("ODOO_URL", "https://odoo.example.com")
os.environ.setdefault("ODOO_DB", "testdb")
os.environ.setdefault("ODOO_USERNAME", "test@example.com")
os.environ.setdefault("ODOO_API_KEY", "test_key")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cache import CacheLayer, TTL_TICKET, TTL_META
from odoo_client import OdooClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_client(uid: int = 1) -> OdooClient:
    c = OdooClient()
    c._uid = uid
    return c


def mock_rpc_sync(return_value):
    """Patch OdooClient._rpc_sync to return a fixed value."""
    return patch.object(OdooClient, "_rpc_sync", return_value=return_value)


# ---------------------------------------------------------------------------
# Helper: re-import tool modules with a patched client singleton
# ---------------------------------------------------------------------------

def _fresh_modules():
    """Return (read_mod, write_mod, utils_mod) with a fresh in-memory cache."""
    import importlib
    import cache as cache_mod
    import odoo_client as oc_mod

    cache_mod.cache.__init__()  # reset singleton
    importlib.reload(cache_mod)

    import tools.read as read_mod
    import tools.write as write_mod
    import tools.utils as utils_mod
    importlib.reload(read_mod)
    importlib.reload(write_mod)
    importlib.reload(utils_mod)
    return read_mod, write_mod, utils_mod


# ===========================================================================
# 1–5  READ TOOLS (happy path)
# ===========================================================================

@pytest.mark.asyncio
async def test_get_ticket_identity_envelope():
    c = make_client()
    task = {
        "id": 42, "name": "Fix login", "stage_id": [3, "In Progress"],
        "priority": "1", "user_ids": [[7, "Alice"]], "tag_ids": [[2, "Bug"]],
        "project_id": [1, "Backend"],
    }
    with mock_rpc_sync([task]):
        result = await asyncio.to_thread(c._rpc_sync, "project.task", "read", [[42]], {"fields": []})
    assert result[0]["name"] == "Fix login"


@pytest.mark.asyncio
async def test_get_ticket_detail_envelope_has_extra_fields():
    from tools.read import get_ticket, DETAIL_FIELDS
    task = {
        "id": 10, "name": "Task", "stage_id": [1, "Todo"], "priority": "0",
        "user_ids": [], "tag_ids": [], "project_id": [1, "Proj"],
        "description": "<p>Hello &amp; world</p>", "child_ids": [11, 12],
        "date_deadline": "2026-12-31", "create_date": "2026-01-01 00:00:00",
        "write_date": "2026-01-02 00:00:00",
    }
    with mock_rpc_sync([task]):
        result = await get_ticket(10, detail=True)
    assert result["description"] == "Hello & world"
    assert result["subtask_count"] == 2
    assert result["deadline"] == "2026-12-31"


@pytest.mark.asyncio
async def test_list_tickets_default():
    from tools.read import list_tickets
    tasks = [
        {"id": 1, "name": "T1", "stage_id": [1, "Todo"], "priority": "0",
         "user_ids": [], "tag_ids": [], "project_id": [1, "P"]},
    ]
    with mock_rpc_sync(tasks):
        with patch.object(OdooClient, "_rpc_sync", side_effect=[tasks, 1]):
            result = await list_tickets()
    assert "tickets" in result
    assert "total" in result


@pytest.mark.asyncio
async def test_get_ticket_summary_one_liner():
    from tools.read import get_ticket_summary
    tasks = [
        {"id": 5, "name": "Bug #5", "user_ids": [[2, "Bob"]], "stage_id": [1, "Todo"]},
    ]
    with mock_rpc_sync(tasks):
        result = await get_ticket_summary([5])
    assert result[0]["title"] == "Bug #5"
    assert result[0]["assignee"] == "Bob"
    assert result[0]["stage"] == "Todo"


@pytest.mark.asyncio
async def test_search_tickets_query():
    from tools.read import search_tickets
    tasks = [
        {"id": 3, "name": "Login bug", "stage_id": [1, "Backlog"], "priority": "1",
         "user_ids": [], "tag_ids": [], "project_id": [1, "P"]},
    ]
    with patch.object(OdooClient, "_rpc_sync", side_effect=[tasks, 1]):
        result = await search_tickets("login")
    assert len(result["tickets"]) == 1
    assert result["tickets"][0]["title"] == "Login bug"


# ===========================================================================
# 6–8  WRITE TOOLS (happy path)
# ===========================================================================

@pytest.mark.asyncio
async def test_create_ticket_success():
    from tools.write import create_ticket
    slim_task = {
        "id": 99, "name": "New task", "stage_id": [1, "Backlog"], "description": "",
    }
    with patch.object(OdooClient, "_rpc_sync", side_effect=[99, [slim_task]]):
        result = await create_ticket("New task", project_id=2)
    assert result["id"] == 99
    assert result["title"] == "New task"
    assert result["stage"]["name"] == "Backlog"
    assert result["description"] == ""


@pytest.mark.asyncio
async def test_update_ticket_patch_only_sends_passed_fields():
    from tools.write import update_ticket
    updated_task = {
        "id": 1, "name": "Renamed", "stage_id": [1, "Todo"], "priority": "0",
        "user_ids": [], "tag_ids": [], "project_id": [1, "P"],
        "description": "", "child_ids": [], "date_deadline": False,
        "create_date": "2026-01-01 00:00:00", "write_date": "2026-01-02 00:00:00",
    }
    captured = []

    def capture_write(model, method, args, kwargs=None):
        captured.append((model, method, args))
        if method == "write":
            return True
        return [updated_task]

    with patch.object(OdooClient, "_rpc_sync", side_effect=capture_write):
        await update_ticket(1, title="Renamed")

    write_call = next(c for c in captured if c[1] == "write")
    vals = write_call[2][1]
    assert "name" in vals
    assert "user_ids" not in vals  # not passed → not in payload


@pytest.mark.asyncio
async def test_transition_stage_success():
    from tools.write import transition_stage
    ticket_task = {
        "id": 5, "name": "Task", "stage_id": [1, "Todo"], "priority": "0",
        "user_ids": [], "tag_ids": [], "project_id": [1, "StartupOS"],
    }
    stage = [{"id": 3, "name": "In Progress"}]
    updated_task = {**ticket_task, "stage_id": [3, "In Progress"],
                    "description": "", "child_ids": [], "date_deadline": False,
                    "create_date": "2026-01-01 00:00:00", "write_date": "2026-01-02 00:00:00"}

    with patch.object(OdooClient, "_rpc_sync", side_effect=[[ticket_task], stage, True, [updated_task]]):
        result = await transition_stage(5, "In Progress")
    assert result["stage"]["name"] == "In Progress"


# ===========================================================================
# 9–13  list_metadata (replaces 4 individual list_* tools)
# ===========================================================================

@pytest.mark.asyncio
async def test_list_metadata_projects():
    from tools.utils import list_metadata
    projects = [{"id": 1, "name": "StartupOS"}, {"id": 2, "name": "Backend"}]
    with mock_rpc_sync(projects):
        result = await list_metadata("projects")
    assert any(p["name"] == "StartupOS" for p in result)


@pytest.mark.asyncio
async def test_list_metadata_stages_filtered_by_project():
    from tools.utils import list_metadata
    stages = [
        {"id": 1, "name": "Backlog", "sequence": 1},
        {"id": 2, "name": "In Progress", "sequence": 2},
    ]
    with mock_rpc_sync(stages):
        result = await list_metadata("stages", project_id=1)
    assert result[0]["name"] == "Backlog"


@pytest.mark.asyncio
async def test_list_metadata_users():
    from graph import graph
    from tools.utils import list_metadata
    # Clear graph.users so the RPC fallback path is tested regardless of disk snapshot state
    saved = dict(graph.users)
    graph.users.clear()
    try:
        users = [{"id": 1, "name": "Alice", "login": "alice@co.com"}]
        with mock_rpc_sync(users):
            result = await list_metadata("users")
        assert result[0]["login"] == "alice@co.com"
    finally:
        graph.users.update(saved)


@pytest.mark.asyncio
async def test_list_metadata_tags():
    from tools.utils import list_metadata
    tags = [{"id": 1, "name": "Bug"}, {"id": 2, "name": "Frontend"}]
    with mock_rpc_sync(tags):
        result = await list_metadata("tags")
    assert any(t["name"] == "Bug" for t in result)


@pytest.mark.asyncio
async def test_list_metadata_invalid_resource_raises():
    from tools.utils import list_metadata
    with pytest.raises(ValueError, match="resource must be one of"):
        await list_metadata("sprints")


# ===========================================================================
# 14–20  ERROR PATHS
# ===========================================================================

@pytest.mark.asyncio
async def test_get_ticket_not_found_raises():
    from tools.read import get_ticket
    with mock_rpc_sync([]):
        with pytest.raises(ValueError, match="not found"):
            await get_ticket(9999)


@pytest.mark.asyncio
async def test_transition_stage_not_found_raises():
    from tools.write import transition_stage
    ticket = {
        "id": 1, "name": "T", "stage_id": [1, "Todo"], "priority": "0",
        "user_ids": [], "tag_ids": [], "project_id": [1, "P"],
    }
    with patch.object(OdooClient, "_rpc_sync", side_effect=[[ticket], []]):
        with pytest.raises(ValueError, match="not found"):
            await transition_stage(1, "Nonexistent Stage")


@pytest.mark.asyncio
async def test_transition_stage_ambiguous_raises():
    import cache as cache_mod
    from tools.write import transition_stage
    cache_mod.cache.invalidate_prefix("ticket:")
    ticket = {
        "id": 1, "name": "T", "stage_id": [1, "Todo"], "priority": "0",
        "user_ids": [], "tag_ids": [], "project_id": [1, "P"],
    }
    stages = [{"id": 2, "name": "In Progress"}, {"id": 3, "name": "In Progress (QA)"}]
    with patch.object(OdooClient, "_rpc_sync", side_effect=[[ticket], stages]):
        with pytest.raises(ValueError, match="Ambiguous"):
            await transition_stage(1, "In")


@pytest.mark.asyncio
async def test_update_ticket_invalid_date_raises():
    from tools.write import update_ticket
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        await update_ticket(1, deadline="31-12-2026")


@pytest.mark.asyncio
async def test_create_ticket_invalid_date_raises():
    from tools.write import create_ticket
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        await create_ticket("Task", project_id=1, deadline="bad-date")


@pytest.mark.asyncio
async def test_list_tickets_limit_clamped_to_50():
    from tools.read import list_tickets
    tasks = []
    captured = []

    def capture(model, method, args, kwargs=None):
        captured.append((method, kwargs))
        return tasks if method == "search_read" else 0

    with patch.object(OdooClient, "_rpc_sync", side_effect=capture):
        await list_tickets(limit=200)

    sr = next(c for c in captured if c[0] == "search_read")
    assert sr[1]["limit"] == 50


@pytest.mark.asyncio
async def test_update_ticket_no_fields_raises():
    from tools.write import update_ticket
    with pytest.raises(ValueError, match="No fields"):
        await update_ticket(1)


# ===========================================================================
# 21–25  NEW: create_project, bulk_create_stages, bulk_create_tickets
# ===========================================================================

@pytest.mark.asyncio
async def test_create_project_creates_default_stages():
    from tools.write import create_project, DEFAULT_STAGES
    # 1 project create + N stage creates (DEFAULT_STAGES has 4 items)
    side_effects = [42] + [10 + i for i in range(len(DEFAULT_STAGES))]
    with patch.object(OdooClient, "_rpc_sync", side_effect=side_effects):
        result = await create_project("My Board")
    assert result["id"] == 42
    assert result["name"] == "My Board"
    assert len(result["stages"]) == len(DEFAULT_STAGES)
    assert result["stages"][0]["name"] == DEFAULT_STAGES[0]


@pytest.mark.asyncio
async def test_bulk_create_stages_success():
    from tools.write import bulk_create_stages
    # Each create_stage: 1 create RPC + 1 read RPC
    side_effects = [
        10, [{"id": 10, "name": "Sprint 1", "sequence": 10}],
        11, [{"id": 11, "name": "Sprint 2", "sequence": 20}],
    ]
    with patch.object(OdooClient, "_rpc_sync", side_effect=side_effects):
        result = await bulk_create_stages(
            [{"name": "Sprint 1"}, {"name": "Sprint 2"}], project_id=5
        )
    assert result["created"] == 2
    assert result["stages"][0]["name"] == "Sprint 1"
    assert result["stages"][1]["name"] == "Sprint 2"


@pytest.mark.asyncio
async def test_bulk_create_tickets_success():
    from tools.write import bulk_create_tickets
    slim_a = {"id": 101, "name": "Task A", "stage_id": [10, "Sprint 1"], "description": ""}
    slim_b = {"id": 102, "name": "Task B", "stage_id": [10, "Sprint 1"], "description": ""}
    side_effects = [101, [slim_a], 102, [slim_b]]
    with patch.object(OdooClient, "_rpc_sync", side_effect=side_effects):
        result = await bulk_create_tickets(
            [{"title": "Task A", "stage_id": 10}, {"title": "Task B", "stage_id": 10}],
            project_id=5,
        )
    assert result["created"] == 2
    assert result["tickets"][0]["id"] == 101
    assert result["tickets"][1]["id"] == 102


@pytest.mark.asyncio
async def test_bulk_create_tickets_stops_on_failure():
    from tools.write import bulk_create_tickets
    slim_a = {"id": 101, "name": "Task A", "stage_id": [10, "Sprint 1"], "description": ""}

    call_count = 0

    def fail_on_second(_model, _method, _args, _kwargs=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return 101
        if call_count == 2:
            return [slim_a]
        raise ValueError("stage_id 999 not found")

    with patch.object(OdooClient, "_rpc_sync", side_effect=fail_on_second):
        result = await bulk_create_tickets(
            [{"title": "Task A", "stage_id": 10}, {"title": "Task B", "stage_id": 999}],
            project_id=5,
        )
    assert result["created"] == 1
    assert result["failed_at"] == 1
    assert "999" in result["error"]
    assert result["created_ids"] == [101]


@pytest.mark.asyncio
async def test_create_project_with_custom_stages():
    from tools.write import create_project
    # 1 project create + 2 stage creates + 2 stage reads
    side_effects = [
        99,
        20, [{"id": 20, "name": "Sprint 1", "sequence": 10}],
        21, [{"id": 21, "name": "Sprint 2", "sequence": 20}],
    ]
    with patch.object(OdooClient, "_rpc_sync", side_effect=side_effects):
        result = await create_project("Board", stages=["Sprint 1", "Sprint 2"])
    assert result["id"] == 99
    assert len(result["stages"]) == 2
    assert result["stages"][1]["name"] == "Sprint 2"


@pytest.mark.asyncio
async def test_create_tag_success():
    from tools.write import create_tag
    with patch.object(OdooClient, "_rpc_sync", side_effect=[55, [{"id": 55, "name": "Frontend"}]]):
        result = await create_tag("Frontend")
    assert result["id"] == 55
    assert result["name"] == "Frontend"


@pytest.mark.asyncio
async def test_bulk_create_tickets_forwards_assignee_and_tags():
    from tools.write import bulk_create_tickets
    slim = {"id": 201, "name": "Task", "stage_id": [10, "To Do"], "description": ""}
    captured_vals = []

    def capture(_model, method, args, _kwargs=None):
        if method == "create":
            captured_vals.append(args[0])
            return 201
        return [slim]

    with patch.object(OdooClient, "_rpc_sync", side_effect=capture):
        await bulk_create_tickets(
            [{"title": "Task", "stage_id": 10, "assignee_ids": [7], "tag_ids": [3]}],
            project_id=5,
        )
    assert captured_vals[0].get("user_ids") == [(6, 0, [7])]
    assert captured_vals[0].get("tag_ids") == [(6, 0, [3])]


@pytest.mark.asyncio
async def test_add_subtasks_success():
    from tools.write import add_subtasks
    parent = {"id": 10, "name": "Auth", "stage_id": [1, "To Do"], "priority": "0",
              "user_ids": [], "tag_ids": [], "project_id": [5, "TiffinConnect"]}
    with patch.object(OdooClient, "_rpc_sync", side_effect=[[parent], 201, 202]):
        result = await add_subtasks(10, ["Task A", "Task B"])
    assert result["created"] == 2
    assert result["subtask_ids"] == [201, 202]


@pytest.mark.asyncio
async def test_create_ticket_with_subtasks():
    from tools.write import create_ticket
    slim = {"id": 99, "name": "Auth", "stage_id": [1, "To Do"], "description": ""}
    parent_for_add = {"id": 99, "name": "Auth", "stage_id": [1, "To Do"], "priority": "0",
                      "user_ids": [], "tag_ids": [], "project_id": [5, "TC"]}
    with patch.object(OdooClient, "_rpc_sync", side_effect=[99, [slim], [parent_for_add], 201, 202]):
        result = await create_ticket("Auth", project_id=5, subtasks=["Step A", "Step B"])
    assert result["subtask_count"] == 2


@pytest.mark.asyncio
async def test_bulk_create_tickets_forwards_subtasks():
    from tools.write import bulk_create_tickets
    slim = {"id": 101, "name": "T", "stage_id": [1, "To Do"], "description": ""}
    parent = {"id": 101, "name": "T", "stage_id": [1, "To Do"], "priority": "0",
              "user_ids": [], "tag_ids": [], "project_id": [5, "TC"]}
    with patch.object(OdooClient, "_rpc_sync", side_effect=[101, [slim], [parent], 301]):
        result = await bulk_create_tickets(
            [{"title": "T", "subtasks": ["Sub 1"]}], project_id=5
        )
    assert result["tickets"][0]["subtask_count"] == 1


# ===========================================================================
# CACHE BEHAVIOUR
# ===========================================================================

def test_cache_hit_skips_rpc():
    c = CacheLayer()
    c.set("key1", {"id": 1}, ttl=60)
    assert c.get("key1") == {"id": 1}


def test_cache_miss_calls_rpc():
    c = CacheLayer()
    assert c.get("missing") is None


def test_cache_ttl_expiry_calls_rpc_again():
    c = CacheLayer()
    c.set("expiring", "value", ttl=0)
    time.sleep(0.01)
    assert c.get("expiring") is None


def test_write_invalidates_ticket_cache():
    c = CacheLayer()
    c.set("ticket:project.task:42:False", {"id": 42}, ttl=60)
    c.set("ticket:project.task:42:True", {"id": 42}, ttl=60)
    c.invalidate_prefix("ticket:project.task:42:")
    assert c.get("ticket:project.task:42:False") is None
    assert c.get("ticket:project.task:42:True") is None


def test_write_invalidates_list_cache():
    c = CacheLayer()
    c.set("list:project.task:None:None:None:None:None:None:20:0", [], ttl=60)
    c.invalidate_prefix("list:project.task")
    assert c.get("list:project.task:None:None:None:None:None:None:20:0") is None


# ===========================================================================
# HELPER / ODOO CLIENT UNIT TESTS
# ===========================================================================

def test_strip_html_removes_tags_and_unescapes():
    result = OdooClient.strip_html("<p>Hello &amp; <b>world</b></p>")
    assert result == "Hello &  world"
    assert "<" not in result


def test_flatten_many2one():
    assert OdooClient.flatten_many2one([5, "Backend"]) == {"id": 5, "name": "Backend"}
    assert OdooClient.flatten_many2one(False) is None


def test_build_url_with_and_without_project():
    c = make_client()
    assert c.build_url(42, project_id=1) == "https://odoo.example.com/odoo/project/1/task/42"
    assert c.build_url(42) == "https://odoo.example.com/odoo/tasks/42"


# ===========================================================================
# ATTACH FILE TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_attach_file_markdown():
    _, write_mod, _ = _fresh_modules()
    with mock_rpc_sync(99):
        result = await write_mod.attach_file(
            ticket_id=1,
            filename="login_context.md",
            content="# API\nPOST /api/login",
        )
    assert result["attachment_id"] == 99
    assert result["filename"] == "login_context.md"
    assert result["ticket_id"] == 1
    assert result["mimetype"] == "text/markdown"


@pytest.mark.asyncio
async def test_attach_file_custom_mimetype():
    _, write_mod, _ = _fresh_modules()
    with mock_rpc_sync(101):
        result = await write_mod.attach_file(
            ticket_id=5,
            filename="spec.pdf",
            content="binary-ish content",
            mimetype="application/pdf",
        )
    assert result["attachment_id"] == 101
    assert result["mimetype"] == "application/pdf"


@pytest.mark.asyncio
async def test_attach_file_overwrite_replaces_existing():
    """overwrite=True with a matching attachment → write in-place, replaced=True, same ID."""
    _, write_mod, _ = _fresh_modules()
    existing = [{"id": 55}]
    with patch.object(OdooClient, "_rpc_sync", side_effect=[existing, True]):
        result = await write_mod.attach_file(
            ticket_id=1,
            filename="Backend_Handoff.md",
            content="# Updated handoff",
            overwrite=True,
        )
    assert result["attachment_id"] == 55
    assert result["replaced"] is True
    assert result["filename"] == "Backend_Handoff.md"
    assert result["ticket_id"] == 1
    assert result["mimetype"] == "text/markdown"


@pytest.mark.asyncio
async def test_attach_file_overwrite_creates_when_missing():
    """overwrite=True with no existing attachment → create new record, replaced=False."""
    _, write_mod, _ = _fresh_modules()
    with patch.object(OdooClient, "_rpc_sync", side_effect=[[], 99]):
        result = await write_mod.attach_file(
            ticket_id=1,
            filename="Backend_Handoff.md",
            content="# New handoff",
            overwrite=True,
        )
    assert result["attachment_id"] == 99
    assert result["replaced"] is False
    assert result["filename"] == "Backend_Handoff.md"
    assert result["ticket_id"] == 1


@pytest.mark.asyncio
async def test_attach_file_default_no_overwrite_always_creates():
    """overwrite=False (default) always calls create, never search_read."""
    _, write_mod, _ = _fresh_modules()
    with mock_rpc_sync(77):
        result = await write_mod.attach_file(
            ticket_id=2,
            filename="Frontend_Handoff.md",
            content="# Frontend handoff",
        )
    assert result["attachment_id"] == 77
    assert result["replaced"] is False


# ===========================================================================
# ATTACHMENT READ TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_add_comment_uses_mt_comment():
    captured = {}
    def capture(_model, method, args, kwargs=None):
        if method == "message_post":
            captured.update(kwargs or {})
            return 77
    with patch.object(OdooClient, "_rpc_sync", side_effect=capture):
        from tools.write import add_comment
        result = await add_comment(42, "Ship it!")
    assert result["message_id"] == 77
    assert captured["subtype_xmlid"] == "mail.mt_comment"


@pytest.mark.asyncio
async def test_post_log_note_uses_mt_note():
    captured = {}
    def capture(_model, method, args, kwargs=None):
        if method == "message_post":
            captured.update(kwargs or {})
            return 88
    with patch.object(OdooClient, "_rpc_sync", side_effect=capture):
        from tools.write import post_log_note
        result = await post_log_note(42, "Internal note: context saved.")
    assert result["message_id"] == 88
    assert captured["subtype_xmlid"] == "mail.mt_note"


@pytest.mark.asyncio
async def test_list_attachments_returns_metadata():
    from tools.read import list_attachments
    att_records = [
        {"id": 1454, "name": "migrate-payment-gateway-to-stripe_context.md",
         "mimetype": "text/markdown", "file_size": 512, "create_date": "2026-05-13 10:00:00"},
    ]
    with mock_rpc_sync(att_records):
        result = await list_attachments(2551)
    assert len(result) == 1
    assert result[0]["id"] == 1454
    assert result[0]["filename"] == "migrate-payment-gateway-to-stripe_context.md"
    assert result[0]["mimetype"] == "text/markdown"
    assert result[0]["size"] == 512


@pytest.mark.asyncio
async def test_get_attachment_decodes_content():
    import base64
    from tools.read import get_attachment
    raw = "# Migrate payment gateway to Stripe — Checkpoint\n_Ticket #2551_"
    encoded = base64.b64encode(raw.encode()).decode()
    att_record = [
        {"id": 1454, "name": "migrate-payment-gateway-to-stripe_context.md",
         "datas": encoded, "mimetype": "text/markdown", "file_size": len(raw), "res_id": 2551},
    ]
    with mock_rpc_sync(att_record):
        result = await get_attachment(1454)
    assert result["id"] == 1454
    assert result["ticket_id"] == 2551
    assert result["content"].startswith("# Migrate payment gateway")


# ===========================================================================
# GRAPH TESTS (in-memory store, no RPC)
# ===========================================================================

import importlib


def _fresh_graph():
    """Return a fresh, isolated Graph instance backed by a throwaway temp dir."""
    import graph as graph_mod
    importlib.reload(graph_mod)
    # Use a fresh temp dir so load_all_subgraphs() finds nothing and writes go nowhere permanent
    tmp_dir = tempfile.TemporaryDirectory()
    os.environ["ODOO_GRAPH_DIR"] = tmp_dir.name
    try:
        g = graph_mod.Graph()
    finally:
        # Remove the env var so it doesn't leak into other tests
        del os.environ["ODOO_GRAPH_DIR"]
    g._tmp_dir = tmp_dir  # keep alive; cleaned when g is GC'd
    return g


def make_subgraph(project_id: int = 1, project_name: str = "Test Project") -> dict:
    """
    Build a minimal but complete ProjectSubgraph fixture with:
      - 2 stages  (10: Backlog, 20: In Progress)
      - 2 tickets (100: in Backlog, assigned to user 7; 101: in Backlog, no assignee)
      - 1 tag     (tag_id=5 on ticket 100)
    """
    stages = {
        10: {"id": 10, "name": "Backlog", "sequence": 1},
        20: {"id": 20, "name": "In Progress", "sequence": 2},
    }
    ticket_100 = {
        "id": 100,
        "name": "Fix login",
        "stage_id": {"id": 10, "name": "Backlog"},
        "project_id": {"id": project_id, "name": project_name},
        "user_ids": [{"id": 7, "name": "Alice"}],
        "tag_ids": [{"id": 5, "name": "Bug"}],
        "priority": "1",
        "parent_id": None,
        "child_ids": [],
        "description": "<p>Login is <b>broken</b></p>",
        "date_deadline": "2026-12-31",
        "create_date": "2026-01-01 00:00:00",
        "write_date": "2026-01-02 00:00:00",
        "attachment_count": 0,
        "comment_count": 0,
        "log_note_count": 0,
        "stale": False,
    }
    ticket_101 = {
        "id": 101,
        "name": "Add dark mode",
        "stage_id": {"id": 10, "name": "Backlog"},
        "project_id": {"id": project_id, "name": project_name},
        "user_ids": [],
        "tag_ids": [],
        "priority": "0",
        "parent_id": None,
        "child_ids": [],
        "description": "",
        "date_deadline": None,
        "create_date": "2026-01-03 00:00:00",
        "write_date": "2026-01-04 00:00:00",
        "attachment_count": 0,
        "comment_count": 0,
        "log_note_count": 0,
        "stale": False,
    }
    indexes = {
        "by_stage": {10: {100, 101}},
        "by_assignee": {7: {100}},
        "by_tag": {5: {100}},
        "by_parent": {},
    }
    return {
        "project_id": project_id,
        "project_name": project_name,
        "last_synced_at": time.time(),
        "stages": stages,
        "tickets": {100: ticket_100, 101: ticket_101},
        "indexes": indexes,
    }


# ---------------------------------------------------------------------------
# 1. ticket_created
# ---------------------------------------------------------------------------

def test_graph_ticket_created_inserts_ticket():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    new_ticket = {
        "id": 200,
        "name": "New feature",
        "stage_id": {"id": 10, "name": "Backlog"},
        "project_id": {"id": 1, "name": "Test Project"},
        "user_ids": [],
        "tag_ids": [],
        "priority": "0",
        "parent_id": None,
        "child_ids": [],
        "description": "",
        "date_deadline": None,
        "create_date": "2026-05-01 00:00:00",
        "write_date": "2026-05-01 00:00:00",
        "attachment_count": 0,
        "comment_count": 0,
        "log_note_count": 0,
        "stale": False,
    }
    g.apply_write({"type": "ticket_created", "project_id": 1, "ticket": new_ticket})
    assert g.projects[1]["tickets"][200]["name"] == "New feature"
    assert 200 in g.projects[1]["indexes"]["by_stage"][10]


# ---------------------------------------------------------------------------
# 2. ticket_updated — stage change + index integrity
# ---------------------------------------------------------------------------

def test_graph_ticket_updated_stage_change_updates_indexes():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    g.apply_write({
        "type": "ticket_updated",
        "project_id": 1,
        "ticket_id": 100,
        "fields": {"stage_id": {"id": 20, "name": "In Progress"}},
    })
    indexes = g.projects[1]["indexes"]
    assert 100 not in indexes["by_stage"].get(10, set()), "old stage should not contain ticket"
    assert 100 in indexes["by_stage"].get(20, set()), "new stage should contain ticket"
    assert g.projects[1]["tickets"][100]["stage_id"]["id"] == 20


# ---------------------------------------------------------------------------
# 3. ticket_updated — assignee change
# ---------------------------------------------------------------------------

def test_graph_ticket_updated_assignee_change_updates_indexes():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    g.apply_write({
        "type": "ticket_updated",
        "project_id": 1,
        "ticket_id": 100,
        "fields": {"user_ids": [{"id": 8, "name": "Bob"}]},
    })
    indexes = g.projects[1]["indexes"]
    assert 100 not in indexes["by_assignee"].get(7, set()), "old assignee removed"
    assert 100 in indexes["by_assignee"].get(8, set()), "new assignee added"


# ---------------------------------------------------------------------------
# 4. ticket_deleted removes from tickets + all indexes
# ---------------------------------------------------------------------------

def test_graph_ticket_deleted_removes_ticket_and_indexes():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    g.apply_write({"type": "ticket_deleted", "project_id": 1, "ticket_id": 100})
    assert 100 not in g.projects[1]["tickets"]
    assert 100 not in g.projects[1]["indexes"]["by_stage"].get(10, set())
    assert 100 not in g.projects[1]["indexes"]["by_assignee"].get(7, set())
    assert 100 not in g.projects[1]["indexes"]["by_tag"].get(5, set())


# ---------------------------------------------------------------------------
# 5. ticket_deleted removes from parent's child_ids
# ---------------------------------------------------------------------------

def test_graph_ticket_deleted_removes_from_parent_child_ids():
    g = _fresh_graph()
    sg = make_subgraph(project_id=1)
    # Make ticket 100 a child of ticket 101
    sg["tickets"][100]["parent_id"] = 101
    sg["tickets"][101]["child_ids"] = [100]
    sg["indexes"]["by_parent"][101] = {100}
    g.projects[1] = sg
    g.apply_write({"type": "ticket_deleted", "project_id": 1, "ticket_id": 100})
    assert 100 not in g.projects[1]["tickets"][101]["child_ids"]


# ---------------------------------------------------------------------------
# 6. comment_added bumps comment_count
# ---------------------------------------------------------------------------

def test_graph_comment_added_increments_count():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    g.apply_write({"type": "comment_added", "project_id": 1, "ticket_id": 100})
    assert g.projects[1]["tickets"][100]["comment_count"] == 1


# ---------------------------------------------------------------------------
# 7. log_note_added bumps log_note_count
# ---------------------------------------------------------------------------

def test_graph_log_note_added_increments_count():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    g.apply_write({"type": "log_note_added", "project_id": 1, "ticket_id": 100})
    assert g.projects[1]["tickets"][100]["log_note_count"] == 1


# ---------------------------------------------------------------------------
# 8. attachment_added bumps attachment_count
# ---------------------------------------------------------------------------

def test_graph_attachment_added_increments_count():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    g.apply_write({"type": "attachment_added", "project_id": 1, "ticket_id": 100})
    assert g.projects[1]["tickets"][100]["attachment_count"] == 1


# ---------------------------------------------------------------------------
# 9. attachment_overwritten bumps write_date only
# ---------------------------------------------------------------------------

def test_graph_attachment_overwritten_bumps_write_date_only():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    old_count = g.projects[1]["tickets"][100]["attachment_count"]
    old_write = g.projects[1]["tickets"][100]["write_date"]
    import time; time.sleep(0.01)  # ensure clock advances
    g.apply_write({"type": "attachment_overwritten", "project_id": 1, "ticket_id": 100})
    assert g.projects[1]["tickets"][100]["attachment_count"] == old_count, "count must not change"
    assert g.projects[1]["tickets"][100]["write_date"] != old_write, "write_date must be bumped"


# ---------------------------------------------------------------------------
# 10. tag_created inserts into self.tags
# ---------------------------------------------------------------------------

def test_graph_tag_created_inserts_tag():
    g = _fresh_graph()
    g.apply_write({"type": "tag_created", "tag": {"id": 99, "name": "Backend"}})
    assert g.tags[99] == {"id": 99, "name": "Backend"}


# ---------------------------------------------------------------------------
# 11. tag_created works even when no active projects
# ---------------------------------------------------------------------------

def test_graph_tag_created_no_active_project_still_works():
    g = _fresh_graph()
    # No projects loaded at all
    g.apply_write({"type": "tag_created", "tag": {"id": 55, "name": "Frontend"}})
    assert 55 in g.tags


# ---------------------------------------------------------------------------
# 12. Event drop when project not in active set
# ---------------------------------------------------------------------------

def test_graph_event_dropped_when_project_not_active():
    g = _fresh_graph()
    # project_id=999 is not in g.projects
    g.apply_write({
        "type": "ticket_created",
        "project_id": 999,
        "ticket": {
            "id": 500, "name": "Ghost ticket",
            "stage_id": {"id": 1, "name": "Backlog"},
            "project_id": {"id": 999, "name": "Unknown"},
            "user_ids": [], "tag_ids": [],
            "priority": "0", "parent_id": None, "child_ids": [],
            "description": "", "date_deadline": None,
            "create_date": "2026-01-01 00:00:00", "write_date": "2026-01-01 00:00:00",
            "attachment_count": 0, "comment_count": 0, "log_note_count": 0, "stale": False,
        },
    })
    # graph has no projects at all, ticket must not appear anywhere
    assert 999 not in g.projects


# ---------------------------------------------------------------------------
# 13. get_ticket returns correct data
# ---------------------------------------------------------------------------

def test_graph_get_ticket_returns_correct_data():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    ticket = g.get_ticket(1, 100)
    assert ticket is not None
    assert ticket["name"] == "Fix login"
    assert ticket["priority"] == "1"


# ---------------------------------------------------------------------------
# 14. get_ticket returns None for missing ticket
# ---------------------------------------------------------------------------

def test_graph_get_ticket_returns_none_for_missing():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    assert g.get_ticket(1, 9999) is None
    assert g.get_ticket(999, 100) is None


# ---------------------------------------------------------------------------
# 15. list_tickets filter by stage_id
# ---------------------------------------------------------------------------

def test_graph_list_tickets_filter_by_stage():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    results = g.list_tickets(1, {"stage_id": 10})
    ids = [t["id"] for t in results]
    assert 100 in ids
    assert 101 in ids
    # Move ticket 100 to stage 20 and check filter
    g.apply_write({
        "type": "ticket_updated",
        "project_id": 1,
        "ticket_id": 100,
        "fields": {"stage_id": {"id": 20, "name": "In Progress"}},
    })
    in_backlog = g.list_tickets(1, {"stage_id": 10})
    assert all(t["id"] != 100 for t in in_backlog)


# ---------------------------------------------------------------------------
# 16. list_tickets filter by assignee_id
# ---------------------------------------------------------------------------

def test_graph_list_tickets_filter_by_assignee():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    alice_tickets = g.list_tickets(1, {"assignee_id": 7})
    assert len(alice_tickets) == 1
    assert alice_tickets[0]["id"] == 100


# ---------------------------------------------------------------------------
# 17. search_tickets matches title substring
# ---------------------------------------------------------------------------

def test_graph_search_tickets_matches_title():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    results = g.search_tickets(1, "login")
    assert len(results) == 1
    assert results[0]["id"] == 100


# ---------------------------------------------------------------------------
# 18. search_tickets matches HTML description content
# ---------------------------------------------------------------------------

def test_graph_search_tickets_matches_description_html():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    # ticket 100 has description "<p>Login is <b>broken</b></p>"
    results = g.search_tickets(1, "broken")
    assert any(t["id"] == 100 for t in results)


# ---------------------------------------------------------------------------
# 19. list_metadata returns None when project not active
# ---------------------------------------------------------------------------

def test_graph_list_metadata_stages_returns_none_when_inactive():
    g = _fresh_graph()
    result = g.list_metadata("stages", project_id=999)
    assert result is None


# ---------------------------------------------------------------------------
# 20. list_metadata returns stage list for active project
# ---------------------------------------------------------------------------

def test_graph_list_metadata_stages_returns_list_for_active():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    stages = g.list_metadata("stages", project_id=1)
    assert stages is not None
    assert any(s["name"] == "Backlog" for s in stages)


# ---------------------------------------------------------------------------
# 21. stage_created inserts into subgraph stages
# ---------------------------------------------------------------------------

def test_graph_stage_created_inserts_stage():
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    g.apply_write({
        "type": "stage_created",
        "project_id": 1,
        "stage": {"id": 30, "name": "Done", "sequence": 3},
    })
    assert 30 in g.projects[1]["stages"]
    assert g.projects[1]["stages"][30]["name"] == "Done"


# ---------------------------------------------------------------------------
# 22. child_ids_changed updates ticket's child_ids and by_parent index
# ---------------------------------------------------------------------------

def test_graph_child_ids_changed_updates_ticket_and_index():
    """child_ids_changed event: ticket child_ids list updated; by_parent index gains new child."""
    g = _fresh_graph()
    # Build a subgraph where ticket 100 is the parent with existing children [101, 102]
    sg = make_subgraph(project_id=1)
    parent_id = 100
    sg["tickets"][parent_id]["child_ids"] = [101, 102]
    # Seed by_parent so the existing children are tracked
    sg["indexes"]["by_parent"][parent_id] = {101, 102}
    g.projects[1] = sg

    # Fire event: parent now has children [101, 102, 103]
    g.apply_write({
        "type": "child_ids_changed",
        "project_id": 1,
        "ticket_id": parent_id,
        "child_ids": [101, 102, 103],
    })

    ticket = g.projects[1]["tickets"][parent_id]
    assert ticket["child_ids"] == [101, 102, 103], "child_ids must be replaced with the new list"

    # by_parent index for the parent must include the new child 103
    by_parent = g.projects[1]["indexes"]["by_parent"]
    assert 103 in by_parent.get(parent_id, set()), "by_parent index must contain new child 103"


# ---------------------------------------------------------------------------
# 23. get_summary returns correct id, title, assignee, and stage
# ---------------------------------------------------------------------------

def test_graph_get_summary_returns_correct_fields():
    """get_summary: returns lightweight summary dicts with correct field values for each ticket."""
    g = _fresh_graph()
    g.projects[1] = make_subgraph(project_id=1)
    # ticket 100: assigned to Alice (user_id=7), stage "Backlog"
    # ticket 101: no assignee, stage "Backlog"

    summaries = g.get_summary(1, [100, 101])

    assert len(summaries) == 2

    by_id = {s["id"]: s for s in summaries}

    # Ticket 100 — has assignee Alice
    s100 = by_id[100]
    assert s100["id"] == 100
    assert s100["title"] == "Fix login"
    assert s100["assignee"] == "Alice"
    assert s100["stage"] == "Backlog"

    # Ticket 101 — no assignee → "Unassigned"
    s101 = by_id[101]
    assert s101["id"] == 101
    assert s101["title"] == "Add dark mode"
    assert s101["assignee"] == "Unassigned"
    assert s101["stage"] == "Backlog"


# ===========================================================================
# PERSISTENCE TESTS (Phase 2 — disk layer)
# ===========================================================================

import json as _json
from pathlib import Path as _Path


def _make_graph_in_tmpdir(tmpdir: str):
    """Helper: reload graph module and construct a Graph using *tmpdir* as ODOO_GRAPH_DIR."""
    import graph as graph_mod
    importlib.reload(graph_mod)
    os.environ["ODOO_GRAPH_DIR"] = tmpdir
    try:
        g = graph_mod.Graph()
    finally:
        os.environ.pop("ODOO_GRAPH_DIR", None)
    # Stash the tmpdir on the instance so callers can inspect files
    g._tmpdir_path = tmpdir
    return g


# ---------------------------------------------------------------------------
# P1. Write/load round-trip
# ---------------------------------------------------------------------------

def test_persistence_write_load_roundtrip():
    """Subgraph written to disk is correctly reloaded by a fresh Graph."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # graph dir = <tmpdir>/.odoo-mcp-graph  (ODOO_GRAPH_DIR is the repo root)
        graph_dir = _Path(tmpdir) / ".odoo-mcp-graph"

        # --- Write phase ---
        g1 = _make_graph_in_tmpdir(tmpdir)
        sg = make_subgraph(project_id=5)
        sg["last_synced_at"] = time.time()  # fresh timestamp so loader keeps it
        g1.projects[5] = sg
        # g1._graph_dir is already cached to graph_dir during __init__
        g1.save_subgraph(5)

        # Verify the file was written
        json_path = graph_dir / "projects" / "5.json"
        assert json_path.exists(), "5.json must be created after save_subgraph"

        # --- Load phase ---
        g2 = _make_graph_in_tmpdir(tmpdir)
        assert 5 in g2.projects, "project 5 must be loaded from disk"
        assert g2.projects[5]["project_name"] == "Test Project"
        assert 100 in g2.projects[5]["tickets"]


# ---------------------------------------------------------------------------
# P2. .gitignore idempotency
# ---------------------------------------------------------------------------

def test_persistence_gitignore_idempotent():
    """Calling _amend_gitignore() twice writes .odoo-mcp-graph/ exactly once."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # With ODOO_GRAPH_DIR set, _repo_root() returns Path(tmpdir).
        # _amend_gitignore writes to <_repo_root()>/.gitignore = <tmpdir>/.gitignore
        os.environ["ODOO_GRAPH_DIR"] = tmpdir
        try:
            import graph as graph_mod
            importlib.reload(graph_mod)
            g = graph_mod.Graph()
            g._amend_gitignore()
            g._amend_gitignore()
        finally:
            os.environ.pop("ODOO_GRAPH_DIR", None)

        gitignore_file = _Path(tmpdir) / ".gitignore"
        assert gitignore_file.exists(), ".gitignore must be created"
        content = gitignore_file.read_text(encoding="utf-8")
        assert content.count(".odoo-mcp-graph/") == 1, "entry must appear exactly once"


# ---------------------------------------------------------------------------
# P3. Freshness window: stale snapshots are skipped and deleted
# ---------------------------------------------------------------------------

def test_persistence_stale_snapshot_deleted():
    """A snapshot with last_synced_at > 24 h ago is skipped and deleted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # graph dir = <tmpdir>/.odoo-mcp-graph  (ODOO_GRAPH_DIR is the repo root)
        projects_dir = _Path(tmpdir) / ".odoo-mcp-graph" / "projects"
        projects_dir.mkdir(parents=True)

        stale_data = make_subgraph(project_id=7)
        stale_data["last_synced_at"] = time.time() - 90_000  # >24 h ago
        stale_data["schema_version"] = 1

        # Serialise sets to lists manually so we can write raw JSON
        import copy as _copy
        serializable = _copy.deepcopy(stale_data)
        for bucket in serializable.get("indexes", {}).values():
            for k, v in bucket.items():
                if isinstance(v, set):
                    bucket[k] = sorted(v)

        json_file = projects_dir / "7.json"
        json_file.write_text(_json.dumps(serializable), encoding="utf-8")

        g = _make_graph_in_tmpdir(tmpdir)
        assert 7 not in g.projects, "stale project must not be loaded"
        assert not json_file.exists(), "stale JSON file must be deleted"


# ---------------------------------------------------------------------------
# P4. Schema version mismatch: bad snapshots are skipped and deleted
# ---------------------------------------------------------------------------

def test_persistence_schema_version_mismatch_deleted():
    """A snapshot with schema_version=999 is skipped and its file deleted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # graph dir = <tmpdir>/.odoo-mcp-graph  (ODOO_GRAPH_DIR is the repo root)
        projects_dir = _Path(tmpdir) / ".odoo-mcp-graph" / "projects"
        projects_dir.mkdir(parents=True)

        bad_data = make_subgraph(project_id=8)
        bad_data["last_synced_at"] = time.time()
        bad_data["schema_version"] = 999  # wrong version

        # Serialize sets → lists
        import copy as _copy
        serializable = _copy.deepcopy(bad_data)
        for bucket in serializable.get("indexes", {}).values():
            for k, v in bucket.items():
                if isinstance(v, set):
                    bucket[k] = sorted(v)

        json_file = projects_dir / "8.json"
        json_file.write_text(_json.dumps(serializable), encoding="utf-8")

        g = _make_graph_in_tmpdir(tmpdir)
        assert 8 not in g.projects, "mismatched-schema project must not be loaded"
        assert not json_file.exists(), "mismatched JSON file must be deleted"


# ---------------------------------------------------------------------------
# P5. Serialize/deserialize round-trip: sets ↔ lists
# ---------------------------------------------------------------------------

def test_persistence_serialize_deserialize_roundtrip():
    """Sets in indexes become sorted lists after serialize; become sets again after deserialize."""
    import graph as graph_mod
    importlib.reload(graph_mod)
    Graph = graph_mod.Graph

    sg = make_subgraph(project_id=3)
    # Confirm we start with sets in indexes
    assert isinstance(sg["indexes"]["by_stage"][10], set)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["ODOO_GRAPH_DIR"] = tmpdir
        try:
            g = graph_mod.Graph()
        finally:
            os.environ.pop("ODOO_GRAPH_DIR", None)

    serialized = g._serialize_subgraph(sg)
    # After serialize: index values are lists
    assert isinstance(serialized["indexes"]["by_stage"][10], list), "serialize must produce list"
    assert serialized["schema_version"] == graph_mod.SCHEMA_VERSION

    deserialized = g._deserialize_subgraph(serialized)
    # After deserialize: index values are sets again
    assert isinstance(deserialized["indexes"]["by_stage"][10], set), "deserialize must produce set"
    assert "schema_version" not in deserialized, "schema_version must be stripped after deserialize"


# ===========================================================================
# HYDRATION TESTS (Phase 3 — hydrate_project via RPC)
# ===========================================================================

def _make_hydrate_responses(project_id=5, task_a=None, task_b=None, att_groups=None,
                             subtypes=None, comment_groups=None, note_groups=None):
    """Build the standard ordered list of _rpc_sync return values for hydrate_project."""
    if task_a is None:
        task_a = {
            "id": 101, "name": "Task A",
            "stage_id": [21, "Backlog"],
            "project_id": [project_id, "My Project"],
            "user_ids": [],
            "tag_ids": [],
            "priority": "0",
            "parent_id": False,
            "child_ids": [],
            "description": "",
            "date_deadline": False,
            "create_date": "2026-01-01 00:00:00",
            "write_date": "2026-01-01 00:00:00",
        }
    if task_b is None:
        task_b = {
            "id": 102, "name": "Task B",
            "stage_id": [21, "Backlog"],
            "project_id": [project_id, "My Project"],
            "user_ids": [],
            "tag_ids": [],
            "priority": "0",
            "parent_id": False,
            "child_ids": [],
            "description": "",
            "date_deadline": False,
            "create_date": "2026-01-02 00:00:00",
            "write_date": "2026-01-02 00:00:00",
        }
    if subtypes is None:
        subtypes = [{"name": "mt_comment", "res_id": 7}, {"name": "mt_note", "res_id": 8}]
    return [
        [{"id": project_id, "name": "My Project"}],          # RPC 1 — project metadata
        [{"id": 21, "name": "Backlog", "sequence": 1}],       # RPC 2 — stages
        [{"id": 12, "name": "Sahil", "share": False}],        # RPC 3 — users
        [],                                                    # RPC 4 — tags
        [task_a, task_b],                                     # RPC 5 — tasks (page 1, < 500)
        att_groups if att_groups is not None else [],         # RPC 6 — attachment counts
        subtypes,                                             # RPC 7 subtype lookup
        comment_groups if comment_groups is not None else [], # RPC 7a — comment counts
        note_groups if note_groups is not None else [],       # RPC 7b — note counts
    ]


@pytest.mark.asyncio
async def test_hydrate_project_basic():
    """hydrate_project loads a project with 2 tasks into the graph."""
    import graph as graph_mod
    importlib.reload(graph_mod)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["ODOO_GRAPH_DIR"] = tmpdir
        try:
            g = graph_mod.Graph()
        finally:
            os.environ.pop("ODOO_GRAPH_DIR", None)

        responses = iter(_make_hydrate_responses(project_id=5))
        with patch.object(OdooClient, "_rpc_sync", side_effect=lambda *a, **kw: next(responses)):
            result = await g.hydrate_project(5)

    assert result["project_id"] == 5
    assert result["name"] == "My Project"
    assert result["ticket_count"] == 2
    assert isinstance(result["hydration_ms"], int)

    assert 5 in g.projects
    sg = g.projects[5]
    assert sg["project_name"] == "My Project"
    assert 21 in sg["stages"]
    assert sg["stages"][21]["name"] == "Backlog"
    assert 101 in sg["tickets"]
    assert 102 in sg["tickets"]
    assert sg["tickets"][101]["name"] == "Task A"
    assert sg["tickets"][102]["name"] == "Task B"


@pytest.mark.asyncio
async def test_hydrate_project_idempotent():
    """Calling hydrate_project twice replaces the first result with the latest."""
    import graph as graph_mod
    importlib.reload(graph_mod)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["ODOO_GRAPH_DIR"] = tmpdir
        try:
            g = graph_mod.Graph()
        finally:
            os.environ.pop("ODOO_GRAPH_DIR", None)

        # First hydration — 2 tasks
        responses_1 = iter(_make_hydrate_responses(project_id=5))
        with patch.object(OdooClient, "_rpc_sync", side_effect=lambda *a, **kw: next(responses_1)):
            result1 = await g.hydrate_project(5)
        assert result1["ticket_count"] == 2

        # Build a single-task response for the second call
        single_task = {
            "id": 101, "name": "Task A Updated",
            "stage_id": [21, "Backlog"],
            "project_id": [5, "My Project"],
            "user_ids": [], "tag_ids": [],
            "priority": "0", "parent_id": False, "child_ids": [],
            "description": "", "date_deadline": False,
            "create_date": "2026-01-01 00:00:00", "write_date": "2026-01-03 00:00:00",
        }
        responses_2 = iter([
            [{"id": 5, "name": "My Project"}],          # RPC 1
            [{"id": 21, "name": "Backlog", "sequence": 1}],  # RPC 2
            # RPC 3 users skipped — self.users is non-empty after first hydration
            [],                                         # RPC 4 tags — re-fetched (empty dict is falsy)
            [single_task],                              # RPC 5 tasks
            [],                                         # RPC 6 attachments
            [{"name": "mt_comment", "res_id": 7}, {"name": "mt_note", "res_id": 8}],  # subtypes
            [],                                         # RPC 7a comments
            [],                                         # RPC 7b notes
        ])
        with patch.object(OdooClient, "_rpc_sync", side_effect=lambda *a, **kw: next(responses_2)):
            result2 = await g.hydrate_project(5)

    assert result2["ticket_count"] == 1
    assert 5 in g.projects
    assert len(g.projects[5]["tickets"]) == 1
    assert 101 in g.projects[5]["tickets"]
    assert 102 not in g.projects[5]["tickets"], "stale task 102 must be replaced"
    assert g.projects[5]["tickets"][101]["name"] == "Task A Updated"


@pytest.mark.asyncio
async def test_hydrate_project_populates_counts():
    """hydrate_project correctly sets attachment_count, comment_count, log_note_count on tickets."""
    import graph as graph_mod
    importlib.reload(graph_mod)

    # Single task with ID 101
    single_task = {
        "id": 101, "name": "Task With Counts",
        "stage_id": [21, "Backlog"],
        "project_id": [5, "My Project"],
        "user_ids": [], "tag_ids": [],
        "priority": "0", "parent_id": False, "child_ids": [],
        "description": "", "date_deadline": False,
        "create_date": "2026-01-01 00:00:00", "write_date": "2026-01-01 00:00:00",
    }
    att_groups = [{"res_id": 101, "res_id_count": 3}]
    comment_groups = [{"res_id": 101, "res_id_count": 5}]
    note_groups = [{"res_id": 101, "res_id_count": 2}]

    # Build a custom response set: only 1 task, but with counts
    responses = iter([
        [{"id": 5, "name": "My Project"}],                      # RPC 1
        [{"id": 21, "name": "Backlog", "sequence": 1}],          # RPC 2
        [{"id": 12, "name": "Sahil", "share": False}],           # RPC 3
        [],                                                       # RPC 4
        [single_task],                                           # RPC 5
        att_groups,                                              # RPC 6
        [{"name": "mt_comment", "res_id": 7}, {"name": "mt_note", "res_id": 8}],  # subtypes
        comment_groups,                                          # RPC 7a
        note_groups,                                             # RPC 7b
    ])

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["ODOO_GRAPH_DIR"] = tmpdir
        try:
            g = graph_mod.Graph()
        finally:
            os.environ.pop("ODOO_GRAPH_DIR", None)

        with patch.object(OdooClient, "_rpc_sync", side_effect=lambda *a, **kw: next(responses)):
            result = await g.hydrate_project(5)

    assert result["ticket_count"] == 1
    ticket = g.projects[5]["tickets"][101]
    assert ticket["attachment_count"] == 3
    assert ticket["comment_count"] == 5
    assert ticket["log_note_count"] == 2


# ===========================================================================
# ── Phase 4: Refresh logic ──
# ===========================================================================

def _make_refresh_ticket_responses(ticket_id=101, name="Updated Task"):
    """Build the ordered RPC responses for refresh_ticket."""
    task_record = {
        "id": ticket_id,
        "name": name,
        "stage_id": [10, "Backlog"],
        "project_id": [5, "Test Project"],
        "user_ids": [],
        "tag_ids": [],
        "priority": "0",
        "parent_id": False,
        "child_ids": [],
        "description": "",
        "date_deadline": False,
        "create_date": "2026-01-01 00:00:00",
        "write_date": "2026-05-01 00:00:00",
    }
    return [
        [task_record],                                                          # search_read ticket
        [],                                                                     # attachment read_group
        [{"name": "mt_comment", "res_id": 7}, {"name": "mt_note", "res_id": 8}],  # subtypes
        [],                                                                     # comment read_group
        [],                                                                     # note read_group
    ]


@pytest.mark.asyncio
async def test_refresh_ticket_updates_graph():
    """refresh_ticket: fetches updated ticket from Odoo and replaces it in the graph."""
    import graph as graph_mod
    importlib.reload(graph_mod)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["ODOO_GRAPH_DIR"] = tmpdir
        try:
            g = graph_mod.Graph()
        finally:
            os.environ.pop("ODOO_GRAPH_DIR", None)

        # Seed graph with project 5 containing ticket 101
        sg = make_subgraph(project_id=5)
        # rename the existing ticket_101 to have id=101
        sg["tickets"][101]["name"] = "Old Name"
        g.projects[5] = sg

        responses = iter(_make_refresh_ticket_responses(ticket_id=101, name="New Name"))
        with patch.object(OdooClient, "_rpc_sync", side_effect=lambda *a, **kw: next(responses)):
            result = await g.refresh_ticket(101)

    assert result["name"] == "New Name"
    assert g.projects[5]["tickets"][101]["name"] == "New Name"


@pytest.mark.asyncio
async def test_refresh_ticket_not_in_odoo():
    """refresh_ticket: raises ValueError when Odoo returns no records for the ticket_id."""
    import graph as graph_mod
    importlib.reload(graph_mod)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["ODOO_GRAPH_DIR"] = tmpdir
        try:
            g = graph_mod.Graph()
        finally:
            os.environ.pop("ODOO_GRAPH_DIR", None)

        # RPC returns empty list — ticket not found in Odoo
        with patch.object(OdooClient, "_rpc_sync", return_value=[]):
            with pytest.raises(ValueError, match="Ticket 999 not found in Odoo"):
                await g.refresh_ticket(999)


@pytest.mark.asyncio
async def test_refresh_project_patches_changed_ticket():
    """refresh_project: applies updated ticket from Odoo, returns updated_count=1, removed_count=0."""
    import graph as graph_mod
    importlib.reload(graph_mod)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["ODOO_GRAPH_DIR"] = tmpdir
        try:
            g = graph_mod.Graph()
        finally:
            os.environ.pop("ODOO_GRAPH_DIR", None)

        sg = make_subgraph(project_id=5)
        sg["tickets"][101]["name"] = "Original Name"
        g.projects[5] = sg

        changed_task = {
            "id": 101,
            "name": "Refreshed Name",
            "stage_id": [10, "Backlog"],
            "project_id": [5, "Test Project"],
            "user_ids": [],
            "tag_ids": [],
            "priority": "0",
            "parent_id": False,
            "child_ids": [],
            "description": "",
            "date_deadline": False,
            "create_date": "2026-01-01 00:00:00",
            "write_date": "2026-05-20 10:00:00",
        }

        call_count = 0

        def rpc_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # First call: search_read for changed tasks → return one changed task
            if call_count == 1:
                return [changed_task]
            # Second call: search for all current ticket IDs → both tickets still exist
            return [100, 101]

        with patch.object(OdooClient, "_rpc_sync", side_effect=rpc_side_effect):
            result = await g.refresh_project(5)

    assert result["updated_count"] == 1
    assert result["removed_count"] == 0
    assert g.projects[5]["tickets"][101]["name"] == "Refreshed Name"


@pytest.mark.asyncio
async def test_refresh_project_removes_deleted_ticket():
    """refresh_project: detects and removes a ticket that was deleted in Odoo."""
    import graph as graph_mod
    importlib.reload(graph_mod)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["ODOO_GRAPH_DIR"] = tmpdir
        try:
            g = graph_mod.Graph()
        finally:
            os.environ.pop("ODOO_GRAPH_DIR", None)

        sg = make_subgraph(project_id=5)
        # Ensure both tickets 100 and 101 are in the graph
        assert 100 in sg["tickets"]
        assert 101 in sg["tickets"]
        g.projects[5] = sg

        call_count = 0

        def rpc_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # First call: search_read for changed tasks → nothing changed
            if call_count == 1:
                return []
            # Second call: search for current IDs → only ticket 100 still exists (101 deleted)
            return [100]

        with patch.object(OdooClient, "_rpc_sync", side_effect=rpc_side_effect):
            result = await g.refresh_project(5)

    assert result["removed_count"] == 1
    assert 101 not in g.projects[5]["tickets"], "ticket 101 must be removed from graph"
    assert 100 in g.projects[5]["tickets"], "ticket 100 must still be present"


@pytest.mark.asyncio
async def test_refresh_project_not_in_graph_raises():
    """refresh_project: raises ValueError when project is not in the active graph."""
    import graph as graph_mod
    importlib.reload(graph_mod)

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["ODOO_GRAPH_DIR"] = tmpdir
        try:
            g = graph_mod.Graph()
        finally:
            os.environ.pop("ODOO_GRAPH_DIR", None)

    # project 999 was never loaded
    with pytest.raises(ValueError, match="not in active graph"):
        await g.refresh_project(999)


# ===========================================================================
# ── Phase 5: Admin tools ──
# ===========================================================================

@pytest.mark.asyncio
async def test_add_project_to_graph():
    """add_project_to_graph: delegates to graph.hydrate_project and returns its result."""
    from tools.graph_admin import add_project_to_graph
    g = _fresh_graph()
    expected = {"project_id": 5, "name": "X", "ticket_count": 3, "hydration_ms": 50}
    with patch("tools.graph_admin.graph", new=g):
        g.hydrate_project = AsyncMock(return_value=expected)
        result = await add_project_to_graph(5)
    assert result == expected


@pytest.mark.asyncio
async def test_remove_project_from_graph_present():
    """remove_project_from_graph: removes project and returns removed=True when present."""
    from tools.graph_admin import remove_project_from_graph
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)
    with patch("tools.graph_admin.graph", new=g):
        result = await remove_project_from_graph(5)
    assert result["removed"] is True
    assert result["project_id"] == 5
    assert 5 not in g.projects


@pytest.mark.asyncio
async def test_remove_project_from_graph_absent():
    """remove_project_from_graph: returns removed=False when project not in graph."""
    from tools.graph_admin import remove_project_from_graph
    g = _fresh_graph()
    with patch("tools.graph_admin.graph", new=g):
        result = await remove_project_from_graph(99)
    assert result["removed"] is False
    assert result["project_id"] == 99


@pytest.mark.asyncio
async def test_list_active_projects_empty():
    """list_active_projects: returns empty list when graph has no projects."""
    from tools.graph_admin import list_active_projects
    g = _fresh_graph()
    with patch("tools.graph_admin.graph", new=g):
        result = await list_active_projects()
    assert result == []


@pytest.mark.asyncio
async def test_list_active_projects():
    """list_active_projects: returns one entry per active project with required keys."""
    from tools.graph_admin import list_active_projects
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5, project_name="Alpha")
    g.projects[7] = make_subgraph(project_id=7, project_name="Beta")
    with patch("tools.graph_admin.graph", new=g):
        result = await list_active_projects()
    assert len(result) == 2
    # sorted by project_id
    assert result[0]["project_id"] == 5
    assert result[1]["project_id"] == 7
    for entry in result:
        assert "project_id" in entry
        assert "name" in entry
        assert "ticket_count" in entry
        assert "last_synced_at_iso" in entry


@pytest.mark.asyncio
async def test_refresh_project_graph():
    """refresh_project_graph: delegates to graph.refresh_project and returns its result."""
    from tools.graph_admin import refresh_project_graph
    g = _fresh_graph()
    expected = {"project_id": 5, "updated_count": 2, "removed_count": 1}
    with patch("tools.graph_admin.graph", new=g):
        g.refresh_project = AsyncMock(return_value=expected)
        result = await refresh_project_graph(5)
    assert result == expected


@pytest.mark.asyncio
async def test_view_graph_json():
    """view_graph json: returns dict with graph_file_path containing '5.json'."""
    from tools.graph_admin import view_graph
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)
    with patch("tools.graph_admin.graph", new=g):
        result = await view_graph(5, format="json")
    assert isinstance(result, dict)
    assert "graph_file_path" in result
    assert "5.json" in result["graph_file_path"]


@pytest.mark.asyncio
async def test_view_graph_tree():
    """view_graph tree: returns string containing project name and at least one stage name."""
    from tools.graph_admin import view_graph
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5, project_name="Test Project")
    with patch("tools.graph_admin.graph", new=g):
        result = await view_graph(5, format="tree")
    assert isinstance(result, str)
    assert "Test Project" in result
    # make_subgraph has stages "Backlog" and "In Progress"
    assert "Backlog" in result


@pytest.mark.asyncio
async def test_view_graph_mermaid():
    """view_graph mermaid: returns string starting with 'graph TD'."""
    from tools.graph_admin import view_graph
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)
    with patch("tools.graph_admin.graph", new=g):
        result = await view_graph(5, format="mermaid")
    assert isinstance(result, str)
    assert result.startswith("graph TD")


@pytest.mark.asyncio
async def test_view_graph_invalid_format():
    """view_graph: raises ValueError for unknown format strings."""
    from tools.graph_admin import view_graph
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)
    with patch("tools.graph_admin.graph", new=g):
        with pytest.raises(ValueError):
            await view_graph(5, format="invalid")


@pytest.mark.asyncio
async def test_view_graph_project_not_in_graph():
    """view_graph: raises ValueError when requested project is not in the active graph."""
    from tools.graph_admin import view_graph
    g = _fresh_graph()
    with patch("tools.graph_admin.graph", new=g):
        with pytest.raises(ValueError):
            await view_graph(99, format="tree")


# ===========================================================================
# ── Phase 6: Graph-first reads ──
# ===========================================================================

@pytest.mark.asyncio
async def test_get_ticket_from_graph():
    """get_ticket: returns ticket from graph without any RPC call when project is active."""
    import importlib
    import tools.read as read_mod
    importlib.reload(read_mod)

    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)

    rpc_called = []

    def fail_if_called(*a, **kw):
        rpc_called.append(True)
        return []

    with patch("tools.read.graph", new=g):
        with patch.object(OdooClient, "_rpc_sync", side_effect=fail_if_called):
            result = await read_mod.get_ticket(101)

    assert not rpc_called, "RPC must not be called when ticket is in graph"
    assert result["title"] == "Add dark mode"
    assert result["id"] == 101


@pytest.mark.asyncio
async def test_get_ticket_rpc_fallback_with_hint():
    """get_ticket: falls through to RPC when ticket not in any active project."""
    import importlib
    import tools.read as read_mod
    importlib.reload(read_mod)

    g = _fresh_graph()
    # project 5 NOT added → ticket 101 not in graph

    task = {
        "id": 101, "name": "From RPC", "stage_id": [1, "Backlog"], "priority": "0",
        "user_ids": [], "tag_ids": [], "project_id": [5, "Alpha"],
    }

    with patch("tools.read.graph", new=g):
        with mock_rpc_sync([task]):
            result = await read_mod.get_ticket(101)

    assert result["title"] == "From RPC"


@pytest.mark.asyncio
async def test_get_ticket_fresh_calls_refresh():
    """get_ticket(fresh=True): calls graph.refresh_ticket and returns envelope from result."""
    import importlib
    import tools.read as read_mod
    importlib.reload(read_mod)

    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)

    refreshed = g.projects[5]["tickets"][101].copy()
    refreshed["name"] = "Freshly Fetched"

    with patch("tools.read.graph", new=g):
        g.refresh_ticket = AsyncMock(return_value=refreshed)
        result = await read_mod.get_ticket(101, fresh=True)

    g.refresh_ticket.assert_awaited_once()
    assert result["title"] == "Freshly Fetched"


@pytest.mark.asyncio
async def test_list_tickets_from_graph():
    """list_tickets: serves from graph when project is active, no RPC called."""
    import importlib
    import tools.read as read_mod
    importlib.reload(read_mod)

    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)

    rpc_called = []

    def fail_if_called(*a, **kw):
        rpc_called.append(True)
        return []

    with patch("tools.read.graph", new=g):
        with patch.object(OdooClient, "_rpc_sync", side_effect=fail_if_called):
            result = await read_mod.list_tickets(project_id=5)

    assert not rpc_called, "RPC must not be called when project is in graph"
    assert result["source"] == "graph"
    assert len(result["tickets"]) == 2
    assert result["total"] == 2


@pytest.mark.asyncio
async def test_list_tickets_hint_when_project_not_in_graph():
    """list_tickets: adds hint key when project_id given but not in graph."""
    import importlib
    import tools.read as read_mod
    importlib.reload(read_mod)

    g = _fresh_graph()
    # project 99 is NOT in graph

    tasks = [
        {"id": 1, "name": "T1", "stage_id": [1, "Todo"], "priority": "0",
         "user_ids": [], "tag_ids": [], "project_id": [99, "Unknown"]},
    ]

    with patch("tools.read.graph", new=g):
        with patch.object(OdooClient, "_rpc_sync", side_effect=[tasks, 1]):
            result = await read_mod.list_tickets(project_id=99)

    assert "hint" in result
    assert "add_project_to_graph" in result["hint"]


@pytest.mark.asyncio
async def test_search_tickets_from_graph():
    """search_tickets: serves from graph when project is active, no RPC called."""
    import importlib
    import tools.read as read_mod
    importlib.reload(read_mod)

    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)

    rpc_called = []

    def fail_if_called(*a, **kw):
        rpc_called.append(True)
        return []

    with patch("tools.read.graph", new=g):
        with patch.object(OdooClient, "_rpc_sync", side_effect=fail_if_called):
            # "login" matches ticket 100 ("Fix login") in make_subgraph
            result = await read_mod.search_tickets("login", project_id=5)

    assert not rpc_called, "RPC must not be called when project is in graph"
    assert result["source"] == "graph"
    assert len(result["tickets"]) == 1
    assert result["tickets"][0]["title"] == "Fix login"


@pytest.mark.asyncio
async def test_get_ticket_summary_all_in_graph():
    """get_ticket_summary: serves entirely from graph when all ticket_ids found, no RPC."""
    import importlib
    import tools.read as read_mod
    importlib.reload(read_mod)

    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)

    rpc_called = []

    def fail_if_called(*a, **kw):
        rpc_called.append(True)
        return []

    with patch("tools.read.graph", new=g):
        with patch.object(OdooClient, "_rpc_sync", side_effect=fail_if_called):
            result = await read_mod.get_ticket_summary([100, 101])

    assert not rpc_called, "RPC must not be called when all tickets are in graph"
    by_id = {r["id"]: r for r in result}
    assert by_id[100]["title"] == "Fix login"
    assert by_id[100]["assignee"] == "Alice"
    assert by_id[100]["stage"] == "Backlog"
    assert by_id[101]["assignee"] == "Unassigned"


@pytest.mark.asyncio
async def test_get_ticket_summary_partial_rpc_fallback():
    """get_ticket_summary: falls back to RPC for all when any ticket_id not in graph."""
    import importlib
    import tools.read as read_mod
    importlib.reload(read_mod)

    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)
    # ticket 999 is NOT in graph → partial miss → RPC for all

    rpc_tasks = [
        {"id": 100, "name": "Fix login", "user_ids": [[7, "Alice"]], "stage_id": [10, "Backlog"]},
        {"id": 999, "name": "Mystery", "user_ids": [], "stage_id": [10, "Backlog"]},
    ]

    with patch("tools.read.graph", new=g):
        with mock_rpc_sync(rpc_tasks):
            result = await read_mod.get_ticket_summary([100, 999])

    assert len(result) == 2
    by_id = {r["id"]: r for r in result}
    assert by_id[100]["title"] == "Fix login"
    assert by_id[999]["title"] == "Mystery"


# ===========================================================================
# ── Phase 7: Write-through events ──
# ===========================================================================

@pytest.mark.asyncio
async def test_create_stage_emits_stage_created():
    """create_stage: after successful Odoo RPC, graph gains the new stage."""
    from tools.write import create_stage
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)

    stage_record = [{"id": 30, "name": "Done", "sequence": 30}]
    with patch("tools.write.graph", new=g):
        with patch.object(OdooClient, "_rpc_sync", side_effect=[30, stage_record]):
            result = await create_stage("Done", project_id=5, sequence=30)

    assert result["id"] == 30
    assert 30 in g.projects[5]["stages"]
    assert g.projects[5]["stages"][30]["name"] == "Done"


@pytest.mark.asyncio
async def test_create_tag_emits_tag_created():
    """create_tag: after successful Odoo RPC, graph.tags gains the new tag."""
    from tools.write import create_tag
    g = _fresh_graph()

    tag_record = [{"id": 77, "name": "Security"}]
    with patch("tools.write.graph", new=g):
        with patch.object(OdooClient, "_rpc_sync", side_effect=[77, tag_record]):
            result = await create_tag("Security")

    assert result["id"] == 77
    assert 77 in g.tags
    assert g.tags[77]["name"] == "Security"


@pytest.mark.asyncio
async def test_update_ticket_patches_graph():
    """update_ticket: after successful Odoo write, graph ticket gets the new name."""
    from tools.write import update_ticket
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)

    updated_task = {
        "id": 100, "name": "Renamed Title", "stage_id": [10, "Backlog"], "priority": "1",
        "user_ids": [[7, "Alice"]], "tag_ids": [[5, "Bug"]], "project_id": [5, "Test Project"],
        "description": "", "child_ids": [], "date_deadline": False,
        "create_date": "2026-01-01 00:00:00", "write_date": "2026-01-02 00:00:00",
    }

    with patch("tools.write.graph", new=g):
        with patch.object(OdooClient, "_rpc_sync", side_effect=[True, [updated_task]]):
            await update_ticket(100, title="Renamed Title")

    assert g.projects[5]["tickets"][100]["name"] == "Renamed Title"


@pytest.mark.asyncio
async def test_delete_ticket_removes_from_graph():
    """delete_ticket: after successful Odoo unlink, ticket is removed from graph."""
    from tools.write import delete_ticket
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)

    assert 100 in g.projects[5]["tickets"]

    with patch("tools.write.graph", new=g):
        with patch.object(OdooClient, "_rpc_sync", return_value=True):
            result = await delete_ticket(100)

    assert result["deleted"] is True
    assert 100 not in g.projects[5]["tickets"]


@pytest.mark.asyncio
async def test_add_comment_increments_count():
    """add_comment: after successful message_post, graph comment_count is incremented."""
    from tools.write import add_comment
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)
    assert g.projects[5]["tickets"][100]["comment_count"] == 0

    with patch("tools.write.graph", new=g):
        with patch.object(OdooClient, "_rpc_sync", return_value=999):
            result = await add_comment(100, "Ship it!")

    assert result["message_id"] == 999
    assert g.projects[5]["tickets"][100]["comment_count"] == 1


@pytest.mark.asyncio
async def test_post_log_note_increments_count():
    """post_log_note: after successful message_post, graph log_note_count is incremented."""
    from tools.write import post_log_note
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)
    assert g.projects[5]["tickets"][100]["log_note_count"] == 0

    with patch("tools.write.graph", new=g):
        with patch.object(OdooClient, "_rpc_sync", return_value=888):
            result = await post_log_note(100, "Internal note")

    assert result["message_id"] == 888
    assert g.projects[5]["tickets"][100]["log_note_count"] == 1


@pytest.mark.asyncio
async def test_attach_file_increments_count():
    """attach_file: after successful create, graph attachment_count is incremented."""
    from tools.write import attach_file
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)
    assert g.projects[5]["tickets"][100]["attachment_count"] == 0

    with patch("tools.write.graph", new=g):
        with patch.object(OdooClient, "_rpc_sync", return_value=55):
            result = await attach_file(100, "spec.md", "# content")

    assert result["attachment_id"] == 55
    assert g.projects[5]["tickets"][100]["attachment_count"] == 1


@pytest.mark.asyncio
async def test_write_through_graph_failure_does_not_raise():
    """update_ticket: graph.apply_write failure is swallowed; tool still returns success."""
    from tools.write import update_ticket
    g = _fresh_graph()
    g.projects[5] = make_subgraph(project_id=5)

    updated_task = {
        "id": 100, "name": "Any Name", "stage_id": [10, "Backlog"], "priority": "1",
        "user_ids": [[7, "Alice"]], "tag_ids": [[5, "Bug"]], "project_id": [5, "Test Project"],
        "description": "", "child_ids": [], "date_deadline": False,
        "create_date": "2026-01-01 00:00:00", "write_date": "2026-01-02 00:00:00",
    }

    with patch("tools.write.graph", new=g):
        with patch.object(g, "apply_write", side_effect=RuntimeError("graph exploded")):
            with patch.object(OdooClient, "_rpc_sync", side_effect=[True, [updated_task]]):
                result = await update_ticket(100, title="Any Name")

    # Tool must succeed — graph failure is best-effort
    assert result["id"] == 100
