"""MCP server for the job pipeline: tools, resources and prompts.

Every tool, resource and prompt is a thin wrapper over a store.py function;
the server itself holds no logic. Each tool's annotations follow the risk
tiers in the README: tier 0 reads, tier 1 safe writes that can be undone,
tier 2 archive_role. Resources and prompts only read.

The database is data_private/pipeline.db, or the file named by the
PIPELINE_DB environment variable (e.g. data_public/pipeline_demo.db).

Run from the agentic-pm-lab folder:

    uv run mcp dev build1_mcp_server/server.py      # with the MCP Inspector
    uv run python -m build1_mcp_server.server       # plain stdio server
"""

import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, ToolAnnotations
from pydantic import BaseModel, Field

# `mcp dev` loads this file by path rather than as part of the package.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build0_spec_qualifier.tools import CVTools  # noqa: E402
from build0_spec_qualifier.tools import ToolError as CVToolError  # noqa: E402
from build1_mcp_server import store  # noqa: E402

DB_PATH = Path(os.environ.get("PIPELINE_DB", store.DEFAULT_DB))
CV_PATH = Path(os.environ.get("CV_PATH", store.PROJECT_DIR / "data_private" / "vnogales-cv-master.md"))

# The assessment CV has a "Right to Work" section for fit assessments only. It is
# kept out of everything this server shows, so it can't find its way into drafts.
_PRIVATE_SECTION = re.compile(r"^## Right to Work\s*$.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)

Stage = Literal["saved", "applied", "talking", "interviewing", "offer", "closed"]
Track = Literal["ai", "integrations", "general", "solutions"]
Channel = Literal["Direct", "Recruiter", "Inbound", "Referral"]
Verdict = Literal["strong", "good", "stretch", "weak"]
RoleId = Annotated[str, Field(description="The role's id, as returned by list_roles or search_roles.")]


class FitItem(BaseModel):
    title: str = Field(min_length=1, description="Short label, e.g. 'Developer platforms'.")
    detail: str = Field(min_length=1, description="The evidence or the gap, in one or two sentences.")

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


def _prompt_error(message: str) -> MCPError:
    return MCPError(INVALID_PARAMS, message)


@contextmanager
def _db(error: Callable[[str], Exception] = ToolError):
    """Open the database. Expected failures are raised as `error` (ToolError for tools, ResourceError
    for resources, MCPError for prompts) so the client sees their message; any other exception is
    treated by the SDK as a crash and its text is withheld."""
    if not DB_PATH.exists():
        raise error(f"No pipeline database at {DB_PATH}. Run import_pipeline.py first or set PIPELINE_DB.")
    try:
        with store.connect(DB_PATH) as conn:
            yield conn
    except ValueError as exc:
        raise error(str(exc)) from exc


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


@mcp.tool(annotations=SAFE_WRITE_REPEATABLE)
def record_fit_assessment(
    role_id: RoleId,
    verdict: Annotated[Verdict, Field(description="Overall fit.")],
    score: Annotated[int, Field(ge=0, le=100, description="Fit score from 0 to 100.")],
    summary: Annotated[str, Field(min_length=1, description="Two or three sentences on the overall fit.")],
    source: Annotated[str, Field(min_length=1, description="What was assessed against what, e.g. 'Job spec vs master CV, Build 0 run'.")],
    matches: Annotated[list[FitItem] | None, Field(description="Where the CV meets the spec, strongest first.")] = None,
    gaps: Annotated[list[FitItem] | None, Field(description="Where it falls short, most serious first.")] = None,
    assessed: Annotated[str | None, Field(description="Date of the assessment, YYYY-MM-DD. Defaults to today.")] = None,
) -> dict:
    """Save a fit assessment of Victor against a role. This server doesn't assess fit itself: the assessment
    is made elsewhere, by you in this conversation with his CV, or by a Build 0 run. Only record an
    assessment that was actually made against his CV, base matches and gaps on its evidence, and say in
    `source` how it was made. Saving the same assessment again (same role, date and source) changes nothing."""
    with _db() as conn:
        return store.record_fit_assessment(
            conn, role_id, verdict, score, summary, source,
            matches=[m.model_dump() for m in matches or []],
            gaps=[g.model_dump() for g in gaps or []],
            assessed=assessed,
        )


@mcp.tool(annotations=READ_ONLY)
def recent_changes(
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum number of changes to return.")] = 20,
    role_id: Annotated[str | None, Field(description="Only changes to this role.")] = None,
) -> list[dict]:
    """The audit log: the most recent changes to the pipeline, newest first, each with when it happened,
    who made it (actor 'mcp' for these tools, 'import' or 'demo'), the action and what changed. Use it
    for questions like "what did you change today?" or "what happened with this role?"."""
    with _db() as conn:
        return store.audit_log(conn, limit=limit, role_id=role_id)


def _cv_text(error: Callable[[str], Exception] = ToolError) -> str:
    """The CV as Markdown, without the Right to Work section."""
    if not CV_PATH.exists():
        raise error(f"No CV at {CV_PATH}. Set CV_PATH to a Markdown CV.")
    return _PRIVATE_SECTION.sub("", CV_PATH.read_text(encoding="utf-8")).rstrip() + "\n"


def _cv_tool(name: str, tool_input: dict) -> str:
    try:
        return CVTools(_cv_text()).run(name, tool_input)
    except CVToolError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
def search_cv(
    query: Annotated[str, Field(min_length=1, description="Words to find, e.g. 'OAuth' or 'pricing packaging'.")],
) -> str:
    """Find lines in Victor's CV that contain every word of the query (case-insensitive, matching from the
    start of words), with line numbers and the section each is in. Use it to check evidence before
    claiming he has some experience, e.g. while assessing fit or preparing for an interview. No result
    means those words aren't there, not that the experience is missing: try synonyms."""
    return _cv_tool("search_cv", {"query": query})


@mcp.tool(annotations=READ_ONLY)
def verify_quote(
    quote: Annotated[str, Field(min_length=1, description="The exact text you intend to quote from the CV.")],
) -> str:
    """Check that a quote appears word for word in Victor's CV (whitespace differences ignored). Use it on
    every quote before citing it as evidence, e.g. in record_fit_assessment. If it isn't found, the result
    shows the closest CV line so you can quote that instead."""
    return _cv_tool("verify_quote", {"quote": quote})


# Resources: read-only context a client can attach to a conversation.


@mcp.resource(
    "cv://master",
    name="cv_master",
    title="Master CV",
    description="Victor's master CV as Markdown: every role, bullet and skill, for evidence.",
    mime_type="text/markdown",
)
def cv_resource() -> str:
    return _cv_text(ResourceError)


@mcp.resource(
    "pipeline://summary",
    name="pipeline_summary",
    title="Pipeline summary",
    description="The pipeline at a glance: counts by stage, flagged and stale roles, due next actions.",
    mime_type="application/json",
)
def summary_resource() -> str:
    with _db(ResourceError) as conn:
        return json.dumps(store.pipeline_summary(conn), indent=2, ensure_ascii=False)


@mcp.resource(
    "pipeline://roles/{role_id}",
    name="role",
    title="Role record",
    description="One role's full record: details, stage history, notes and fit assessments.",
    mime_type="application/json",
)
def role_resource(role_id: str) -> str:
    with _db(ResourceError) as conn:
        role = store.get_role(conn, role_id)
    if role is None:
        raise ResourceNotFoundError(f"No role with id {role_id!r}.")
    return json.dumps(role, indent=2, ensure_ascii=False)


@mcp.resource(
    "pipeline://audit",
    name="audit_log",
    title="Recent changes",
    description="The 50 most recent changes to the pipeline, newest first: when, who, what and to which role.",
    mime_type="application/json",
)
def audit_resource() -> str:
    with _db(ResourceError) as conn:
        return json.dumps(store.audit_log(conn, limit=50), indent=2, ensure_ascii=False)


# Prompts: ready-made requests the user picks from the client's menu. Each one
# includes the current data, so Claude can answer without calling a tool first.


@mcp.prompt(
    name="weekly_review",
    title="Weekly pipeline review",
    description="Review the pipeline: where things stand, what's due or stale, and this week's priorities.",
)
def weekly_review() -> str:
    with _db(_prompt_error) as conn:
        summary = store.pipeline_summary(conn)
        active = store.list_roles(conn, limit=100)
    return f"""Give me my weekly job-search review. Here is my pipeline as of {summary['today']}.

<pipeline_summary>
{json.dumps(summary, indent=2, ensure_ascii=False)}
</pipeline_summary>

<roles>
{json.dumps([r for r in active if r['stage'] != 'closed'], indent=2, ensure_ascii=False)}
</roles>

Cover, briefly:
1. Where things stand: one line per stage with its count.
2. Overdue and due next actions, most urgent first.
3. Stale roles: for each, recommend chasing, closing or leaving it, with a one-line reason.
4. My top three priorities for this week, favouring flagged roles and later stages.

Don't change anything yet. End with a short list of the changes you'd suggest (stage moves, next \
actions, notes) and ask which ones to apply with the pipeline tools."""


@mcp.prompt(
    name="prepare_interview",
    title="Prepare for an interview",
    description="Interview prep for one role, from its record, notes and fit assessment.",
)
def prepare_interview(
    role_id: Annotated[str, Field(description="The role's id, e.g. 'telnyx-devex-plg'.")],
) -> str:
    with _db(_prompt_error) as conn:
        role = store.get_role(conn, role_id)
    if role is None:
        raise _prompt_error(f"No role with id {role_id!r}. Use search_roles to find the right id.")
    return f"""Help me prepare for my interview for this role. Here is everything in my pipeline about it.

<role>
{json.dumps(role, indent=2, ensure_ascii=False)}
</role>

Prepare me with:
1. The role in two lines: company, title and what they seem to need most.
2. Where things stand, from the stage history and notes, including anyone named.
3. My strongest evidence for this role, from the fit assessment's matches.
4. The hardest questions I'm likely to get, from the gaps, each with an honest angle to answer it.
5. Five good questions for me to ask them.

Use only what's in the record and say clearly when you're inferring. If there's no fit assessment, \
say so and base points 3 and 4 on the job title and notes instead."""


if __name__ == "__main__":
    mcp.run()
