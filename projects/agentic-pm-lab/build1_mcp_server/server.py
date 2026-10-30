"""MCP server for the job pipeline: tools, resources and prompts.

Every tool is a thin wrapper over a store.py function; the server itself
holds no logic. Each tool's annotations follow the risk tiers in the README:
tier 0 reads, tier 1 safe writes that can be undone, tier 2 archive_role.

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
Track = Literal["ai", "integrations", "general", "solutions"]
Channel = Literal["Direct", "Recruiter", "Inbound", "Referral"]
RoleId = Annotated[str, Field(description="The role's id, as returned by list_roles or search_roles.")]

# Risk tiers (see README). Tier 0: changes nothing.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# Tier 1: changes data, can be undone. "Repeatable" tools give the same result if called twice.
SAFE_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
SAFE_WRITE_REPEATABLE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
# Tier 2: hides data. Reversible with restore_role, but clients should always confirm it.
RISKY = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

mcp = MCPServer(
    name="job-pipeline",
    instructions=(
        "Victor's job-search pipeline: roles he is tracking, their stage, next steps, notes and fit "
        "assessments. Start with pipeline_summary for an overview, use list_roles or search_roles to "
        "find roles, and get_role for one role's full record. Role ids look like 'telnyx-devex-plg'. "
        "Change the pipeline only when Victor asks you to, and look the role up first so you change "
        "the right one. Every change returns the role's updated record; confirm what changed."
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
    include_archived: Annotated[bool, Field(description="Also list archived roles (archived_at is set).")] = False,
) -> list[dict]:
    """List roles in the pipeline, flagged ones first, then the most recently active. Each role comes
    with its stage, next action and latest fit verdict and score. Use it to browse by stage, e.g. all
    roles where Victor is 'talking'. To find roles by company or keyword, use search_roles instead.
    Archived roles are left out unless include_archived is true."""
    with _db() as conn:
        return store.list_roles(conn, stage=stage, limit=limit, include_archived=include_archived)


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
def get_role(role_id: RoleId) -> dict:
    """Everything about one role: its details and links, full stage history, notes and every fit
    assessment (newest first, with matches and gaps). Use it after list_roles or search_roles when
    you need the detail, e.g. to prepare for an interview. Works for archived roles too."""
    with _db() as conn:
        role = store.get_role(conn, role_id)
    if role is None:
        raise ToolError(f"No role with id {role_id!r}. Use search_roles to find the right id.")
    return role


@mcp.tool(annotations=SAFE_WRITE)
def add_role(
    company: Annotated[str, Field(min_length=1, description="Hiring company, e.g. 'Telnyx'.")],
    title: Annotated[str, Field(min_length=1, description="Job title as posted.")],
    stage: Annotated[Stage, Field(description="Starting stage. Usually 'saved' for a role just found.")] = "saved",
    track: Annotated[Track | None, Field(description="Which kind of role this is.")] = None,
    channel: Annotated[Channel | None, Field(description="How the role came in.")] = None,
    job_url: Annotated[str | None, Field(description="Link to the job post (https://...).")] = None,
    spec_url: Annotated[str | None, Field(description="Link to the saved job spec (https://...).")] = None,
    next_action: Annotated[str | None, Field(description="The first next step, e.g. 'Tailor CV'.")] = None,
) -> dict:
    """Add a new role to the pipeline and return its record, including the id it was given. Search
    first with search_roles: if the role is already there, update it instead of adding it again (a
    role with the same company and title is rejected)."""
    with _db() as conn:
        return store.add_role(
            conn, company, title, stage=stage, track=track, channel=channel,
            job_url=job_url, spec_url=spec_url, next_action=next_action,
        )


@mcp.tool(annotations=SAFE_WRITE_REPEATABLE)
def update_stage(
    role_id: RoleId,
    stage: Annotated[Stage, Field(description="The new stage.")],
    note: Annotated[str | None, Field(description="Why it moved, e.g. 'Recruiter call booked'.")] = None,
) -> dict:
    """Move a role to a new stage, e.g. from 'applied' to 'talking', and record the change in its stage
    history. Use 'closed' when the role is over (rejected, withdrawn or filled). Moving a role to the
    stage it is already in changes nothing."""
    with _db() as conn:
        return store.update_stage(conn, role_id, stage, note=note)


@mcp.tool(annotations=SAFE_WRITE)
def add_note(
    role_id: RoleId,
    text: Annotated[str, Field(min_length=1, max_length=store.MAX_NOTE_LENGTH, description="The note.")],
) -> dict:
    """Add a dated note to a role, e.g. what was said on a call or who the hiring manager is. Use it for
    anything worth remembering that isn't the next step (use set_next_action for that)."""
    with _db() as conn:
        return store.add_note(conn, role_id, text)


@mcp.tool(annotations=SAFE_WRITE_REPEATABLE)
def set_next_action(
    role_id: RoleId,
    action: Annotated[str, Field(description="The next step, e.g. 'Send portfolio'. Empty clears it.")],
    due_date: Annotated[str | None, Field(description="When it's due, as YYYY-MM-DD.")] = None,
) -> dict:
    """Set a role's next step and, optionally, its due date. This replaces the previous next step. Roles
    whose due date has passed show as overdue in pipeline_summary. An empty action clears the next step
    and its due date."""
    with _db() as conn:
        return store.set_next_action(conn, role_id, action, due_date=due_date)


@mcp.tool(annotations=RISKY)
def archive_role(
    role_id: RoleId,
    reason: Annotated[str, Field(min_length=1, description="Why it's being archived, e.g. 'Duplicate entry'.")],
) -> dict:
    """Hide a role from list_roles, search_roles and pipeline_summary. Nothing is deleted and
    restore_role brings it back, but only archive when Victor asks to: a role that has simply ended
    should usually move to the 'closed' stage with update_stage instead."""
    with _db() as conn:
        return store.archive_role(conn, role_id, reason)


@mcp.tool(annotations=SAFE_WRITE)
def restore_role(role_id: RoleId) -> dict:
    """Bring back a role hidden by archive_role, exactly as it was. To find an archived role's id, use
    list_roles with include_archived set to true."""
    with _db() as conn:
        return store.restore_role(conn, role_id)


if __name__ == "__main__":
    mcp.run()
