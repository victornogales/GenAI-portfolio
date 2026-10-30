from datetime import date

from build1_mcp_server import store
from build1_mcp_server.make_demo_db import ROLES, build


def test_demo_covers_every_feature(tmp_path):
    out = tmp_path / "demo.db"
    summary = build(out, today=date(2026, 10, 30))
    assert summary["total"] == len(ROLES) - 1 and summary["archived"] == 1
    assert set(summary["by_stage"]) == set(store.STAGES)
    assert summary["flagged"] and summary["stale"]
    assert any(r["overdue"] for r in summary["due"]) and not all(r["overdue"] for r in summary["due"])
    with store.connect(out) as conn:
        role = store.get_role(conn, "harbourline-health-pm-integrations")
    assert [h["to_stage"] for h in role["history"]] == ["saved", "applied", "talking", "interviewing"]
    assert role["notes"] and role["fit_assessments"]


def test_rebuilding_replaces_the_file(tmp_path):
    out = tmp_path / "demo.db"
    build(out, today=date(2026, 10, 30))
    assert build(out, today=date(2026, 10, 30))["total"] == len(ROLES) - 1
