from datetime import date

import pytest

from build1_mcp_server import store

FIT = {"verdict": "good", "score": 70, "summary": "", "matches": [], "gaps": [], "assessed": "2026-10-01", "source": "x"}


def role(**overrides):
    fields = {
        "company": "Acme", "title": "Product Manager", "stage": "applied", "track": "ai",
        "channel": "Direct", "date_added": "2026-10-01", "next_action": "Wait for reply.",
        "flagged": False, "job_url": None, "spec_url": None, "cv_url": None,
    }
    return fields | overrides


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p.db"
    with store.connect(path) as conn:
        store.upsert_role(conn, "acme", role(), updated_at="2026-10-01")
        store.upsert_role(conn, "beta", role(company="Beta", title="Platform PM", stage="talking", flagged=True),
                          updated_at="2026-10-25")
        store.upsert_role(conn, "gamma", role(company="Gamma", stage="closed"), updated_at="2026-09-01")
        store.upsert_role(conn, "delta", role(company="Delta"), updated_at="2026-10-26")
        conn.execute("UPDATE roles SET next_due = '2026-10-27' WHERE id = 'delta'")
        conn.execute("UPDATE roles SET next_due = '2026-11-05' WHERE id = 'beta'")
        conn.execute("UPDATE roles SET archived_at = '2026-10-20' WHERE id = 'gamma'")
        conn.execute("INSERT INTO notes (role_id, text, created_at) VALUES ('acme', 'Recruiter mentioned Kubernetes', '2026-10-02')")
        store.add_fit_assessment(conn, "acme", FIT)
        store.add_fit_assessment(conn, "acme", FIT | {"assessed": "2026-10-20", "score": 85, "verdict": "strong"})
    return path


def test_list_roles_hides_archived_and_puts_flagged_first(db):
    with store.connect(db) as conn:
        ids = [r["id"] for r in store.list_roles(conn)]
        assert ids == ["beta", "delta", "acme"]
        assert "gamma" in [r["id"] for r in store.list_roles(conn, include_archived=True)]


def test_list_roles_filters_by_stage_and_shows_latest_fit(db):
    with store.connect(db) as conn:
        assert [r["id"] for r in store.list_roles(conn, stage="talking")] == ["beta"]
        acme = next(r for r in store.list_roles(conn) if r["id"] == "acme")
        assert (acme["fit_verdict"], acme["fit_score"]) == ("strong", 85)


def test_list_roles_rejects_unknown_stage_and_caps_limit(db):
    with store.connect(db) as conn:
        with pytest.raises(ValueError, match="Unknown stage"):
            store.list_roles(conn, stage="ghosted")
        assert len(store.list_roles(conn, limit=1)) == 1
        assert len(store.list_roles(conn, limit=10_000)) == 3


def test_search_matches_every_word_across_fields_and_notes(db):
    with store.connect(db) as conn:
        assert [r["id"] for r in store.search_roles(conn, "platform beta")] == ["beta"]
        assert [r["id"] for r in store.search_roles(conn, "kubernetes")] == ["acme"]
        assert store.search_roles(conn, "50%") == []
        with pytest.raises(ValueError):
            store.search_roles(conn, "   ")


def test_get_role_returns_full_record(db):
    with store.connect(db) as conn:
        acme = store.get_role(conn, "acme")
        assert acme["flagged"] is False
        assert [h["to_stage"] for h in acme["history"]] == ["applied"]
        assert acme["notes"][0]["text"] == "Recruiter mentioned Kubernetes"
        assert [f["score"] for f in acme["fit_assessments"]] == [85, 70]
        assert store.get_role(conn, "gamma")["archived_at"] == "2026-10-20"
        assert store.get_role(conn, "nope") is None


def test_pipeline_summary(db):
    with store.connect(db) as conn:
        s = store.pipeline_summary(conn, stale_days=14, today=date(2026, 10, 28))
    assert s["total"] == 3 and s["archived"] == 1
    assert s["by_stage"] == {"applied": 2, "talking": 1}
    assert [r["id"] for r in s["flagged"]] == ["beta"]
    assert [r["id"] for r in s["stale"]] == ["acme"]
    assert [(r["id"], r["overdue"]) for r in s["due"]] == [("delta", True), ("beta", False)]
