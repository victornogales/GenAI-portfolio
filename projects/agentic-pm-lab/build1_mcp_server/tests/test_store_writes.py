from datetime import date

import pytest

from build1_mcp_server import store


@pytest.fixture
def conn(tmp_path):
    with store.connect(tmp_path / "p.db") as c:
        yield c


@pytest.fixture
def role_id(conn):
    return store.add_role(conn, "Acme Ltd", "Senior PM, Platform", track="ai", channel="Direct")["id"]


def test_add_role_creates_a_saved_role_with_history(conn):
    role = store.add_role(conn, " Acme Ltd ", "Senior PM, Platform", job_url="https://acme.example/jobs/1",
                          next_action="Read the spec", today=date(2026, 10, 30))
    assert role["id"] == "acme-ltd-senior-pm-platform"
    assert (role["company"], role["stage"], role["date_added"]) == ("Acme Ltd", "saved", "2026-10-30")
    assert role["next_action"] == "Read the spec"
    assert [(h["from_stage"], h["to_stage"]) for h in role["history"]] == [(None, "saved")]


def test_add_role_rejects_bad_input_and_duplicates(conn, role_id):
    with pytest.raises(ValueError, match="already in the pipeline"):
        store.add_role(conn, "ACME LTD", "senior pm, platform")
    with pytest.raises(ValueError, match="company can't be empty"):
        store.add_role(conn, "  ", "PM")
    with pytest.raises(ValueError, match="Unknown stage"):
        store.add_role(conn, "Beta", "PM", stage="ghosted")
    with pytest.raises(ValueError, match="Unknown channel"):
        store.add_role(conn, "Beta", "PM", channel="LinkedIn")
    with pytest.raises(ValueError, match="https"):
        store.add_role(conn, "Beta", "PM", job_url="acme.example/jobs")


def test_same_company_and_title_after_archiving_gets_a_new_id(conn, role_id):
    store.archive_role(conn, role_id, "Closed")
    assert store.add_role(conn, "Acme Ltd", "Senior PM, Platform")["id"] == f"{role_id}-2"


def test_update_stage_records_history_and_is_a_no_op_for_the_same_stage(conn, role_id):
    store.update_stage(conn, role_id, "applied", note="Applied via Greenhouse")
    role = store.update_stage(conn, role_id, "applied")
    assert role["stage"] == "applied"
    assert [(h["from_stage"], h["to_stage"], h["note"]) for h in role["history"]] == [
        (None, "saved", "Added"),
        ("saved", "applied", "Applied via Greenhouse"),
    ]
    with pytest.raises(ValueError, match="Unknown stage"):
        store.update_stage(conn, role_id, "hired")


def test_add_note(conn, role_id):
    role = store.add_note(conn, role_id, "  Recruiter call went well.  ")
    assert [n["text"] for n in role["notes"]] == ["Recruiter call went well."]
    with pytest.raises(ValueError, match="text can't be empty"):
        store.add_note(conn, role_id, "   ")
    with pytest.raises(ValueError, match="limit"):
        store.add_note(conn, role_id, "x" * (store.MAX_NOTE_LENGTH + 1))


def test_set_next_action_and_clear_it(conn, role_id):
    role = store.set_next_action(conn, role_id, "Send portfolio", due_date="2026-11-03")
    assert (role["next_action"], role["next_due"]) == ("Send portfolio", "2026-11-03")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        store.set_next_action(conn, role_id, "Send portfolio", due_date="next Tuesday")
    role = store.set_next_action(conn, role_id, "")
    assert (role["next_action"], role["next_due"]) == (None, None)


def test_due_next_action_shows_in_the_summary(conn, role_id):
    store.set_next_action(conn, role_id, "Send portfolio", due_date="2026-10-29")
    summary = store.pipeline_summary(conn, today=date(2026, 10, 30))
    assert [(r["id"], r["overdue"]) for r in summary["due"]] == [(role_id, True)]


def test_archive_hides_the_role_blocks_changes_and_restore_undoes_it(conn, role_id):
    role = store.archive_role(conn, role_id, "Position filled")
    assert role["archive_reason"] == "Position filled"
    assert store.list_roles(conn) == []
    for change in (
        lambda: store.update_stage(conn, role_id, "applied"),
        lambda: store.add_note(conn, role_id, "x"),
        lambda: store.set_next_action(conn, role_id, "x"),
        lambda: store.archive_role(conn, role_id, "again"),
    ):
        with pytest.raises(ValueError, match="archived"):
            change()
    role = store.restore_role(conn, role_id)
    assert role["archived_at"] is None and role["archive_reason"] is None
    assert [r["id"] for r in store.list_roles(conn)] == [role_id]
    with pytest.raises(ValueError, match="isn't archived"):
        store.restore_role(conn, role_id)


def test_unknown_role_id(conn):
    for change in (
        lambda: store.update_stage(conn, "nope", "applied"),
        lambda: store.add_note(conn, "nope", "x"),
        lambda: store.archive_role(conn, "nope", "x"),
        lambda: store.restore_role(conn, "nope"),
    ):
        with pytest.raises(ValueError, match="No role with id 'nope'"):
            change()
