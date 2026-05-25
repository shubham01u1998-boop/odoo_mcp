"""
PURPOSE: In-memory graph store for the Odoo MCP server — Phase 1 scaffold + Phase 2 disk persistence.
EXPORTS: graph (singleton Graph), Graph class, strip_html helper
DEPENDS ON: stdlib only (threading, re, html, datetime, json, os, pathlib, time, copy, logging)
PATTERNS: graph.apply_write(event) mutates state under lock; read helpers are lock-free.
          Disk persistence: each project subgraph serialized to .odoo-mcp-graph/projects/<id>.json
"""
from __future__ import annotations

import copy
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Schema version — bump when the on-disk format changes in a breaking way
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# HTML stripping helper (standalone — do NOT import from odoo_client.py)
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    """Strip HTML tags and unescape HTML entities for plain-text search."""
    if not text:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string (matches Odoo write_date format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _extract_ids(items: list[dict]) -> set[int]:
    return {item["id"] for item in items}


# ---------------------------------------------------------------------------
# Graph class
# ---------------------------------------------------------------------------

class Graph:
    """
    In-memory graph of active Odoo projects, tickets, users, and tags.

    State shape
    -----------
    self.projects : dict[int, ProjectSubgraph]
    self.users    : dict[int, {id, name}]
    self.tags     : dict[int, {id, name}]

    ProjectSubgraph shape
    ---------------------
    {
        "project_id":    int,
        "project_name":  str,
        "last_synced_at": float,        # epoch seconds
        "stages":  {stage_id: {id, name, sequence}},
        "tickets": {ticket_id: TicketDict},
        "indexes": {
            "by_stage":    {stage_id: set[ticket_id]},
            "by_assignee": {user_id:  set[ticket_id]},
            "by_tag":      {tag_id:   set[ticket_id]},
            "by_parent":   {parent_id: set[ticket_id]},
        },
    }
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Top-level stores
        self.projects: dict[int, dict] = {}
        self.users: dict[int, dict] = {}
        self.tags: dict[int, dict] = {}
        # Persistence state
        self._gitignore_amended: bool = False
        self._graph_dir: Path | None = None  # cached; resolved lazily by _repo_root()
        # Load any previously persisted subgraphs
        self.load_all_subgraphs()

    # -----------------------------------------------------------------------
    # Private index helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _empty_indexes() -> dict:
        return {
            "by_stage": {},
            "by_assignee": {},
            "by_tag": {},
            "by_parent": {},
        }

    @staticmethod
    def _build_indexes_for_ticket(ticket: dict, indexes: dict) -> None:
        """Add a newly inserted ticket into all four index sets."""
        tid = ticket["id"]

        # by_stage
        stage_id = ticket["stage_id"]["id"] if ticket.get("stage_id") else None
        if stage_id is not None:
            indexes["by_stage"].setdefault(stage_id, set()).add(tid)

        # by_assignee
        for u in ticket.get("user_ids", []):
            indexes["by_assignee"].setdefault(u["id"], set()).add(tid)

        # by_tag
        for t in ticket.get("tag_ids", []):
            indexes["by_tag"].setdefault(t["id"], set()).add(tid)

        # by_parent
        parent_id = ticket.get("parent_id")
        if parent_id is not None:
            indexes["by_parent"].setdefault(parent_id, set()).add(tid)

    @staticmethod
    def _remove_from_indexes(ticket: dict, indexes: dict) -> None:
        """Remove a ticket from all four index sets."""
        tid = ticket["id"]

        stage_id = ticket["stage_id"]["id"] if ticket.get("stage_id") else None
        if stage_id is not None:
            indexes["by_stage"].get(stage_id, set()).discard(tid)

        for u in ticket.get("user_ids", []):
            indexes["by_assignee"].get(u["id"], set()).discard(tid)

        for t in ticket.get("tag_ids", []):
            indexes["by_tag"].get(t["id"], set()).discard(tid)

        parent_id = ticket.get("parent_id")
        if parent_id is not None:
            indexes["by_parent"].get(parent_id, set()).discard(tid)

    def _update_indexes(
        self,
        ticket_id: int,
        old_ticket: dict,
        new_ticket: dict,
        indexes: dict,
        tickets: dict,
    ) -> None:
        """
        Recompute only the index buckets affected by the diff between old and new ticket.
        Also keeps parent ticket's child_ids list in sync when parent_id changes.
        """
        tid = ticket_id

        # --- stage_id ---
        old_stage = old_ticket["stage_id"]["id"] if old_ticket.get("stage_id") else None
        new_stage = new_ticket["stage_id"]["id"] if new_ticket.get("stage_id") else None
        if old_stage != new_stage:
            if old_stage is not None:
                indexes["by_stage"].get(old_stage, set()).discard(tid)
            if new_stage is not None:
                indexes["by_stage"].setdefault(new_stage, set()).add(tid)

        # --- user_ids ---
        old_users = _extract_ids(old_ticket.get("user_ids", []))
        new_users = _extract_ids(new_ticket.get("user_ids", []))
        if old_users != new_users:
            for uid in old_users - new_users:
                indexes["by_assignee"].get(uid, set()).discard(tid)
            for uid in new_users - old_users:
                indexes["by_assignee"].setdefault(uid, set()).add(tid)

        # --- tag_ids ---
        old_tags = _extract_ids(old_ticket.get("tag_ids", []))
        new_tags = _extract_ids(new_ticket.get("tag_ids", []))
        if old_tags != new_tags:
            for tag_id in old_tags - new_tags:
                indexes["by_tag"].get(tag_id, set()).discard(tid)
            for tag_id in new_tags - old_tags:
                indexes["by_tag"].setdefault(tag_id, set()).add(tid)

        # --- parent_id ---
        old_parent = old_ticket.get("parent_id")
        new_parent = new_ticket.get("parent_id")
        if old_parent != new_parent:
            if old_parent is not None:
                indexes["by_parent"].get(old_parent, set()).discard(tid)
                # also remove from parent ticket's child_ids list
                parent_ticket = tickets.get(old_parent)
                if parent_ticket and tid in parent_ticket.get("child_ids", []):
                    parent_ticket["child_ids"] = [
                        c for c in parent_ticket["child_ids"] if c != tid
                    ]
            if new_parent is not None:
                indexes["by_parent"].setdefault(new_parent, set()).add(tid)
                # also append to new parent ticket's child_ids list
                parent_ticket = tickets.get(new_parent)
                if parent_ticket and tid not in parent_ticket.get("child_ids", []):
                    parent_ticket["child_ids"].append(tid)

    # -----------------------------------------------------------------------
    # Persistence — repo root + graph directory
    # -----------------------------------------------------------------------

    def _repo_root(self) -> Path:
        """
        Resolve the root directory for graph storage.

        Priority:
          1. ODOO_GRAPH_DIR env var  → use that path directly
          2. Walk up from cwd looking for .git or pyproject.toml
          3. Fall back to the directory containing this file (graph.py)
        """
        env_dir = os.environ.get("ODOO_GRAPH_DIR")
        if env_dir:
            return Path(env_dir).resolve()

        # Walk up from cwd
        candidate = Path.cwd().resolve()
        while True:
            if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists():
                return candidate
            parent = candidate.parent
            if parent == candidate:
                # Reached filesystem root — fall back to this file's directory
                break
            candidate = parent

        return Path(__file__).parent.resolve()

    @property
    def _graph_directory(self) -> Path:
        """Return (and cache) the .odoo-mcp-graph directory under repo root."""
        if self._graph_dir is None:
            self._graph_dir = self._repo_root() / ".odoo-mcp-graph"
        return self._graph_dir

    # -----------------------------------------------------------------------
    # Persistence — atomic write
    # -----------------------------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        """Write *data* as JSON to *path* atomically (via a .tmp sibling)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.stem + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
        os.replace(tmp, path)

    # -----------------------------------------------------------------------
    # Persistence — .gitignore amendment
    # -----------------------------------------------------------------------

    def _amend_gitignore(self) -> None:
        """Ensure .odoo-mcp-graph/ is in <repo_root>/.gitignore. Idempotent."""
        gitignore_path = self._repo_root() / ".gitignore"
        entry = ".odoo-mcp-graph/"

        if gitignore_path.exists():
            content = gitignore_path.read_text(encoding="utf-8")
        else:
            content = ""

        if entry in content:
            return  # already present — nothing to do

        # Append on a new line (ensure we don't glue to an existing last line)
        separator = "" if (not content or content.endswith("\n")) else "\n"
        gitignore_path.write_text(content + separator + entry + "\n", encoding="utf-8")

    # -----------------------------------------------------------------------
    # Persistence — serialization helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _serialize_subgraph(sub: dict) -> dict:
        """
        Deep-copy *sub* and convert all set values in indexes to sorted lists
        (JSON cannot serialise sets).  Adds ``schema_version`` key.
        """
        data = copy.deepcopy(sub)
        indexes = data.get("indexes", {})
        for index_name, bucket in indexes.items():
            for key, value in bucket.items():
                if isinstance(value, set):
                    bucket[key] = sorted(value)
        data["schema_version"] = SCHEMA_VERSION
        return data

    @staticmethod
    def _deserialize_subgraph(data: dict) -> dict:
        """
        Reverse of ``_serialize_subgraph``: convert index lists back to sets and
        restore integer keys for tickets, stages, and index buckets (JSON serialises
        all dict keys as strings).  Removes the ``schema_version`` metadata key.
        """
        sub = copy.deepcopy(data)
        sub.pop("schema_version", None)

        # Restore integer keys for tickets dict: {"100": {...}} → {100: {...}}
        if "tickets" in sub:
            sub["tickets"] = {int(k): v for k, v in sub["tickets"].items()}

        # Restore integer keys for stages dict
        if "stages" in sub:
            sub["stages"] = {int(k): v for k, v in sub["stages"].items()}

        # Restore integer keys for each index bucket, and convert lists → sets
        indexes = sub.get("indexes", {})
        for index_name, bucket in list(indexes.items()):
            new_bucket: dict = {}
            for key, value in bucket.items():
                int_key = int(key)
                new_bucket[int_key] = set(value) if isinstance(value, list) else value
            indexes[index_name] = new_bucket

        return sub

    # -----------------------------------------------------------------------
    # Persistence — save / load
    # -----------------------------------------------------------------------

    def save_subgraph(self, project_id: int) -> None:
        """Persist a single project subgraph to disk and refresh _meta.json."""
        sub = self.projects.get(project_id)
        if sub is None:
            return  # project was removed before save, skip silently
        projects_dir = self._graph_directory / "projects"
        path = projects_dir / f"{project_id}.json"
        serialized = self._serialize_subgraph(sub)
        self._atomic_write(path, serialized)
        self._save_meta()

        if not self._gitignore_amended:
            self._amend_gitignore()
            self._gitignore_amended = True

    def _save_meta(self) -> None:
        """Write _meta.json (users + tags + schema_version)."""
        meta_path = self._graph_directory / "_meta.json"
        self._atomic_write(meta_path, {
            "users": self.users,
            "tags": self.tags,
            "schema_version": SCHEMA_VERSION,
        })

    def load_all_subgraphs(self) -> None:
        """
        Load all persisted subgraphs from disk into memory.

        Skips (and deletes) files that:
          - have a ``schema_version`` != SCHEMA_VERSION
          - have a ``last_synced_at`` older than 24 h
        Errors on individual files are caught and logged.
        """
        projects_dir = self._graph_directory / "projects"
        if not projects_dir.exists():
            return

        for json_file in projects_dir.glob("*.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))

                # Schema version guard
                if data.get("schema_version") != SCHEMA_VERSION:
                    logger.warning("Deleting %s: schema_version mismatch", json_file)
                    json_file.unlink(missing_ok=True)
                    continue

                # Staleness guard (>24 h)
                last_synced = data.get("last_synced_at", 0)
                if time.time() - last_synced > 86400:
                    logger.warning("Deleting %s: data older than 24 h", json_file)
                    json_file.unlink(missing_ok=True)
                    continue

                subgraph = self._deserialize_subgraph(data)
                project_id = subgraph["project_id"]
                self.projects[project_id] = subgraph

            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to load %s: %s", json_file, exc)

        # Load shared meta (users + tags)
        meta_path = self._graph_directory / "_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                # Keys are strings in JSON — convert back to int
                self.users = {int(k): v for k, v in meta.get("users", {}).items()}
                self.tags = {int(k): v for k, v in meta.get("tags", {}).items()}
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to load _meta.json: %s", exc)

    # -----------------------------------------------------------------------
    # apply_write — main event handler
    # -----------------------------------------------------------------------

    def apply_write(self, event: dict) -> None:
        """
        Handle a single domain event and mutate in-memory state.

        Supported event types:
            ticket_created, ticket_updated, ticket_deleted,
            stage_created, tag_created,
            comment_added, log_note_added,
            attachment_added, attachment_overwritten,
            child_ids_changed
        """
        etype = event.get("type")
        project_id = event.get("project_id")

        # tag_created is the only event that doesn't need an active project
        if etype != "tag_created" and project_id not in self.projects:
            return  # silently drop

        with self._lock:
            # Re-check inside the lock — safe double-check
            if etype != "tag_created" and project_id not in self.projects:
                return

            if etype == "ticket_created":
                self._handle_ticket_created(event)

            elif etype == "ticket_updated":
                self._handle_ticket_updated(event)

            elif etype == "ticket_deleted":
                self._handle_ticket_deleted(event)

            elif etype == "stage_created":
                self._handle_stage_created(event)

            elif etype == "tag_created":
                self._handle_tag_created(event)

            elif etype == "comment_added":
                self._handle_count_bump(event, "comment_count")

            elif etype == "log_note_added":
                self._handle_count_bump(event, "log_note_count")

            elif etype == "attachment_added":
                self._handle_count_bump(event, "attachment_count")

            elif etype == "attachment_overwritten":
                # bump write_date only, no count change
                ticket = self._get_ticket_unsafe(event["project_id"], event["ticket_id"])
                if ticket:
                    ticket["write_date"] = _now_iso()

            elif etype == "child_ids_changed":
                self._handle_child_ids_changed(event)

            # --- Disk persistence ---
            try:
                if etype == "tag_created":
                    # No project subgraph involved — save meta only
                    self._save_meta()
                elif project_id in self.projects:
                    self.save_subgraph(project_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Persistence error after %s: %s", etype, exc)

    # -----------------------------------------------------------------------
    # Individual event handlers (called with lock already held)
    # -----------------------------------------------------------------------

    def _handle_ticket_created(self, event: dict) -> None:
        subgraph = self.projects[event["project_id"]]
        ticket = dict(event["ticket"])  # shallow copy
        # Ensure required count fields exist
        ticket.setdefault("comment_count", 0)
        ticket.setdefault("log_note_count", 0)
        ticket.setdefault("attachment_count", 0)
        ticket.setdefault("stale", False)
        ticket.setdefault("child_ids", [])
        ticket.setdefault("parent_id", None)
        ticket.setdefault("description", "")
        ticket.setdefault("date_deadline", None)
        tid = ticket["id"]
        subgraph["tickets"][tid] = ticket
        self._build_indexes_for_ticket(ticket, subgraph["indexes"])

    def _handle_ticket_updated(self, event: dict) -> None:
        subgraph = self.projects[event["project_id"]]
        tickets = subgraph["tickets"]
        indexes = subgraph["indexes"]
        tid = event["ticket_id"]
        ticket = tickets.get(tid)
        if ticket is None:
            return  # unknown ticket — silently drop
        old_ticket = dict(ticket)  # shallow snapshot for index diffing
        # Patch ticket with the provided fields
        for k, v in event.get("fields", {}).items():
            ticket[k] = v
        ticket["write_date"] = _now_iso()
        self._update_indexes(tid, old_ticket, ticket, indexes, tickets)

    def _handle_ticket_deleted(self, event: dict) -> None:
        subgraph = self.projects[event["project_id"]]
        tickets = subgraph["tickets"]
        indexes = subgraph["indexes"]
        tid = event["ticket_id"]
        ticket = tickets.pop(tid, None)
        if ticket is None:
            return
        self._remove_from_indexes(ticket, indexes)
        # Remove from parent's child_ids
        parent_id = ticket.get("parent_id")
        if parent_id is not None:
            parent_ticket = tickets.get(parent_id)
            if parent_ticket:
                parent_ticket["child_ids"] = [
                    c for c in parent_ticket.get("child_ids", []) if c != tid
                ]

    def _handle_stage_created(self, event: dict) -> None:
        pid = event["project_id"]
        if pid not in self.projects:
            return
        subgraph = self.projects[pid]
        stage = event["stage"]
        subgraph["stages"][stage["id"]] = stage

    def _handle_tag_created(self, event: dict) -> None:
        tag = event["tag"]
        self.tags[tag["id"]] = tag

    def _handle_count_bump(self, event: dict, field: str) -> None:
        ticket = self._get_ticket_unsafe(event["project_id"], event["ticket_id"])
        if ticket:
            ticket[field] = ticket.get(field, 0) + 1
            ticket["write_date"] = _now_iso()

    def _handle_child_ids_changed(self, event: dict) -> None:
        subgraph = self.projects[event["project_id"]]
        parent_id = event["ticket_id"]
        ticket = subgraph["tickets"].get(parent_id)
        if ticket is None:
            return
        new_child_ids: list[int] = list(event["child_ids"])
        old_child_ids: set[int] = set(ticket.get("child_ids", []))
        new_child_ids_set = set(new_child_ids)
        ticket["child_ids"] = new_child_ids
        # Sync by_parent index: add newly added children, remove dropped children
        by_parent = subgraph["indexes"]["by_parent"]
        for cid in new_child_ids_set - old_child_ids:
            by_parent.setdefault(parent_id, set()).add(cid)
        for cid in old_child_ids - new_child_ids_set:
            by_parent.get(parent_id, set()).discard(cid)

    # -----------------------------------------------------------------------
    # Private lock-free ticket accessor (for use inside locked sections)
    # -----------------------------------------------------------------------

    def _get_ticket_unsafe(self, project_id: int, ticket_id: int) -> dict | None:
        subgraph = self.projects.get(project_id)
        if subgraph is None:
            return None
        return subgraph["tickets"].get(ticket_id)

    # -----------------------------------------------------------------------
    # Read helpers — lock-free, pure dict reads
    # -----------------------------------------------------------------------

    def get_ticket(self, project_id: int, ticket_id: int) -> dict | None:
        """Return a ticket dict or None if not found."""
        subgraph = self.projects.get(project_id)
        if subgraph is None:
            return None
        return subgraph["tickets"].get(ticket_id)

    def list_tickets(self, project_id: int, filters: dict | None = None) -> list[dict]:
        """
        Return tickets in a project matching all provided filters.

        filters keys (all optional):
            stage_id    : int   — exact stage match
            assignee_id : int   — ticket has this user in user_ids
            tag_id      : int   — ticket has this tag in tag_ids
            priority    : str   — "0" or "1"
            search      : str   — substring match on name + description
        """
        subgraph = self.projects.get(project_id)
        if subgraph is None:
            return []
        filters = filters or {}
        tickets = subgraph["tickets"]
        indexes = subgraph["indexes"]

        # Start with a candidate set
        candidates: set[int] | None = None

        stage_id = filters.get("stage_id")
        if stage_id is not None:
            s = indexes["by_stage"].get(stage_id, set())
            candidates = set(s) if candidates is None else candidates & s

        assignee_id = filters.get("assignee_id")
        if assignee_id is not None:
            s = indexes["by_assignee"].get(assignee_id, set())
            candidates = set(s) if candidates is None else candidates & s

        tag_id = filters.get("tag_id")
        if tag_id is not None:
            s = indexes["by_tag"].get(tag_id, set())
            candidates = set(s) if candidates is None else candidates & s

        if candidates is None:
            result = list(tickets.values())
        else:
            result = [tickets[tid] for tid in candidates if tid in tickets]

        # Priority filter (not indexed — cheap scan on the subset)
        priority = filters.get("priority")
        if priority is not None:
            result = [t for t in result if t.get("priority") == priority]

        # Full-text substring filter
        search = filters.get("search")
        if search:
            q = search.lower()
            result = [
                t for t in result
                if q in (t.get("name", "") + " " + strip_html(t.get("description", ""))).lower()
            ]

        return result

    def get_summary(self, project_id: int, ticket_ids: list[int]) -> list[dict]:
        """
        Return lightweight summary dicts for the given ticket IDs.

        Each element: {id, title, assignee (first assignee name or "Unassigned"), stage (name)}
        """
        subgraph = self.projects.get(project_id)
        if subgraph is None:
            return []
        tickets = subgraph["tickets"]
        summaries = []
        for tid in ticket_ids:
            t = tickets.get(tid)
            if t is None:
                continue
            user_ids = t.get("user_ids", [])
            assignee = user_ids[0]["name"] if user_ids else "Unassigned"
            stage_info = t.get("stage_id") or {}
            summaries.append({
                "id": tid,
                "title": t.get("name", ""),
                "assignee": assignee,
                "stage": stage_info.get("name", ""),
            })
        return summaries

    def search_tickets(self, project_id: int, query: str) -> list[dict]:
        """
        Local full-text search: query.lower() in (name + ' ' + strip_html(description)).lower()
        """
        subgraph = self.projects.get(project_id)
        if subgraph is None:
            return []
        q = query.lower()
        results = []
        for ticket in subgraph["tickets"].values():
            haystack = (
                ticket.get("name", "") + " " + strip_html(ticket.get("description", ""))
            ).lower()
            if q in haystack:
                results.append(ticket)
        return results

    def list_metadata(self, resource: str, project_id: int | None = None) -> list | None:
        """
        Return metadata for 'projects', 'users', 'tags', or 'stages'.

        Returns None if the requested project is not in the graph.
        """
        if resource == "projects":
            return [
                {"id": pid, "name": sg["project_name"]}
                for pid, sg in self.projects.items()
            ]
        if resource == "users":
            return list(self.users.values())
        if resource == "tags":
            return list(self.tags.values())
        if resource == "stages":
            subgraph = self.projects.get(project_id)
            if subgraph is None:
                return None
            return list(subgraph["stages"].values())
        return None


    # -----------------------------------------------------------------------
    # Hydration — load a project from Odoo via RPC
    # -----------------------------------------------------------------------

    async def hydrate_project(self, project_id: int) -> dict:
        """Hydrate a project from Odoo into the graph. Returns {project_id, name, ticket_count, hydration_ms}."""
        # Import client inside the method body to avoid circular imports
        from odoo_client import client  # noqa: PLC0415

        t0 = time.monotonic()

        # RPC 1 — Project metadata
        project_records = await client._rpc(
            "project.project", "read",
            [[project_id]], {"fields": ["id", "name"]}
        )
        if not project_records:
            raise ValueError(f"Project {project_id} not found in Odoo")
        project_record = project_records[0]

        # RPC 2 — Stages for this project
        stages_raw = await client._rpc(
            "project.task.type", "search_read",
            [[["project_ids", "in", [project_id]]]],
            {"fields": ["id", "name", "sequence"], "order": "sequence"},
        )

        # RPC 3 — Users (global, all active internal users): skip if already populated
        users_raw = await client._rpc(
            "res.users", "search_read",
            [[["active", "=", True], ["share", "=", False]]],
            {"fields": ["id", "name", "login"]},
        ) if not self.users else []

        # RPC 4 — Tags (global): skip if already populated
        tags_raw = await client._rpc(
            "project.tags", "search_read",
            [[]], {"fields": ["id", "name"]},
        ) if not self.tags else []

        # RPC 5 — Tasks for this project (paginate if >500)
        TASK_FIELDS = [
            "id", "name", "stage_id", "project_id", "user_ids", "tag_ids",
            "priority", "parent_id", "child_ids", "description",
            "date_deadline", "create_date", "write_date",
        ]
        tasks = []
        offset = 0
        while True:
            batch = await client._rpc(
                "project.task", "search_read",
                [[["project_id", "=", project_id]]],
                {"fields": TASK_FIELDS, "limit": 500, "offset": offset, "order": "id"},
            )
            tasks.extend(batch)
            if len(batch) < 500:
                break
            offset += 500
        task_ids = [t["id"] for t in tasks]

        # RPC 6 — Attachment counts (grouped by ticket)
        att_groups = await client._rpc(
            "ir.attachment", "read_group",
            [[["res_model", "=", "project.task"], ["res_id", "in", task_ids]]],
            {"fields": ["res_id"], "groupby": ["res_id"], "lazy": False},
        )
        attachment_counts = {g["res_id"]: g.get("__count", g.get("res_id_count", 0)) for g in att_groups}

        # RPC 7 — Message counts (comments vs log notes)
        # Resolve subtype IDs once
        subtypes_raw = await client._rpc(
            "ir.model.data", "search_read",
            [[["model", "=", "mail.message.subtype"], ["module", "=", "mail"],
              ["name", "in", ["mt_comment", "mt_note"]]]],
            {"fields": ["name", "res_id"]},
        )
        subtype_map = {r["name"]: r["res_id"] for r in subtypes_raw}
        mt_comment_id = subtype_map.get("mt_comment")
        mt_note_id = subtype_map.get("mt_note")

        # Comments
        comment_groups = await client._rpc(
            "mail.message", "read_group",
            [[["model", "=", "project.task"], ["res_id", "in", task_ids],
              ["subtype_id", "=", mt_comment_id]]],
            {"fields": ["res_id"], "groupby": ["res_id"], "lazy": False},
        ) if mt_comment_id else []
        comment_counts = {g["res_id"]: g.get("__count", g.get("res_id_count", 0)) for g in comment_groups}

        # Log notes
        note_groups = await client._rpc(
            "mail.message", "read_group",
            [[["model", "=", "project.task"], ["res_id", "in", task_ids],
              ["subtype_id", "=", mt_note_id]]],
            {"fields": ["res_id"], "groupby": ["res_id"], "lazy": False},
        ) if mt_note_id else []
        note_counts = {g["res_id"]: g.get("__count", g.get("res_id_count", 0)) for g in note_groups}

        with self._lock:
            # Populate global caches (only if we fetched them this call)
            if users_raw:
                for u in users_raw:
                    self.users[u["id"]] = {"id": u["id"], "name": u["name"], "login": u.get("login", "")}
            if tags_raw:
                for t in tags_raw:
                    self.tags[t["id"]] = {"id": t["id"], "name": t["name"]}

            # Build stages dict
            stages = {
                s["id"]: {"id": s["id"], "name": s["name"], "sequence": s["sequence"]}
                for s in stages_raw
            }

            # Build tickets dict + indexes
            tickets = {}
            indexes = {
                "by_stage": {}, "by_assignee": {}, "by_tag": {}, "by_parent": {}
            }

            for t in tasks:
                tid = t["id"]

                # Flatten many2one fields inline
                stage_raw = t.get("stage_id")
                stage = {"id": stage_raw[0], "name": stage_raw[1]} if stage_raw and isinstance(stage_raw, list) else None
                proj_raw = t.get("project_id")
                proj = {"id": proj_raw[0], "name": proj_raw[1]} if proj_raw and isinstance(proj_raw, list) else None

                raw_users = t.get("user_ids") or []
                user_ids_flat = [{"id": u, "name": self.users.get(u, {}).get("name", str(u))} for u in raw_users if isinstance(u, int)]

                raw_tags = t.get("tag_ids") or []
                tag_ids_flat = [{"id": tg, "name": self.tags.get(tg, {}).get("name", str(tg))} for tg in raw_tags if isinstance(tg, int)]

                raw_parent = t.get("parent_id")
                parent_id = raw_parent[0] if isinstance(raw_parent, list) and raw_parent else None

                child_ids = [c for c in (t.get("child_ids") or []) if isinstance(c, int)]

                ticket = {
                    "id": tid, "name": t["name"],
                    "stage_id": stage, "project_id": proj,
                    "user_ids": user_ids_flat, "tag_ids": tag_ids_flat,
                    "priority": str(t.get("priority", "0")),
                    "parent_id": parent_id, "child_ids": child_ids,
                    "description": t.get("description") or "",
                    "date_deadline": t.get("date_deadline") or None,
                    "create_date": t.get("create_date") or "",
                    "write_date": t.get("write_date") or "",
                    "attachment_count": attachment_counts.get(tid, 0),
                    "comment_count": comment_counts.get(tid, 0),
                    "log_note_count": note_counts.get(tid, 0),
                    "stale": False,
                }
                tickets[tid] = ticket

                # Build indexes
                if stage:
                    indexes["by_stage"].setdefault(stage["id"], set()).add(tid)
                for u in user_ids_flat:
                    indexes["by_assignee"].setdefault(u["id"], set()).add(tid)
                for tg in tag_ids_flat:
                    indexes["by_tag"].setdefault(tg["id"], set()).add(tid)
                if parent_id:
                    indexes["by_parent"].setdefault(parent_id, set()).add(tid)

            subgraph = {
                "project_id": project_id,
                "project_name": project_record["name"],
                "last_synced_at": time.time(),
                "stages": stages,
                "tickets": tickets,
                "indexes": indexes,
            }
            self.projects[project_id] = subgraph
            self.save_subgraph(project_id)

        hydration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "project_id": project_id,
            "name": project_record["name"],
            "ticket_count": len(tickets),
            "hydration_ms": hydration_ms,
        }


    # -----------------------------------------------------------------------
    # Refresh — single ticket and full project delta sync
    # -----------------------------------------------------------------------

    async def refresh_ticket(self, ticket_id: int, model: str = "project.task") -> dict:
        """
        Refresh a single ticket from Odoo. Updates graph state if the project is active.
        Returns the new graph-shaped ticket dict.
        """
        from odoo_client import client  # noqa: PLC0415

        TASK_FIELDS = [
            "id", "name", "stage_id", "project_id", "user_ids", "tag_ids",
            "priority", "parent_id", "child_ids", "description",
            "date_deadline", "create_date", "write_date",
        ]

        # RPC 1 — fetch ticket from Odoo
        records = await client._rpc(
            model, "search_read",
            [[["id", "=", ticket_id]]],
            {"fields": TASK_FIELDS, "limit": 1},
        )
        if not records:
            raise ValueError(f"Ticket {ticket_id} not found in Odoo")
        record = records[0]

        # RPC 2 — attachment count for this ticket
        att_groups = await client._rpc(
            "ir.attachment", "read_group",
            [[["res_model", "=", model], ["res_id", "in", [ticket_id]]]],
            {"fields": ["res_id"], "groupby": ["res_id"], "lazy": False},
        )
        attachment_counts = {g["res_id"]: g.get("__count", g.get("res_id_count", 0)) for g in att_groups}

        # RPC 3 — message counts (comments vs log notes)
        subtypes_raw = await client._rpc(
            "ir.model.data", "search_read",
            [[["model", "=", "mail.message.subtype"], ["module", "=", "mail"],
              ["name", "in", ["mt_comment", "mt_note"]]]],
            {"fields": ["name", "res_id"]},
        )
        subtype_map = {r["name"]: r["res_id"] for r in subtypes_raw}
        mt_comment_id = subtype_map.get("mt_comment")
        mt_note_id = subtype_map.get("mt_note")

        comment_groups = await client._rpc(
            "mail.message", "read_group",
            [[["model", "=", model], ["res_id", "in", [ticket_id]],
              ["subtype_id", "=", mt_comment_id]]],
            {"fields": ["res_id"], "groupby": ["res_id"], "lazy": False},
        ) if mt_comment_id else []
        comment_counts = {g["res_id"]: g.get("__count", g.get("res_id_count", 0)) for g in comment_groups}

        note_groups = await client._rpc(
            "mail.message", "read_group",
            [[["model", "=", model], ["res_id", "in", [ticket_id]],
              ["subtype_id", "=", mt_note_id]]],
            {"fields": ["res_id"], "groupby": ["res_id"], "lazy": False},
        ) if mt_note_id else []
        note_counts = {g["res_id"]: g.get("__count", g.get("res_id_count", 0)) for g in note_groups}

        # Build graph-shaped ticket dict
        tid = record["id"]

        stage_raw = record.get("stage_id")
        stage = {"id": stage_raw[0], "name": stage_raw[1]} if stage_raw and isinstance(stage_raw, list) else None

        proj_raw = record.get("project_id")
        proj = {"id": proj_raw[0], "name": proj_raw[1]} if proj_raw and isinstance(proj_raw, list) else None

        raw_users = record.get("user_ids") or []
        user_ids_flat = [{"id": u, "name": self.users.get(u, {}).get("name", str(u))} for u in raw_users if isinstance(u, int)]

        raw_tags = record.get("tag_ids") or []
        tag_ids_flat = [{"id": tg, "name": self.tags.get(tg, {}).get("name", str(tg))} for tg in raw_tags if isinstance(tg, int)]

        raw_parent = record.get("parent_id")
        parent_id = raw_parent[0] if isinstance(raw_parent, list) and raw_parent else None

        child_ids = [c for c in (record.get("child_ids") or []) if isinstance(c, int)]

        new_ticket = {
            "id": tid, "name": record["name"],
            "stage_id": stage, "project_id": proj,
            "user_ids": user_ids_flat, "tag_ids": tag_ids_flat,
            "priority": str(record.get("priority", "0")),
            "parent_id": parent_id, "child_ids": child_ids,
            "description": record.get("description") or "",
            "date_deadline": record.get("date_deadline") or None,
            "create_date": record.get("create_date") or "",
            "write_date": record.get("write_date") or "",
            "attachment_count": attachment_counts.get(tid, 0),
            "comment_count": comment_counts.get(tid, 0),
            "log_note_count": note_counts.get(tid, 0),
            "stale": False,
        }

        # Determine which project this ticket belongs to
        # Use project_id from Odoo response first, then scan graph as fallback
        odoo_project_id = proj["id"] if proj else None

        with self._lock:
            # Scan for the ticket's current location in the graph (regardless of Odoo project)
            current_pid: int | None = None
            for pid, sub in self.projects.items():
                if tid in sub["tickets"]:
                    current_pid = pid
                    break

            # Determine destination: prefer Odoo's reported project if it's active
            if odoo_project_id and odoo_project_id in self.projects:
                dest_pid = odoo_project_id
            else:
                dest_pid = current_pid  # stay in current active project (if any)

            # If ticket was in a different active project, remove the stale copy first
            if current_pid is not None and current_pid != dest_pid:
                old_sub = self.projects[current_pid]
                old_ticket = old_sub["tickets"].pop(tid, None)
                if old_ticket:
                    self._remove_from_indexes(old_ticket, old_sub["indexes"])
                try:
                    self.save_subgraph(current_pid)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Persistence error after refresh_ticket (old project): %s", exc)

            if dest_pid is not None:
                dest_sub = self.projects[dest_pid]
                dest_tickets = dest_sub["tickets"]
                dest_indexes = dest_sub["indexes"]
                if tid in dest_tickets:
                    old_ticket = dest_tickets[tid]
                    dest_tickets[tid] = new_ticket
                    self._update_indexes(tid, old_ticket, new_ticket, dest_indexes, dest_tickets)
                else:
                    dest_tickets[tid] = new_ticket
                    self._build_indexes_for_ticket(new_ticket, dest_indexes)
                try:
                    self.save_subgraph(dest_pid)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Persistence error after refresh_ticket: %s", exc)

        return new_ticket

    async def refresh_project(self, project_id: int) -> dict:
        """
        Delta sync for a project already in the active graph.
        Fetches all tickets changed since last_synced_at, applies updates,
        detects deleted tickets, and updates last_synced_at.
        Returns {project_id, updated_count, removed_count}.
        """
        from odoo_client import client  # noqa: PLC0415

        # Read last_synced_at under lock (brief, just a dict read)
        with self._lock:
            if project_id not in self.projects:
                raise ValueError(
                    f"Project {project_id} not in active graph; call add_project_to_graph first"
                )
            last_synced_at = self.projects[project_id]["last_synced_at"]

        # Format for Odoo comparison (outside lock — pure computation)
        last_synced_str = datetime.fromtimestamp(last_synced_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        TASK_FIELDS = [
            "id", "name", "stage_id", "project_id", "user_ids", "tag_ids",
            "priority", "parent_id", "child_ids", "description",
            "date_deadline", "create_date", "write_date",
        ]

        # RPC — fetch changed tickets since last sync
        changed_tasks = await client._rpc(
            "project.task", "search_read",
            [[["project_id", "=", project_id], ["write_date", ">", last_synced_str]]],
            {"fields": TASK_FIELDS, "limit": 0},
        )

        # Collect all new_ticket dicts first (outside any lock — pure computation)
        new_tickets_by_id = {}
        for t in changed_tasks:
            tid = t["id"]

            stage_raw = t.get("stage_id")
            stage = {"id": stage_raw[0], "name": stage_raw[1]} if stage_raw and isinstance(stage_raw, list) else None

            proj_raw = t.get("project_id")
            proj = {"id": proj_raw[0], "name": proj_raw[1]} if proj_raw and isinstance(proj_raw, list) else None

            raw_users = t.get("user_ids") or []
            user_ids_flat = [{"id": u, "name": self.users.get(u, {}).get("name", str(u))} for u in raw_users if isinstance(u, int)]

            raw_tags = t.get("tag_ids") or []
            tag_ids_flat = [{"id": tg, "name": self.tags.get(tg, {}).get("name", str(tg))} for tg in raw_tags if isinstance(tg, int)]

            raw_parent = t.get("parent_id")
            parent_id = raw_parent[0] if isinstance(raw_parent, list) and raw_parent else None

            child_ids = [c for c in (t.get("child_ids") or []) if isinstance(c, int)]

            new_ticket = {
                "id": tid, "name": t["name"],
                "stage_id": stage, "project_id": proj,
                "user_ids": user_ids_flat, "tag_ids": tag_ids_flat,
                "priority": str(t.get("priority", "0")),
                "parent_id": parent_id, "child_ids": child_ids,
                "description": t.get("description") or "",
                "date_deadline": t.get("date_deadline") or None,
                "create_date": t.get("create_date") or "",
                "write_date": t.get("write_date") or "",
                "attachment_count": 0,
                "comment_count": 0,
                "log_note_count": 0,
                "stale": False,
            }
            new_tickets_by_id[tid] = new_ticket

        # Deletion detection: get all current ticket IDs from Odoo (RPC must be outside lock)
        current_ids_raw = await client._rpc(
            "project.task", "search",
            [[["project_id", "=", project_id]]],
        )
        current_ids = set(current_ids_raw)

        # Single lock block for ALL mutations
        with self._lock:
            if project_id not in self.projects:
                # project was removed while we were fetching — abort silently
                return {"project_id": project_id, "updated_count": 0, "removed_count": 0}
            subgraph = self.projects[project_id]
            tickets = subgraph["tickets"]
            indexes = subgraph["indexes"]

            for tid, new_ticket in new_tickets_by_id.items():
                if tid in tickets:
                    old_ticket = tickets[tid]
                    # Preserve existing counts (not returned by search_read)
                    new_ticket["attachment_count"] = old_ticket.get("attachment_count", 0)
                    new_ticket["comment_count"] = old_ticket.get("comment_count", 0)
                    new_ticket["log_note_count"] = old_ticket.get("log_note_count", 0)
                    tickets[tid] = new_ticket
                    # Update all relational indexes (stage, users, tags, parent)
                    self._update_indexes(tid, old_ticket, new_ticket, indexes, tickets)
                    # Also reconcile by_parent[tid] when child_ids changes
                    old_child_set = set(old_ticket.get("child_ids", []))
                    new_child_set = set(new_ticket.get("child_ids", []))
                    if old_child_set != new_child_set:
                        by_parent = indexes["by_parent"]
                        for cid in new_child_set - old_child_set:
                            by_parent.setdefault(tid, set()).add(cid)
                        for cid in old_child_set - new_child_set:
                            by_parent.get(tid, set()).discard(cid)
                else:
                    # New ticket — init counts to 0 (delta sync doesn't fetch counts for new tickets)
                    tickets[tid] = new_ticket
                    self._build_indexes_for_ticket(new_ticket, indexes)

            # Deletion detection: compare graph IDs against Odoo current IDs
            graph_ids = set(tickets.keys())
            removed_ids = graph_ids - current_ids
            removed_count = len(removed_ids)
            for tid in removed_ids:
                ticket = tickets.pop(tid, None)
                if ticket:
                    self._remove_from_indexes(ticket, indexes)
                    parent_id = ticket.get("parent_id")
                    if parent_id is not None:
                        parent_ticket = tickets.get(parent_id)
                        if parent_ticket:
                            parent_ticket["child_ids"] = [c for c in parent_ticket.get("child_ids", []) if c != tid]

            subgraph["last_synced_at"] = time.time()
            try:
                self.save_subgraph(project_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Persistence error after refresh_project: %s", exc)

        return {
            "project_id": project_id,
            "updated_count": len(changed_tasks),
            "removed_count": removed_count,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

graph = Graph()
