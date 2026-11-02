"""Tests for agent.py --record, using the demo case's saved answer instead of a live run."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from build0_spec_qualifier.agent import AgentError, AgentResult, check_role, record_result, to_pipeline_fit
from build0_spec_qualifier.checks import CheckResult, run_checks
from build0_spec_qualifier.schema import FitAssessment
from build1_mcp_server import store

PROJECT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads((PROJECT / "build0_spec_qualifier/evals/golden/demo_spec1.json").read_text(encoding="utf-8"))
CV = (PROJECT / GOLDEN["cv"]).read_text(encoding="utf-8")


def demo_result(**changes) -> AgentResult:
    assessment = FitAssessment.model_validate(GOLDEN["reference_output"] | changes)
    return AgentResult(assessment=assessment, turns=6, usage={}, checks=run_checks(assessment, CV))


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p.db"
    with store.connect(path) as conn:
        store.add_role(conn, "Lumen Data", "Senior Product Manager, AI Platform")
    return path


ROLE = "lumen-data-senior-product-manager-ai-platform"


def test_maps_requirements_to_matches_and_gaps():
    fit = to_pipeline_fit(demo_result().assessment, "claude-haiku-5-5", "demo_spec.md")
    assert (fit["verdict"], fit["score"]) == ("weak", 35)
    assert "Recommendation: skip." in fit["summary"]
    assert fit["source"] == "Build 0 run (claude-haiku-5-5) on demo_spec.md, all hard checks passed"
    assert len(fit["matches"]) == 4 and len(fit["gaps"]) == 4
    assert fit["matches"][0]["title"].startswith("5+ years") and "Evidence:" in fit["matches"][0]["detail"]
    assert fit["gaps"][0]["detail"].startswith("Partial (must-have).")
    assert fit["gaps"][-1]["detail"].startswith("Missing (nice-to-have).")


@pytest.mark.parametrize("score,verdict", [(100, "strong"), (80, "strong"), (79, "good"), (65, "good"),
                                           (64, "stretch"), (45, "stretch"), (44, "weak"), (0, "weak")])
def test_verdict_bands(score, verdict):
    assert to_pipeline_fit(demo_result(fit_score=score).assessment, "m", "s")["verdict"] == verdict


def test_records_the_assessment_audited_as_build0(db):
    role = record_result(demo_result(), ROLE, db, "claude-haiku-5-5", "demo_spec.md")
    assert [(f["verdict"], f["score"]) for f in role["fit_assessments"]] == [("weak", 35)]
    with store.connect(db) as conn:
        latest = store.audit_log(conn, limit=1)[0]
    assert (latest["actor"], latest["action"], latest["role_id"]) == ("build0", "record_fit_assessment", ROLE)


def test_refuses_to_record_when_a_check_failed(db):
    result = demo_result()
    result.checks.append(CheckResult("quotes_in_cv", False, "Not in CV: invented quote"))
    with pytest.raises(AgentError, match="hard checks failed \\(quotes_in_cv\\)"):
        record_result(result, ROLE, db, "m", "s")
    with store.connect(db) as conn:
        assert store.get_role(conn, ROLE)["fit_assessments"] == []


def test_check_role_catches_problems_before_a_paid_run(db, tmp_path):
    check_role(ROLE, db)
    with pytest.raises(AgentError, match="No pipeline database"):
        check_role(ROLE, tmp_path / "absent.db")
    with pytest.raises(AgentError, match="No role with id 'nope'"):
        check_role("nope", db)
    with store.connect(db) as conn:
        store.archive_role(conn, ROLE, "Closed")
    with pytest.raises(AgentError, match="archived"):
        check_role(ROLE, db)


def test_cli_stops_before_the_run_for_an_unknown_role(db):
    proc = subprocess.run(
        [sys.executable, "-m", "build0_spec_qualifier.agent", "--cv", "data_public/demo_cv.md",
         "--spec", "data_public/demo_spec.md", "--record", "nope", "--db", str(db)],
        cwd=PROJECT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    assert "No role with id 'nope'" in proc.stderr
    assert proc.stdout == ""
