"""End-to-end tests: start server.py over stdio and call it as an MCP client would."""

import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from build1_mcp_server import store


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p.db"
    fields = {
        "company": "Acme", "title": "Platform PM", "stage": "talking", "track": "ai", "channel": "Direct",
        "date_added": "2026-10-01", "next_action": "Prep for call.", "flagged": True,
        "job_url": None, "spec_url": None, "cv_url": None,
    }
    with store.connect(path) as conn:
        store.upsert_role(conn, "acme-pm", fields, updated_at="2026-10-01")
    return path


def call(db_path, *calls):
    """Start the server against db_path, run (tool, arguments) calls in order, return the results."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "build1_mcp_server.server"],
        env={**os.environ, "PIPELINE_DB": str(db_path)},
    )

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                return tools, [await session.call_tool(name, args) for name, args in calls]

    return asyncio.run(run())


def test_lists_the_four_read_only_tools(db):
    tools, _ = call(db)
    assert sorted(t.name for t in tools) == ["get_role", "list_roles", "pipeline_summary", "search_roles"]
    assert all(t.annotations.read_only_hint for t in tools)


def test_read_tools_return_pipeline_data(db):
    _, (summary, listed, found, role) = call(
        db,
        ("pipeline_summary", {}),
        ("list_roles", {"stage": "talking"}),
        ("search_roles", {"query": "acme"}),
        ("get_role", {"role_id": "acme-pm"}),
    )
    assert json.loads(summary.content[0].text)["by_stage"] == {"talking": 1}
    assert [r["id"] for r in listed.structured_content["result"]] == ["acme-pm"]
    assert [r["id"] for r in found.structured_content["result"]] == ["acme-pm"]
    assert json.loads(role.content[0].text)["next_action"] == "Prep for call."


def test_expected_errors_reach_the_client(db):
    _, (missing, bad_stage, empty) = call(
        db,
        ("get_role", {"role_id": "nope"}),
        ("list_roles", {"stage": "ghosted"}),
        ("search_roles", {"query": "   "}),
    )
    assert missing.is_error and "No role with id 'nope'" in missing.content[0].text
    assert bad_stage.is_error and "stage" in bad_stage.content[0].text
    assert empty.is_error and "Query is empty" in empty.content[0].text


def test_missing_database_is_reported_not_created(tmp_path):
    db_path = tmp_path / "absent.db"
    _, (result,) = call(db_path, ("list_roles", {}))
    assert result.is_error and "No pipeline database" in result.content[0].text
    assert not db_path.exists()
