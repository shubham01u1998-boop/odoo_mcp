import sys
import os

# Ensure the package root is on the path when launched by Claude Desktop
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

from mcp.server.fastmcp import FastMCP

from tools.read import get_ticket, get_ticket_summary, list_tickets, search_tickets
from tools.write import (
    create_project, create_stage, create_tag, create_ticket,
    bulk_create_stages, bulk_create_tickets,
    update_ticket, transition_stage, add_subtasks,
)
from tools.utils import list_metadata

mcp = FastMCP("odoo-mcp")

for _fn in [
    get_ticket,
    list_tickets,
    get_ticket_summary,
    search_tickets,
    create_project,
    create_stage,
    create_tag,
    create_ticket,
    bulk_create_stages,
    bulk_create_tickets,
    update_ticket,
    transition_stage,
    add_subtasks,
    list_metadata,
]:
    mcp.tool()(_fn)

if __name__ == "__main__":
    mcp.run()
