"""The job scout: find new roles on company job boards that fit the CV, and add good ones to the pipeline.

A funnel, cheapest step first:

    1. fetch every open posting from the watchlist's job boards      (free)
    2. skip postings already seen on an earlier run                  (free)
    3. screen title and location with the profile's rules            (free)
    4. triage the rest with a cheap model call, once per company and title,
       at most --max-triage a run                                    (~$0.0003 each)
    5. run the Build 0 agent on the shortlist, best first, capped    (~$0.01 each)
    6. add roles that pass every hard check and aren't a "skip" to the
       pipeline as "saved", with the fit assessment                  (audited as "scout")

It's a workflow, not an agent: code decides the steps, the model only judges
inside steps 4 and 5. Postings are untrusted text, so the model never has a
tool that writes; every write here is plain code through store.py.

Run from the agentic-pm-lab folder:

    uv run python -m job_scout.run --dry-run            # fetch, screen and triage; change nothing
    uv run python -m job_scout.run --max-assess 3       # the real thing, assessing at most 3 roles
"""

import argparse
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from build0_spec_qualifier.agent import AgentError, record_result, run_agent
from build1_mcp_server import store
from shared.llm_log import LLMLog

from .filters import screen
from .sources import Posting, fetch_all
from .triage import MODEL, TriageError, triage

PROJECT = store.PROJECT_DIR
DEFAULT_CONFIG = PROJECT / "data_private" / "scout"
DEFAULT_CV = PROJECT / "data_private" / "vnogales-cv-master.md"
SPECS_DIR = PROJECT / "data_private" / "specs" / "scout"


@dataclass
class Outcome:
    posting: Posting
    stage: str
    reason: str = ""
    score: int | None = None
    role_id: str | None = None
    fit: str = ""


@dataclass
class ScoutRun:
    fetched: int = 0
    new: int = 0
    errors: list[str] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    def count(self, stage: str) -> int:
        return sum(o.stage == stage for o in self.outcomes)


def load_config(config_dir: Path) -> tuple[list[dict], dict]:
    watchlist = tomllib.loads((config_dir / "watchlist.toml").read_text(encoding="utf-8")).get("companies", [])
    profile = tomllib.loads((config_dir / "profile.toml").read_text(encoding="utf-8"))
    return watchlist, profile


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].rstrip("-")


def save_spec(posting: Posting, specs_dir: Path) -> Path:
    specs_dir.mkdir(parents=True, exist_ok=True)
    path = specs_dir / f"{slug(posting.company)}-{slug(posting.title)}-{posting.external_id[:8]}.md"
    path.write_text(
        f"# {posting.company} - {posting.title}\n\n- Location: {posting.location}\n- Job post: {posting.url}\n"
        f"- Posted: {posting.posted_at or 'unknown'}\n- Found by the job scout on {date.today().isoformat()}\n\n"
        f"{posting.description}\n",
        encoding="utf-8",
    )
    return path


def scout(
    config_dir: Path = DEFAULT_CONFIG,
    db_path: Path = store.DEFAULT_DB,
    cv_path: Path = DEFAULT_CV,
    model: str = MODEL,
    max_assess: int = 5,
    dry_run: bool = False,
    specs_dir: Path = SPECS_DIR,
    max_triage: int = 40,
    fetch=fetch_all,
    triage_fn=triage,
    assess_fn=run_agent,
    log: LLMLog | None = None,
) -> ScoutRun:
    watchlist, profile = load_config(config_dir)
    log = log or LLMLog(run_id=f"scout-{date.today().isoformat()}")
    run = ScoutRun()

    postings, run.errors = fetch(watchlist)
    run.fetched = len(postings)
    with store.connect(db_path) as conn:
        seen = store.seen_posting_keys(conn)
        pending = {r["key"]: r for r in conn.execute("SELECT * FROM scout_postings WHERE stage = 'shortlisted'")}
        judged_titles = {
            (r["company"].lower(), r["title"].lower())
            for r in conn.execute("SELECT company, title FROM scout_postings WHERE stage NOT IN ('filtered_out', 'duplicate')")
        }
    unique = {p.key: p for p in postings}
    new = [p for k, p in unique.items() if k not in seen]
    run.new = len(new)

    # Steps 3 and 4: screen, then triage. Postings shortlisted on an earlier run keep their triage.
    # The same company and title posted for many locations is triaged once; the copies are "duplicate".
    shortlist: list[Outcome] = []
    triaged = 0
    for p in new:
        if p.key in pending:
            row = pending[p.key]
            shortlist.append(Outcome(p, "shortlisted", row["reason"] or "", row["score"]))
            judged_titles.add((p.company.lower(), p.title.lower()))
            continue
        result = screen(p, profile)
        if not result.keep:
            run.outcomes.append(Outcome(p, "filtered_out", result.reason))
            continue
        title_key = (p.company.lower(), p.title.lower())
        if title_key in judged_titles:
            run.outcomes.append(Outcome(p, "duplicate", "Same company and title as a posting already judged."))
            continue
        judged_titles.add(title_key)
        if triaged >= max_triage:
            run.outcomes.append(Outcome(p, "triage_deferred", f"Over this run's cap of {max_triage} triage calls."))
            continue
        triaged += 1
        try:
            t = triage_fn(p, profile.get("triage", {}).get("profile", ""), log, model)
        except TriageError as exc:
            run.outcomes.append(Outcome(p, "triage_error", str(exc)))
            continue
        min_score = profile.get("triage", {}).get("min_score", 0)
        if t.decision == "shortlist" and t.score >= min_score:
            shortlist.append(Outcome(p, "shortlisted", t.reason, t.score))
        else:
            run.outcomes.append(Outcome(p, "triaged_out", t.reason, t.score))

    # Step 5 and 6: assess the best of the shortlist, and add good fits.
    shortlist.sort(key=lambda o: o.score or 0, reverse=True)
    cv_text = cv_path.read_text(encoding="utf-8") if shortlist and not dry_run else ""
    for i, o in enumerate(shortlist):
        if dry_run or i >= max_assess:
            run.outcomes.append(o)  # stays shortlisted for a later run
            continue
        run.outcomes.append(_assess(o, cv_text, db_path, model, specs_dir, assess_fn, log))

    run.usage = log.totals()
    if not dry_run:
        with store.connect(db_path) as conn:
            for o in run.outcomes:
                if o.stage == "triage_deferred":  # not recorded, so the next run picks it up
                    continue
                store.record_posting(conn, o.posting.key, o.posting.company, o.posting.title, o.posting.url,
                                     o.stage, reason=o.reason, score=o.score, role_id=o.role_id)
            store.record_scout_run(conn, {
                "fetched": run.fetched, "new": run.new, "errors": len(run.errors),
                **{s: run.count(s) for s in ("filtered_out", "duplicate", "triaged_out", "shortlisted", "added",
                                             "assessed_skip", "checks_failed", "already_in_pipeline")},
                "cost_usd": run.usage.get("cost_usd", 0),
            })
    return run


def _assess(o: Outcome, cv_text: str, db_path: Path, model: str, specs_dir: Path, assess_fn, log: LLMLog) -> Outcome:
    p = o.posting
    with store.connect(db_path) as conn:
        existing = store.find_role_by_job_url(conn, p.url)
    if existing:
        return Outcome(p, "already_in_pipeline", o.reason, o.score, existing)

    spec_path = save_spec(p, specs_dir)
    try:
        result = assess_fn(cv_text=cv_text, spec_text=spec_path.read_text(encoding="utf-8"), log=log, model=model)
    except AgentError as exc:
        return Outcome(p, "assessment_error", str(exc), o.score)
    a = result.assessment
    if any(not c.passed for c in result.checks):
        failed = ", ".join(c.name for c in result.checks if not c.passed)
        return Outcome(p, "checks_failed", f"hard checks failed: {failed}", o.score)
    if a.recommendation == "skip":
        return Outcome(p, "assessed_skip", a.summary, o.score, fit=f"{a.fit_score}, skip")

    shown_path = spec_path.relative_to(PROJECT).as_posix() if spec_path.is_relative_to(PROJECT) else str(spec_path)
    with store.connect(db_path) as conn:
        try:
            role = store.add_role(conn, p.company, p.title, stage="saved", channel="Direct", job_url=p.url,
                                  next_action="Review this lead from the job scout", actor="scout")
        except ValueError as exc:
            if "already in the pipeline" not in str(exc):
                raise
            return Outcome(p, "already_in_pipeline", str(exc), o.score)
        store.add_note(conn, role["id"], f"Found by the job scout. Triage ({o.score}): {o.reason} "
                                         f"Spec saved at {shown_path}.", actor="scout")
    recorded = record_result(result, role["id"], db_path, model, spec_path.name)
    fit = recorded["fit_assessments"][0]
    return Outcome(p, "added", o.reason, o.score, role["id"], f"{fit['verdict']} {fit['score']}, {a.recommendation}")


def write_report(run: ScoutRun, out_dir: Path, dry_run: bool) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now():%Y-%m-%d_%H%M}{'_dry-run' if dry_run else ''}.md"
    by = lambda stage: [o for o in run.outcomes if o.stage == stage]
    lines = [
        f"# Job scout: {date.today().isoformat()}{' (dry run, nothing changed)' if dry_run else ''}",
        "",
        f"- **Fetched:** {run.fetched} open postings, **new since last run:** {run.new}",
        f"- **Filtered out by rules:** {len(by('filtered_out'))}, **duplicates:** {len(by('duplicate'))}, "
        f"**skipped by triage:** {len(by('triaged_out'))}, **deferred to next run:** {len(by('triage_deferred'))}",
        f"- **Shortlisted:** {len(by('shortlisted')) + len(by('added')) + len(by('assessed_skip')) + len(by('checks_failed'))}"
        f", **already in the pipeline:** {len(by('already_in_pipeline'))}, **added to the pipeline:** {len(by('added'))}",
        f"- **Cost:** ${run.usage.get('cost_usd', 0):.4f} ({run.usage.get('calls', 0)} model calls)",
    ]
    if run.errors:
        lines += ["", "## Board errors", *[f"- {e}" for e in run.errors]]
    sections = [
        ("Added to the pipeline", "added", True),
        ("Shortlisted, waiting for assessment" if not dry_run else "Would be assessed", "shortlisted", False),
        ("Assessed: not a fit", "assessed_skip", True),
        ("Assessed: hard checks failed, not added", "checks_failed", False),
        ("Already in the pipeline", "already_in_pipeline", False),
        ("Skipped by triage", "triaged_out", False),
    ]
    for title, stage, with_fit in sections:
        rows = sorted(by(stage), key=lambda o: o.score or 0, reverse=True)
        if not rows:
            continue
        lines += ["", f"## {title}", "", "| Company | Role | Location | Score | " + ("Fit | " if with_fit else "") + "Why |",
                  "|---|---|---|---|" + ("---|" if with_fit else "") + "---|"]
        for o in rows:
            fit = f"{o.fit} | " if with_fit else ""
            why = o.reason.replace("|", "/").replace("\n", " ")[:200]
            lines.append(f"| {o.posting.company} | [{o.posting.title}]({o.posting.url}) | {o.posting.location} | "
                         f"{o.score if o.score is not None else ''} | {fit}{why} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Find new roles that fit the CV on company job boards.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch, screen and triage only; write nothing but the report.")
    parser.add_argument("--max-assess", type=int, default=5, help="Most Build 0 assessments this run (default 5).")
    parser.add_argument("--max-triage", type=int, default=40, help="Most triage calls this run (default 40); the rest wait.")
    parser.add_argument("--model", default=MODEL, help=f"Model for triage and assessment (default {MODEL}).")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Folder with watchlist.toml and profile.toml.")
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("PIPELINE_DB", store.DEFAULT_DB)))
    parser.add_argument("--cv", type=Path, default=Path(os.environ.get("CV_PATH", DEFAULT_CV)))
    args = parser.parse_args()

    for needed in (args.config / "watchlist.toml", args.config / "profile.toml", args.cv):
        if not needed.exists():
            sys.exit(f"Missing {needed}")
    run = scout(args.config, args.db, args.cv, args.model, args.max_assess, args.dry_run, max_triage=args.max_triage)
    report = write_report(run, PROJECT / "data_private" / "scout" / "runs", args.dry_run)
    print(f"{run.fetched} fetched, {run.new} new, {run.count('filtered_out')} filtered out, {run.count('duplicate')} duplicates, "
          f"{run.count('triage_deferred')} deferred, "
          f"{run.count('triaged_out')} skipped by triage, {run.count('already_in_pipeline')} already in the pipeline, {run.count('added')} added, "
          f"{run.count('shortlisted')} still shortlisted. Cost ${run.usage.get('cost_usd', 0):.4f}")
    for e in run.errors:
        print(f"Board error: {e}")
    print(f"Report: {report}")


if __name__ == "__main__":
    main()
