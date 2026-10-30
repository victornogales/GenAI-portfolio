"""MCP server for the job pipeline: tools, resources and prompts.

So far it has the four read-only tools (risk tier 0). Every tool is a thin
wrapper over a store.py function; the server itself holds no logic.

The database is data_private/pipeline.db, or the file named by the
PIPELINE_DB environment variable (e.g. data_public/pipeline_demo.db).

Run from the agentic-pm-lab folder:

    uv run mcp dev build1_mcp_server/server.py      # with the MCP Inspector
    uv run python -m build1_mcp_server.server       # plain stdio server
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

# `mcp dev` loads this file by path rather than as part of the package.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build1_mcp_server import store  # noqa: E402

DB_PATH = Path(os.environ.get("PIPELINE_DB", store.DEFAULT_DB))

Stage = Literal["saved", "applied", "talking", "interviewing", "offer", "closed"]

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

mcp = MCPServer(
    name="job-pipeline",
    instructions=(
        "Victor's job-search pipeline: roles he is tracking, their stage, next steps, notes and fit "
        "assessments. Start with pipeline_summary for an overview, use list_roles or search_roles to "
        "find roles, and get_role for one role's full record. Role ids look like 'telnyx-devex-plg'."
    ),
)


@contextmanager
def _db():
    """Open the database. Expected failures become ToolError, so the model sees their message;
    any other exception is treated by the SDK as a crash and its text is withheld."""
    if not DB_PATH.exists():
        raise ToolError(f"No pipeline database at {DB_PATH}. Run import_pipeline.py first or set PIPELINE_DB.")
    try:
        with store.connect(DB_PATH) as conn:
            yield conn
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
def pipeline_summary(
    stale_days: Annotated[int, Field(ge=1, le=365, description="Days without activity before a role counts as stale.")] = 14,
) -> dict:
    """Overview of the whole pipeline: how many roles are in each stage, which are flagged, which have
    gone stale (open roles with no activity for stale_days), and which next actions are due or overdue.
    Use it first for questions like "what needs my attention?" or "how is my search going?"."""
    with _db() as conn:
        return store.pipeline_summary(conn, stale_days=stale_days)


@mcp.tool(annotations=READ_ONLY)
def list_roles(
    stage: Annotated[Stage | None, Field(description="Only roles in this stage. Omit for all stages.")] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum number of roles to return.")] = 20,
) -> list[dict]:
    """List roles in the pipeline, flagged ones first, then the most recently active. Each role comes
    with its stage, next action and latest fit verdict and score. Use it to browse by stage, e.g. all
    roles where Victor is 'talking'. To find roles by company or keyword, use search_roles instead.
    Archived roles are not included."""
    with _db() as conn:
        return store.list_roles(conn, stage=stage, limit=limit)


@mcp.tool(annotations=READ_ONLY)
def search_roles(
    query: Annotated[str, Field(min_length=1, description="Words to look for, e.g. 'telnyx' or 'platform integrations'.")],
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum number of roles to return.")] = 20,
) -> list[dict]:
    """Find roles where every word of the query appears in the company, job title, next action or a
    note (case-insensitive). Use it when you know a company name or keyword. To browse by stage, use
    list_roles instead. Archived roles are not included."""
    with _db() as conn:
        return store.search_roles(conn, query, limit=limit)


@mcp.tool(annotations=READ_ONLY)
def get_role(
    role_id: Annotated[str, Field(description="The role's id, as returned by list_roles or search_roles.")],
) -> dict:
    """Everything about one role: its details and links, full stage history, notes and every fit
    assessment (newest first, with matches and gaps). Use it after list_roles or search_roles when
    you need the detail, e.g. to prepare for an interview. Works for archived roles too."""
    with _db() as conn:
        role = store.get_role(conn, role_id)
    if role is None:
        raise ToolError(f"No role with id {role_id!r}. Use search_roles to find the right id.")
    return role


if __name__ == "__main__":
    mcp.run()
