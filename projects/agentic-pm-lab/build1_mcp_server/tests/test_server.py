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


def test_tools_carry_their_risk_tier(db):
    tools, _ = call(db)
    by_name = {t.name: t.annotations for t in tools}
    reads = {"get_role", "list_roles", "pipeline_summary", "search_roles", "recent_changes"}
    writes = {"add_role", "update_stage", "add_note", "set_next_action", "restore_role", "record_fit_assessment"}
    assert set(by_name) == reads | writes | {"archive_role"}
    assert all(by_name[n].read_only_hint for n in reads)
    assert all(not by_name[n].read_only_hint and not by_name[n].destructive_hint for n in writes)
    assert by_name["archive_role"].destructive_hint
    assert by_name["update_stage"].idempotent_hint and not by_name["add_note"].idempotent_hint


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


def test_write_tools_change_the_pipeline(db):
    _, results = call(
        db,
        ("add_role", {"company": "Beta", "title": "AI PM", "track": "ai", "job_url": "https://beta.example/1"}),
        ("update_stage", {"role_id": "beta-ai-pm", "stage": "applied", "note": "Applied online"}),
        ("add_note", {"role_id": "beta-ai-pm", "text": "Hiring manager is Sam."}),
        ("set_next_action", {"role_id": "beta-ai-pm", "action": "Chase recruiter", "due_date": "2026-11-04"}),
        ("archive_role", {"role_id": "beta-ai-pm", "reason": "Duplicate"}),
        ("list_roles", {}),
        ("list_roles", {"include_archived": True}),
        ("restore_role", {"role_id": "beta-ai-pm"}),
    )
    assert not any(r.is_error for r in results), [r.content[0].text for r in results if r.is_error]
    added, staged, noted, actioned, archived, visible, everything, restored = results
    assert json.loads(added.content[0].text)["id"] == "beta-ai-pm"
    assert [h["to_stage"] for h in json.loads(staged.content[0].text)["history"]] == ["saved", "applied"]
    assert json.loads(noted.content[0].text)["notes"][0]["text"] == "Hiring manager is Sam."
    assert json.loads(actioned.content[0].text)["next_due"] == "2026-11-04"
    assert json.loads(archived.content[0].text)["archive_reason"] == "Duplicate"
    assert [r["id"] for r in visible.structured_content["result"]] == ["acme-pm"]
    assert "beta-ai-pm" in [r["id"] for r in everything.structured_content["result"]]
    assert json.loads(restored.content[0].text)["archived_at"] is None


def test_write_errors_reach_the_client(db):
    _, (duplicate, bad_date, archived_change) = call(
        db,
        ("add_role", {"company": "acme", "title": "platform pm"}),
        ("set_next_action", {"role_id": "acme-pm", "action": "Call", "due_date": "soon"}),
        ("archive_role", {"role_id": "nope", "reason": "x"}),
    )
    assert duplicate.is_error and "already in the pipeline as 'acme-pm'" in duplicate.content[0].text
    assert bad_date.is_error and "YYYY-MM-DD" in bad_date.content[0].text
    assert archived_change.is_error and "No role with id 'nope'" in archived_change.content[0].text


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


def with_session(db_path, fn):
    """Start the server against db_path and run `await fn(session)`."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "build1_mcp_server.server"],
        env={**os.environ, "PIPELINE_DB": str(db_path)},
    )

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    return asyncio.run(run())


def test_resources(db):
    async def check(s):
        listed = [str(r.uri) for r in (await s.list_resources()).resources]
        templates = [t.uri_template for t in (await s.list_resource_templates()).resource_templates]
        summary = json.loads((await s.read_resource("pipeline://summary")).contents[0].text)
        role = json.loads((await s.read_resource("pipeline://roles/acme-pm")).contents[0].text)
        with pytest.raises(Exception, match="No role with id 'nope'"):
            await s.read_resource("pipeline://roles/nope")
        return listed, templates, summary, role

    listed, templates, summary, role = with_session(db, check)
    assert sorted(listed) == ["pipeline://audit", "pipeline://summary"]
    assert templates == ["pipeline://roles/{role_id}"]
    assert summary["by_stage"] == {"talking": 1}
    assert role["company"] == "Acme"


def test_prompts_include_current_pipeline_data(db):
    async def check(s):
        prompts = {p.name: [a.name for a in p.arguments or []] for p in (await s.list_prompts()).prompts}
        weekly = (await s.get_prompt("weekly_review")).messages[0].content.text
        prep = (await s.get_prompt("prepare_interview", {"role_id": "acme-pm"})).messages[0].content.text
        with pytest.raises(Exception, match="No role with id 'nope'"):
            await s.get_prompt("prepare_interview", {"role_id": "nope"})
        return prompts, weekly, prep

    prompts, weekly, prep = with_session(db, check)
    assert prompts == {"weekly_review": [], "prepare_interview": ["role_id"]}
    assert "Platform PM" in weekly and "top three priorities" in weekly
    assert "Prep for call." in prep and "hardest questions" in prep


def test_record_fit_assessment_and_audit_over_stdio(db):
    fit = {
        "role_id": "acme-pm", "verdict": "good", "score": 74, "summary": "Strong platform match.",
        "source": "Chat with master CV attached", "assessed": "2026-11-02",
        "matches": [{"title": "Developer platforms", "detail": "Shipped SDKs and provisioning APIs."}],
        "gaps": [{"title": "AI agents", "detail": "Agent work is internal only."}],
    }
    _, (saved, again, bad_score, changes) = call(
        db,
        ("record_fit_assessment", fit),
        ("record_fit_assessment", fit),
        ("record_fit_assessment", fit | {"score": 140}),
        ("recent_changes", {"role_id": "acme-pm"}),
    )
    role = json.loads(saved.content[0].text)
    assert [(f["verdict"], f["score"]) for f in role["fit_assessments"]] == [("good", 74)]
    assert role["fit_assessments"][0]["gaps"][0]["title"] == "AI agents"
    assert len(json.loads(again.content[0].text)["fit_assessments"]) == 1
    assert bad_score.is_error
    log = changes.structured_content["result"]
    assert [(r["actor"], r["action"]) for r in log] == [("mcp", "record_fit_assessment"), ("import", "add_role")]

    async def read_audit(s):
        return json.loads((await s.read_resource("pipeline://audit")).contents[0].text)

    assert [r["action"] for r in with_session(db, read_audit)][0] == "record_fit_assessment"
