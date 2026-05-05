# Odoo MCP Server

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that connects **Claude Code / Claude Desktop** directly to **Odoo 19 Enterprise**. Lets any developer or QA engineer read tickets, create projects, manage stages, assign tasks, and scaffold entire sprints — all through natural language inside the IDE, with no Odoo UI required.

---

## What It Does

| Category | Capability |
|----------|-----------|
| **Read** | Fetch single tickets, list with filters, full-text search, one-liner summaries |
| **Write** | Create projects (with auto-stages), stages, tags, tickets, subtasks |
| **Bulk** | Scaffold a full sprint board in one call (bulk stages + bulk tickets) |
| **Update** | Patch any ticket field, move tickets between stages by name |
| **Metadata** | List all projects, stages, users, and tags from Odoo |

---

## Available Tools (14)

### Read Tools

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `get_ticket` | Fetch a single task by ID | `ticket_id`, `detail` (bool — includes description, subtask count, dates) |
| `list_tickets` | List tasks with optional filters | `project_id`, `stage`, `tag`, `assigned_to`, `priority`, `limit`, `offset` |
| `get_ticket_summary` | One-liner summary per ticket (most token-efficient) | `ticket_ids` (list of up to 100 IDs) |
| `search_tickets` | Full-text search across title + description | `query`, `project_id`, `limit` |

### Write Tools

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `create_project` | Create a project, auto-creates stages if none supplied | `name`, `description`, `stages` (list of stage names) |
| `create_stage` | Create a single Kanban stage and assign it to a project | `name`, `project_id`, `sequence` |
| `create_tag` | Create a new task tag | `name` |
| `create_ticket` | Create a single task with full metadata | `title`, `project_id`, `description` (markdown auto-converted to HTML), `stage_id`, `assignee_ids`, `tag_ids`, `priority`, `deadline`, `subtasks` |
| `bulk_create_stages` | Create multiple stages in one call | `stages` (list of `{name, sequence?}`), `project_id` |
| `bulk_create_tickets` | Create multiple tickets in one call | `tickets` (list of ticket dicts), `project_id` |
| `update_ticket` | Patch-update any ticket field | `ticket_id`, `title`, `description`, `stage_id`, `assignee_ids`, `priority`, `deadline` |
| `transition_stage` | Move a ticket to a stage by name | `ticket_id`, `stage_name` |
| `add_subtasks` | Add child tasks to an existing ticket | `ticket_id`, `subtasks` (list of strings) |

### Utility Tools

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `list_metadata` | List projects, stages, users, or tags | `resource` (`projects` \| `stages` \| `users` \| `tags`), `project_id` (filters stages) |

---

## Prerequisites

- Python 3.11+
- An Odoo instance (tested on Odoo 19 Enterprise)
- An Odoo API key (Settings → Technical → API Keys)
- Claude Code (VSCode/Windsurf extension) **or** Claude Desktop

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/shubham01u1998-boop/odoo_mcp.git
cd odoo_mcp
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure credentials

Copy the example env file and fill in your Odoo details:

```bash
cp .env.example .env
```

Edit `.env`:

```
ODOO_URL=https://your-instance.odoo.com
ODOO_DB=your-database-name
ODOO_USERNAME=your@email.com
ODOO_API_KEY=your_api_key_here
```

> **How to get an Odoo API key:** Log in to Odoo → Settings → Technical → API Keys → New

---

## Connecting to Claude Code (VSCode / Windsurf)

### Step 1 — Create `.mcp.json` in your workspace root

```json
{
  "mcpServers": {
    "odoo": {
      "command": "odoo_mcp\\venv\\Scripts\\python.exe",
      "args": ["odoo_mcp\\server.py"]
    }
  }
}
```

> Adjust paths if you cloned the repo to a different location or are on macOS/Linux:
> ```json
> "command": "odoo_mcp/venv/bin/python",
> "args": ["odoo_mcp/server.py"]
> ```

### Step 2 — Allow the MCP server in Claude Code settings

Open `.claude/settings.json` (create if it doesn't exist) and add:

```json
{
  "enableAllProjectMcpServers": true
}
```

### Step 3 — Reload the window

`Ctrl+Shift+P` → **Developer: Reload Window**

The MCP server will start automatically and the 14 tools will be available in your Claude session.

---

## Connecting to Claude Desktop

Add the following to your Claude Desktop config file:

**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "odoo": {
      "command": "C:/path/to/odoo_mcp/venv/Scripts/python.exe",
      "args": ["C:/path/to/odoo_mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop after saving.

---

## Usage Examples

Once connected, you can talk to Claude naturally:

### List your projects
```
list my odoo projects
```

### Create a full project with stages
```
Create a project called "Mobile App" with stages: Backlog, In Progress, Review, Done
```

### Scaffold an entire sprint
```
Create these tickets in project 42, stage "To Do":
- User authentication (Backend, assign to kunal@company.com)
- Login screen UI (Frontend, assign to sahil@company.com)
- Profile API (Backend)
```

### Search tickets
```
Search for tickets mentioning "payment" in the TiffinConnect project
```

### Move a ticket
```
Move ticket 2315 to the "In Progress" stage
```

### Add subtasks
```
Add subtasks to ticket 2315: "Write unit tests", "Code review", "Deploy to staging"
```

### Bulk create tickets with subtasks
```json
bulk_create_tickets([
  {
    "title": "Authentication System",
    "stage_id": 348,
    "tag_ids": [44],
    "assignee_ids": [41],
    "subtasks": ["Firebase setup", "JWT middleware", "Token refresh"]
  }
], project_id=58)
```

---

## Token Optimization

This server is designed to minimize token consumption per operation:

| Approach | Calls | Est. Tokens |
|----------|-------|-------------|
| Naive (26 tickets individually) | ~39 calls | ~35,000 |
| Optimized (bulk + list_metadata) | ~10 calls | ~8,600 |
| **Saving** | **29 fewer calls** | **~75% reduction** |

Key optimizations built in:
- `list_metadata` replaces 4 separate list tools
- `create_project` auto-creates stages in one call
- `bulk_create_tickets` replaces N individual create calls
- Slim `create_ticket` response (no redundant second RPC)
- In-memory cache with TTL for metadata (10 min) and ticket reads (1 min)

---

## Running Tests

```bash
venv/Scripts/python.exe -m pytest tests/ -v
# 38 tests, all should pass
```

---

## Project Structure

```
odoo_mcp/
├── server.py              # FastMCP server — registers all 14 tools
├── odoo_client.py         # XML-RPC client, md→HTML conversion, helpers
├── cache.py               # In-memory TTL cache
├── requirements.txt       # Dependencies
├── .env.example           # Credential template (copy to .env)
├── migrate_descriptions.py # One-off script: re-renders existing ticket descriptions as HTML
├── tools/
│   ├── read.py            # get_ticket, list_tickets, get_ticket_summary, search_tickets
│   ├── write.py           # create_*, bulk_*, update_ticket, transition_stage, add_subtasks
│   └── utils.py           # list_metadata
└── tests/
    └── test_mcp.py        # 38 unit tests (mocked XML-RPC)
```

---

## Security Notes

- **Never commit `.env`** — it is excluded by `.gitignore`
- API keys are read from environment variables only — no hardcoded credentials in source
- Use `.env.example` as the template for onboarding new developers

---

## Requirements

```
mcp>=1.0.0
python-dotenv>=1.0.0
markdown>=3.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```
