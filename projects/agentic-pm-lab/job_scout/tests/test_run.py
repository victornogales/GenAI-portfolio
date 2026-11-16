"""The scout funnel end to end, offline: fake boards, fake triage and a fake Build 0 agent."""

import copy
import json
from pathlib import Path

import pytest

from build0_spec_qualifier.agent import AgentResult
from build0_spec_qualifier.checks import CheckResult, run_checks
from build0_spec_qualifier.schema import FitAssessment
from build1_mcp_server import store
from job_scout.run import scout, write_report
from job_scout.sources import Posting
from job_scout.triage import TriageDecision

PROJECT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads((PROJECT / "build0_spec_qualifier/evals/golden/demo_spec1.json").read_text(encoding="utf-8"))
CV = (PROJECT / GOLDEN["cv"]).read_text(encoding="utf-8")

WATCHLIST = '[[companies]]\nname = "Acme"\nats = "greenhouse"\nslug = "acme"\n'
PROFILE = """
[titles]
include = ["product manager"]
exclude = ["intern"]
[locations]
include = ["london"]
[triage]
min_score = 50
profile = "Senior PM."
"""


def post(n, title, location="London"):
    return Posting("greenhouse", "Acme", str(n), title, location, f"https://boards.example/acme/{n}", f"Spec for {title}.")


BOARD = [
    post(1, "Senior Product Manager, AI"),       # strong: added
    post(2, "Product Manager, Payments"),        # assessed but "skip"
    post(3, "Product Manager, Growth"),          # triage skips it
    post(4, "Product Manager Intern"),           # rules filter it out
    post(5, "Product Manager, Data", "Paris"),   # rules filter it out
    post(6, "Product Manager, Checks"),          # assessment fails a hard check
]
TRIAGE = {
    "Senior Product Manager, AI": TriageDecision(decision="shortlist", score=90, reason="Strong AI platform match."),
    "Product Manager, Payments": TriageDecision(decision="shortlist", score=70, reason="Platform, but payments."),
    "Product Manager, Growth": TriageDecision(decision="skip", score=20, reason="Consumer growth."),
    "Product Manager, Checks": TriageDecision(decision="shortlist", score=60, reason="Maybe."),
}


def agent_result(recommendation="apply_with_caveats", break_check=False):
    a = FitAssessment.model_validate(GOLDEN["reference_output"] | {"recommendation": recommendation, "fit_score": 72})
    checks = run_checks(a, CV)
    if break_check:
        checks.append(CheckResult("quotes_in_cv", False, "invented quote"))
    return AgentResult(assessment=a, turns=5, usage={}, checks=checks)


@pytest.fixture
def env(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "watchlist.toml").write_text(WATCHLIST, encoding="utf-8")
    (config / "profile.toml").write_text(PROFILE, encoding="utf-8")
    cv = tmp_path / "cv.md"
    cv.write_text(CV, encoding="utf-8")
    board = list(BOARD)
    calls = {"triage": 0, "assess": 0}

    def fetch(watchlist):
        return list(board), []

    def triage_fn(p, profile_summary, log, model):
        calls["triage"] += 1
        return TRIAGE[p.title]

    def assess_fn(cv_text, spec_text, log, model):
        calls["assess"] += 1
        title = spec_text.splitlines()[0]
        return agent_result("skip" if "Payments" in title else "apply_with_caveats", break_check="Checks" in title)

    def go(**kw):
        return scout(config, tmp_path / "p.db", cv, specs_dir=tmp_path / "specs",
                     fetch=fetch, triage_fn=triage_fn, assess_fn=assess_fn, **kw)

    return go, tmp_path / "p.db", board, calls, tmp_path


def stages(run):
    return {o.posting.title: o.stage for o in run.outcomes}


def test_funnel_adds_only_good_fits_that_pass_checks(env):
    go, db, _, calls, tmp = env
    run = go(max_assess=5)
    assert stages(run) == {
        "Senior Product Manager, AI": "added",
        "Product Manager, Payments": "assessed_skip",
        "Product Manager, Growth": "triaged_out",
        "Product Manager Intern": "filtered_out",
        "Product Manager, Data": "filtered_out",
        "Product Manager, Checks": "checks_failed",
    }
    assert calls == {"triage": 4, "assess": 3}
    with store.connect(db) as conn:
        [role] = store.list_roles(conn)
        assert (role["company"], role["title"], role["stage"], role["fit_score"]) == ("Acme", "Senior Product Manager, AI", "saved", 72)
        full = store.get_role(conn, role["id"])
        assert full["job_url"] == "https://boards.example/acme/1" and "Found by the job scout" in full["notes"][0]["text"]
        actors = {(r["actor"], r["action"]) for r in store.audit_log(conn, limit=50)}
        assert {("scout", "add_role"), ("scout", "add_note"), ("build0", "record_fit_assessment"), ("scout", "scout_run")} <= actors
        assert [r["id"] for r in store.roles_added_by(conn, "scout", "2000-01-01")] == [role["id"]]
    assert len(list((tmp / "specs").glob("*.md"))) == 3


def test_second_run_skips_everything_already_seen(env):
    go, db, board, calls, _ = env
    go(max_assess=5)
    board.append(post(7, "Senior Product Manager, Platform"))
    TRIAGE["Senior Product Manager, Platform"] = TriageDecision(decision="shortlist", score=80, reason="Platform.")
    run = go(max_assess=5)
    assert run.new == 1 and stages(run) == {"Senior Product Manager, Platform": "added"}
    assert calls == {"triage": 5, "assess": 4}


def test_the_cap_leaves_the_rest_shortlisted_for_next_time(env):
    go, db, _, calls, _ = env
    run = go(max_assess=1)
    assert stages(run)["Senior Product Manager, AI"] == "added"
    assert stages(run)["Product Manager, Payments"] == "shortlisted"
    next_run = go(max_assess=5)
    assert stages(next_run) == {"Product Manager, Payments": "assessed_skip", "Product Manager, Checks": "checks_failed"}
    assert calls["triage"] == 4  # the waiting ones weren't triaged again


def test_a_job_already_in_the_pipeline_is_not_added_twice(env):
    go, db, _, _, _ = env
    with store.connect(db) as conn:
        store.add_role(conn, "Acme", "Senior PM (tracked by hand)", job_url="https://boards.example/acme/1")
    run = go(max_assess=5)
    assert stages(run)["Senior Product Manager, AI"] == "already_in_pipeline"


def test_dry_run_changes_nothing(env):
    go, db, _, calls, tmp = env
    run = go(max_assess=5, dry_run=True)
    assert calls["assess"] == 0 and stages(run)["Senior Product Manager, AI"] == "shortlisted"
    with store.connect(db) as conn:
        assert store.list_roles(conn) == [] and store.seen_posting_keys(conn) == set() and store.audit_log(conn) == []
    report = write_report(run, tmp / "runs", dry_run=True)
    assert "dry run, nothing changed" in report.read_text(encoding="utf-8")


def test_same_role_in_many_locations_is_triaged_once(env):
    go, db, board, calls, _ = env
    board.append(post(8, "Senior Product Manager, AI", "London, UK (Remote)"))
    run = go(max_assess=5)
    assert [o.stage for o in run.outcomes if o.posting.title == "Senior Product Manager, AI"].count("duplicate") == 1
    assert calls["triage"] == 4
    board.append(post(9, "Senior Product Manager, AI", "London (Hybrid)"))
    assert stages(go(max_assess=5)) == {"Senior Product Manager, AI": "duplicate"}  # judged on an earlier run


def test_the_triage_cap_defers_the_rest_to_the_next_run(env):
    go, db, _, calls, _ = env
    run = go(max_assess=5, max_triage=1)
    assert calls["triage"] == 1 and sum(o.stage == "triage_deferred" for o in run.outcomes) == 3
    next_run = go(max_assess=5)
    assert calls["triage"] == 4 and "triage_deferred" not in stages(next_run).values()
