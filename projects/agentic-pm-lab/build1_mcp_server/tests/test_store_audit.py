import json
from datetime import date

import pytest

from build1_mcp_server import store
from build1_mcp_server.import_pipeline import import_apps, load
from build1_mcp_server.make_demo_db import build


@pytest.fixture
def conn(tmp_path):
    with store.connect(tmp_path / "p.db") as c:
        yield c


def actions(conn):
    return [(r["actor"], r["action"]) for r in reversed(store.audit_log(conn, limit=100))]


def test_every_tool_write_leaves_one_audit_row(conn):
    role_id = store.add_role(conn, "Acme", "PM")["id"]
    store.update_stage(conn, role_id, "applied", note="Applied online")
    store.add_note(conn, role_id, "Recruiter is Sam.")
    store.set_next_action(conn, role_id, "Chase", due_date="2026-11-05")
    store.record_fit_assessment(conn, role_id, "good", 72, "Solid match.", "Chat with CV attached")
    store.archive_role(conn, role_id, "Duplicate")
    store.restore_role(conn, role_id)
    assert actions(conn) == [
        ("mcp", "add_role"), ("mcp", "update_stage"), ("mcp", "add_note"), ("mcp", "set_next_action"),
        ("mcp", "record_fit_assessment"), ("mcp", "archive_role"), ("mcp", "restore_role"),
    ]
    latest = store.audit_log(conn, limit=7)
    assert all(r["role_id"] == role_id for r in latest)
    by_action = {r["action"]: r["details"] for r in latest}
    assert by_action["update_stage"] == {"from": "saved", "to": "applied", "note": "Applied online"}
    assert by_action["set_next_action"]["after"] == {"action": "Chase", "due_date": "2026-11-05"}
    assert by_action["restore_role"] == {"archive_reason": "Duplicate"}


def test_writes_that_change_nothing_leave_no_audit_row(conn):
    role_id = store.add_role(conn, "Acme", "PM", next_action="Chase")["id"]
    store.record_fit_assessment(conn, role_id, "good", 72, "Solid.", "Chat", assessed="2026-11-01")
    before = len(store.audit_log(conn, limit=100))
    store.update_stage(conn, role_id, "saved")
    store.set_next_action(conn, role_id, "Chase")
    store.record_fit_assessment(conn, role_id, "good", 72, "Solid.", "Chat", assessed="2026-11-01")
    assert len(store.audit_log(conn, limit=100)) == before


def test_a_failed_transaction_leaves_no_audit_row(tmp_path):
    db = tmp_path / "p.db"
    with pytest.raises(RuntimeError):
        with store.connect(db) as conn:
            store.add_role(conn, "Acme", "PM")
            raise RuntimeError("tool crashed after the write")
    with store.connect(db) as conn:
        assert store.list_roles(conn) == [] and store.audit_log(conn) == []


def test_import_and_demo_are_labelled(tmp_path):
    export = tmp_path / "export.json"
    export.write_text(json.dumps({"applications": [
        {"id": "acme", "company": "Acme", "role": "PM", "status": "applied", "fit": {
            "verdict": "good", "score": 70, "assessed": "2026-10-01", "source": "x"}}]}), encoding="utf-8")
    db = tmp_path / "p.db"
    import_apps(load(export), db)
    with store.connect(db) as conn:
        assert actions(conn) == [("import", "add_role"), ("import", "record_fit_assessment")]

    demo = tmp_path / "demo.db"
    build(demo, today=date(2026, 11, 2))
    with store.connect(demo) as conn:
        assert {actor for actor, _ in actions(conn)} == {"demo"}


def test_record_fit_assessment_validates_and_returns_the_role(conn):
    role_id = store.add_role(conn, "Acme", "PM")["id"]
    role = store.record_fit_assessment(
        conn, role_id, "strong", 85, "Strong platform match.", "Build 0 run, Haiku 5.5",
        matches=[{"title": "APIs", "detail": "Built REST and OData APIs."}],
        gaps=[{"title": "Domain", "detail": "No fintech."}], today=date(2026, 11, 2),
    )
    fit = role["fit_assessments"][0]
    assert (fit["verdict"], fit["score"], fit["assessed"]) == ("strong", 85, "2026-11-02")
    assert fit["matches"] == [{"title": "APIs", "detail": "Built REST and OData APIs."}]
    for bad, message in [
        ({"verdict": "great"}, "Unknown verdict"),
        ({"score": 120}, "0 to 100"),
        ({"score": 7.5}, "0 to 100"),
        ({"summary": " "}, "summary can't be empty"),
        ({"source": ""}, "source can't be empty"),
        ({"assessed": "last week"}, "YYYY-MM-DD"),
        ({"matches": [{"title": "APIs"}]}, "title and a detail"),
    ]:
        args = {"verdict": "good", "score": 70, "summary": "x", "source": "y"} | bad
        with pytest.raises(ValueError, match=message):
            store.record_fit_assessment(conn, role_id, **args)
    store.archive_role(conn, role_id, "Closed")
    with pytest.raises(ValueError, match="archived"):
        store.record_fit_assessment(conn, role_id, "good", 70, "x", "y")


def test_audit_log_filters_by_role_and_caps_limit(conn):
    a = store.add_role(conn, "Acme", "PM")["id"]
    b = store.add_role(conn, "Beta", "PM")["id"]
    store.add_note(conn, a, "One")
    assert [r["action"] for r in store.audit_log(conn, role_id=a)] == ["add_note", "add_role"]
    assert [r["role_id"] for r in store.audit_log(conn, limit=1)] == [a]
    assert len(store.audit_log(conn, role_id=b)) == 1
