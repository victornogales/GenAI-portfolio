"""The hand-written agent loop for the spec qualifier.

Given a CV and a job spec, the agent reads the CV through the tools in
tools.py and returns a FitAssessment (schema.py), plus the results of the
hard checks in checks.py. Every model call goes through LLMLog, so tokens,
cost and latency are recorded.

Run from the agentic-pm-lab folder:

    uv run python -m build0_spec_qualifier.agent --cv data_public/demo_cv.md --spec data_public/demo_spec.md

Add --record ROLE_ID to save the assessment to that role in the Build 1
pipeline (only if every hard check passes; audited as actor "build0").
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from anthropic import transform_schema

from shared.llm_log import LLMLog

from .checks import CheckResult, run_checks
from .schema import FitAssessment
from .tools import TOOLS, CVTools, ToolError

MODEL = "claude-opus-5-5"
EFFORT = "high"
MAX_TOKENS = 16000
MAX_TURNS = 25

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")

# If the model declines on safety grounds, the API re-runs the request on a
# recommended fallback model instead of returning a refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AgentError(Exception):
    pass


@dataclass
class AgentResult:
    assessment: FitAssessment
    turns: int
    usage: dict
    checks: list[CheckResult]


def run_agent(
    cv_text: str,
    spec_text: str,
    log: LLMLog | None = None,
    model: str = MODEL,
    effort: str = EFFORT,
) -> AgentResult:
    log = log or LLMLog()
    cv = CVTools(cv_text)
    messages = [
        {
            "role": "user",
            "content": f"<job_spec>\n{spec_text}\n</job_spec>\n\n"
            "Assess how well the candidate's CV fits this job spec.",
        }
    ]

    for turn in range(1, MAX_TURNS + 1):
        response = log.create(
            label=f"turn_{turn}",
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": transform_schema(FitAssessment)},
            },
            # Each turn resends the whole conversation; caching makes the repeated part cheap.
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise AgentError(f"Model refused (category: {category}).")
        if response.stop_reason == "max_tokens":
            raise AgentError(f"Response hit max_tokens ({MAX_TOKENS}) on turn {turn}.")

        # Keep the history append-only: the full assistant content goes back as-is.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "tool_use":
            messages.append({"role": "user", "content": run_tools(cv, response.content)})
            continue
        if response.stop_reason == "pause_turn":
            continue

        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            assessment = FitAssessment.model_validate_json(text)
        except ValueError as exc:
            raise AgentError(f"Final answer didn't match the schema: {exc}") from exc
        return AgentResult(
            assessment=assessment,
            turns=turn,
            usage=log.totals(),
            checks=run_checks(assessment, cv_text),
        )

    raise AgentError(f"No final answer after {MAX_TURNS} turns.")


def run_tools(cv: CVTools, content: list) -> list[dict]:
    """Run every tool call in one response and return all results in one list."""
    results = []
    for block in content:
        if block.type != "tool_use":
            continue
        try:
            output, is_error = cv.run(block.name, block.input), False
        except ToolError as exc:
            output, is_error = f"Error: {exc}", True
        results.append(
            {"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error}
        )
    return results


# Recording a run in the Build 1 pipeline (--record). The pipeline's verdicts
# come from Build 0's 0-100 score, in bands that match the tracker's own data.
VERDICT_BANDS = ((80, "strong"), (65, "good"), (45, "stretch"), (0, "weak"))


def to_pipeline_fit(assessment: FitAssessment, model: str, spec_name: str) -> dict:
    """Map a FitAssessment onto the pipeline's fit format: met requirements become matches,
    partial and missing ones become gaps, must-haves first."""
    verdict = next(v for floor, v in VERDICT_BANDS if assessment.fit_score >= floor)
    reqs = sorted(assessment.requirements, key=lambda r: r.importance != "must_have")
    matches = [
        {"title": r.requirement, "detail": r.reasoning + (f' Evidence: "{r.evidence[0].quote}"' if r.evidence else "")}
        for r in reqs if r.verdict == "met"
    ]
    gaps = [
        {"title": r.requirement, "detail": f"{r.verdict.capitalize()} ({r.importance.replace('_', '-')}). {r.reasoning}"}
        for r in reqs if r.verdict != "met"
    ]
    return {
        "verdict": verdict,
        "score": assessment.fit_score,
        "summary": f"{assessment.summary} Recommendation: {assessment.recommendation.replace('_', ' ')}.",
        "matches": matches,
        "gaps": gaps,
        "source": f"Build 0 run ({model}) on {spec_name}, all hard checks passed",
    }


def check_role(role_id: str, db_path: Path) -> None:
    """Fail before a paid run if the role can't be recorded against."""
    from build1_mcp_server import store

    if not db_path.exists():
        raise AgentError(f"No pipeline database at {db_path}.")
    with store.connect(db_path) as conn:
        role = store.get_role(conn, role_id)
    if role is None:
        raise AgentError(f"No role with id {role_id!r} in {db_path}. Add it to the pipeline first.")
    if role["archived_at"]:
        raise AgentError(f"Role {role_id!r} is archived. Restore it first.")


def record_result(result: AgentResult, role_id: str, db_path: Path, model: str, spec_name: str) -> dict:
    """Save a run's assessment to the pipeline, audited as actor "build0". Refuses if any hard check failed."""
    from build1_mcp_server import store

    failed = [c.name for c in result.checks if not c.passed]
    if failed:
        raise AgentError(f"Not recorded: hard checks failed ({', '.join(failed)}).")
    fit = to_pipeline_fit(result.assessment, model, spec_name)
    with store.connect(db_path) as conn:
        return store.record_fit_assessment(conn, role_id, **fit, actor="build0")


def main() -> None:
    parser = argparse.ArgumentParser(description="Assess how well a CV fits a job spec.")
    parser.add_argument("--cv", required=True, type=Path, help="CV as Markdown or plain text.")
    parser.add_argument("--spec", required=True, type=Path, help="Job spec as Markdown or plain text.")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--effort", default=EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--record", metavar="ROLE_ID", help="Save the assessment to this pipeline role if all checks pass.")
    parser.add_argument("--db", type=Path, help="Pipeline database for --record (default: PIPELINE_DB or data_private/pipeline.db).")
    args = parser.parse_args()

    db_path = None
    if args.record:
        from build1_mcp_server import store

        db_path = args.db or Path(os.environ.get("PIPELINE_DB", store.DEFAULT_DB))
        try:
            check_role(args.record, db_path)
        except AgentError as exc:
            sys.exit(str(exc))

    log = LLMLog(run_id=f"agent-{args.cv.stem}-{args.spec.stem}")
    result = run_agent(
        cv_text=args.cv.read_text(encoding="utf-8"),
        spec_text=args.spec.read_text(encoding="utf-8"),
        log=log,
        model=args.model,
        effort=args.effort,
    )
    print(result.assessment.model_dump_json(indent=2))
    print("\nChecks:")
    for check in result.checks:
        print(f"  {'PASS' if check.passed else 'FAIL'}  {check.name}  {check.detail}".rstrip())
    print(f"\n{result.turns} turns | {json.dumps(result.usage)}")

    if args.record:
        try:
            role = record_result(result, args.record, db_path, args.model, args.spec.name)
        except (AgentError, ValueError) as exc:
            sys.exit(str(exc))
        fit = role["fit_assessments"][0]
        print(f"\nRecorded in {db_path}: {role['company']}, {fit['verdict']} {fit['score']} ({fit['assessed']})")


if __name__ == "__main__":
    main()
