"""The hand-written agent loop for the spec qualifier.

Given a CV and a job spec, the agent reads the CV through the tools in
tools.py and returns a FitAssessment (schema.py), plus the results of the
hard checks in checks.py. Every model call goes through LLMLog, so tokens,
cost and latency are recorded.

Run from the agentic-pm-lab folder:

    uv run python -m build0_spec_qualifier.agent --cv data_public/demo_cv.md --spec data_public/demo_spec.md
"""

import argparse
import json
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Assess how well a CV fits a job spec.")
    parser.add_argument("--cv", required=True, type=Path, help="CV as Markdown or plain text.")
    parser.add_argument("--spec", required=True, type=Path, help="Job spec as Markdown or plain text.")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--effort", default=EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    args = parser.parse_args()

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


if __name__ == "__main__":
    main()
