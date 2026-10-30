# Build 1: MCP server

An MCP server that exposes a job-search pipeline (roles, stages, notes) to Claude through tools, resources and prompts. Data lives in SQLite, and `store.py` is the only code that writes to it.

## Files

| File | Role |
|---|---|
| `server.py` | FastMCP app: tools, resources, prompts |
| `store.py` | SQLite access, the only code that writes |
| `import_pipeline.py` | One-off import from a JSON export |
| `tests/` | Tests |

The demo database is `data_public/pipeline_demo.db` (synthetic roles).

## Install in Claude Desktop

_TODO_

## Install in Claude Code

_TODO_
