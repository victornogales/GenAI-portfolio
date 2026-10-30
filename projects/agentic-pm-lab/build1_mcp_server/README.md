# Build 1: MCP server

An MCP server that exposes a job-search pipeline (roles, stages, notes) to Claude through tools, resources and prompts. Data lives in SQLite, and `store.py` is the only code that writes to it.

## Files

| File | Role |
|---|---|
| `server.py` | FastMCP app: tools, resources, prompts |
| `store.py` | SQLite access, the only code that writes |
| `import_pipeline.py` | One-off import from a JSON export |
| `make_demo_db.py` | Builds the synthetic demo database |
| `tests/` | Tests |

The demo database is `data_public/pipeline_demo.db`: 12 fictional roles covering every stage, flagged and stale roles, due and overdue next actions, notes, fit assessments and an archived role. Rebuild it (dates move to today) with `uv run python -m build1_mcp_server.make_demo_db`. The real one is `data_private/pipeline.db`, built from a tracker export:

```
uv run python -m build1_mcp_server.import_pipeline data_private/pipeline_export.json
```

Add `--dry-run` to validate without writing. Re-running is safe: unchanged roles are skipped and stage changes are added to the history.

## Design

### Risk tiers

Every tool has a tier that decides how much the client should check with you before running it. The tiers map to standard MCP tool annotations, which clients such as Claude Desktop use in their permission prompts.

| Tier | Meaning | Client behaviour | MCP annotation |
|---|---|---|---|
| 0, read | Changes nothing | Can be allowed automatically | `readOnlyHint: true` |
| 1, safe write | Adds or changes data, can be undone | Ask the first time, then allow | `destructiveHint: false` (plus `idempotentHint` where repeating is harmless) |
| 2, risky | Hides data, can't easily be undone, or costs money or leaves the machine | Always confirm | `destructiveHint: true` or `openWorldHint: true` |

There is deliberately no hard-delete tool. `archive_role` hides a role instead, so the worst mistake stays reversible.

### Tools

| Tool | One-liner | Inputs | Tier |
|---|---|---|---|
| `list_roles` | List roles, optionally filtered by stage | `stage?` (enum), `limit?` (default 20), `include_archived?` (default false) | 0 |
| `get_role` | Full record for one role: details, stage history, notes | `role_id` | 0 |
| `search_roles` | Find roles by company, title or keyword | `query` | 0 |
| `pipeline_summary` | Counts by stage, plus roles with no activity for N days | `stale_days?` (default 14) | 0 |
| `add_role` | Add a new role to the pipeline | `company`, `title`, `stage?` (default `saved`), `track?`, `channel?`, `job_url?`, `spec_url?`, `next_action?` | 1 |
| `update_stage` | Move a role to a new stage, recording the change in its history | `role_id`, `stage` (enum), `note?` | 1 |
| `add_note` | Add a dated note to a role | `role_id`, `text` | 1 |
| `set_next_action` | Set the next step and due date for a role | `role_id`, `action`, `due_date` (YYYY-MM-DD) | 1 |
| `archive_role` | Hide a role from the pipeline (can be restored) | `role_id`, `reason` | 2 |
| `restore_role` | Bring an archived role back | `role_id` | 1 |
| `assess_fit` | Run the Build 0 agent on a role's spec against your CV | `role_id`, `model?` (default Haiku) | 2, costs money and sends the CV to the API |

`stage` is one of `saved`, `applied`, `talking`, `interviewing`, `offer`, `closed`, the stages the existing job tracker uses. `track` is one of `ai`, `integrations`, `general`, `solutions`, and `channel` one of `Direct`, `Recruiter`, `Inbound`, `Referral`. `?` marks an optional input. An archived role can be read or restored but not changed.

### Resources

| URI | What it returns |
|---|---|
| `pipeline://summary` | The pipeline at a glance: counts by stage, stale roles, upcoming next actions |
| `pipeline://roles/{role_id}` | One role's full record |

### Prompts

| Prompt | Inputs | What it does |
|---|---|---|
| `weekly_review` | none | What's stale, what's due and what to do next |
| `prepare_interview` | `role_id` | Pulls together the role, your notes and the fit assessment |

### Open questions

- **`list_roles` vs `search_roles`.** Keep both only if they stay clearly different (filtering vs free-text search). Otherwise merge them into `list_roles` with an optional `query`.
- **`assess_fit`.** It links Build 1 to Build 0 but costs money and sends the CV to Anthropic's API. Leaving it out keeps the server fully local.

## Try it with the MCP Inspector

The Inspector is a browser UI for calling the server's tools by hand. It needs Node.js. From the `agentic-pm-lab` folder:

```
uv run mcp dev build1_mcp_server/server.py
```

Open the URL it prints, click **Connect**, then **Tools → List Tools**. The server reads `data_private/pipeline.db`; to use another database, set `PIPELINE_DB` first (in PowerShell, `$env:PIPELINE_DB = "data_public/pipeline_demo.db"`).

The server has every tool in the table below except `assess_fit`. The write tools change whichever database the server is using, so try them on the demo database or a copy before using your real one.

## Install in Claude Desktop

_TODO_

## Install in Claude Code

_TODO_
