"""Offline tests for run_evals: answer keys that accept more than one verdict. No API calls."""

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

import build0_spec_qualifier.evals.run_evals as ev
from build0_spec_qualifier.agent import AgentResult
from build0_spec_qualifier.checks import run_checks
from build0_spec_qualifier.schema import FitAssessment

PROJECT = Path(__file__).resolve().parents[2]
DEMO = json.loads((PROJECT / "build0_spec_qualifier/evals/golden/demo_spec1.json").read_text(encoding="utf-8"))
REF = DEMO["reference_output"]
PYTHON_ROW = 5  # "Hands-on Python": partial in the reference answer


@pytest.fixture
def case(tmp_path, monkeypatch):
    """A golden case whose Python row accepts partial or met, and an agent that answers from a queue."""
    golden = copy.deepcopy(DEMO)
    golden["expected"]["requirements"][PYTHON_ROW]["verdict"] = ["partial", "met"]
    path = tmp_path / "multi.json"
    path.write_text(json.dumps(golden), encoding="utf-8")
    answers = []

    def fake_run_agent(cv_text, spec_text, log, model, effort):
        a = FitAssessment.model_validate(answers.pop(0))
        return AgentResult(assessment=a, turns=5, usage={"cost_usd": 0.0}, checks=run_checks(a, cv_text))

    monkeypatch.setattr(ev, "run_agent", fake_run_agent)
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path / "results")
    return path, answers


YEARS_ROW = 0  # "5+ years of product management": met in the key
LLM_ROW = 2  # "Shipping LLM features": partial in the key


def answer_with(**verdicts):
    """The reference answer with some rows' verdicts changed, keeping evidence consistent with the checks."""
    answer = copy.deepcopy(REF)
    for row, verdict in verdicts.items():
        r = answer["requirements"][int(row.removeprefix("row"))]
        r["verdict"] = verdict
        if verdict == "missing":
            r["evidence"] = []
        elif not r["evidence"]:
            r["evidence"] = copy.deepcopy(REF["requirements"][YEARS_ROW]["evidence"])
    return answer


def test_either_listed_verdict_passes(case):
    path, answers = case
    answers += [answer_with(**{f"row{PYTHON_ROW}": "partial"}), answer_with(**{f"row{PYTHON_ROW}": "met"})]
    results = [ev.run_case(path, i, "t", "m", "high") for i in (1, 2)]
    assert [(r.passed, r.agreement) for r in results] == [(True, 1.0), (True, 1.0)]


def test_one_boundary_call_passes_but_is_reported(case):
    path, answers = case
    answers.append(answer_with(**{f"row{PYTHON_ROW}": "missing"}))
    result = ev.run_case(path, 1, "t", "m", "high")
    assert result.passed and (result.one_step, result.confident_errors) == (1, 0)
    assert result.agreement == (len(result.requirements) - 1) / len(result.requirements)
    report = ev.write_report([result], "m", "high", datetime(2026, 11, 3, 10, 0), private=True)
    assert report.name.startswith("private_")
    text = report.read_text(encoding="utf-8")
    assert "expected **partial or met**, got **missing** (one step)" in text
    assert "**Confident errors (met vs missing):** 0" in text


def test_a_confident_error_fails_the_run(case):
    path, answers = case
    answers.append(answer_with(**{f"row{YEARS_ROW}": "missing"}))
    result = ev.run_case(path, 1, "t", "m", "high")
    assert not result.passed and result.confident_errors == 1
    report = ev.write_report([result], "m", "high", datetime(2026, 11, 3, 10, 0), private=True)
    assert "got **missing** (confident error)" in report.read_text(encoding="utf-8")


def test_too_many_boundary_calls_fail_the_run(case):
    path, answers = case
    answers.append(answer_with(**{f"row{PYTHON_ROW}": "missing", f"row{LLM_ROW}": "met"}))
    result = ev.run_case(path, 1, "t", "m", "high")
    assert (result.one_step, result.confident_errors) == (2, 0)
    assert result.agreement < ev.PASS_AGREEMENT and not result.passed


def test_single_verdict_keys_still_work():
    rows, extra = ev.match_requirements(
        [{"requirement": "5+ years of product management", "importance": "must_have", "verdict": "met"}],
        FitAssessment.model_validate(REF).requirements,
    )
    assert rows[0].verdict_ok and rows[0].matched_to.startswith("5+ years")
    assert len(extra) == len(REF["requirements"]) - 1
