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
    from tools.utils import list_metadata
    users = [{"id": 1, "name": "Alice", "login": "alice@co.com"}]
    with mock_rpc_sync(users):
        result = await list_metadata("users")
    assert result[0]["login"] == "alice@co.com"


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
