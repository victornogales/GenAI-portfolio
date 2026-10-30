"""Runs the golden eval set and writes a dated report to results/.

Each golden/*.json file is one case: a CV, a job spec and the expected
answer. For each case the agent runs, and its answer is compared with the
expected one:

- every expected requirement is found, with the right verdict and importance
- the recommendation is one of the accepted ones
- the fit score is inside the accepted range
- every hard check in checks.py passes

A case passes only if all of these hold. Cases whose "expected" is null (no
reviewed answer key yet) are skipped. Run from the agentic-pm-lab folder:

    uv run python -m build0_spec_qualifier.evals.run_evals --model claude-haiku-5-5
    uv run python -m build0_spec_qualifier.evals.run_evals --case demo_spec1 --repeats 3

Reports for runs that include real (non-demo) cases are named private_* and
are gitignored, because they quote requirements from real job specs.
"""

import argparse
import difflib
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from shared.llm_log import LLMLog

from ..agent import EFFORT, MODEL, AgentError, run_agent
from ..schema import FitAssessment

EVALS_DIR = Path(__file__).resolve().parent
GOLDEN_DIR = EVALS_DIR / "golden"
RESULTS_DIR = EVALS_DIR / "results"
PROJECT_DIR = EVALS_DIR.parent.parent

# How similar two requirement texts must be (0-1) to count as the same requirement.
MATCH_THRESHOLD = 0.6


@dataclass
class RequirementResult:
    expected: str
    matched_to: str | None
    expected_verdict: str
    actual_verdict: str | None
    expected_importance: str
    actual_importance: str | None

    @property
    def verdict_ok(self) -> bool:
        return self.actual_verdict == self.expected_verdict

    @property
    def importance_ok(self) -> bool:
        return self.actual_importance == self.expected_importance


@dataclass
class CaseResult:
    case: str
    repeat: int
    passed: bool = False
    error: str | None = None
    requirements: list[RequirementResult] = field(default_factory=list)
    extra_requirements: list[str] = field(default_factory=list)
    recommendation: str | None = None
    recommendation_ok: bool = False
    fit_score: int | None = None
    fit_score_ok: bool = False
    failed_checks: list[str] = field(default_factory=list)
    turns: int = 0
    usage: dict = field(default_factory=dict)

    @property
    def verdicts_correct(self) -> int:
        return sum(r.verdict_ok for r in self.requirements)


def match_requirements(expected: list[dict], actual: list) -> tuple[list[RequirementResult], list[str]]:
    """Pair each expected requirement with the most similar actual one (each used once)."""
    pairs = sorted(
        (
            (difflib.SequenceMatcher(None, e["requirement"].lower(), a.requirement.lower()).ratio(), i, j)
            for i, e in enumerate(expected)
            for j, a in enumerate(actual)
        ),
        reverse=True,
    )
    match: dict[int, int] = {}
    used: set[int] = set()
    for score, i, j in pairs:
        if score >= MATCH_THRESHOLD and i not in match and j not in used:
            match[i] = j
            used.add(j)

    results = []
    for i, e in enumerate(expected):
        a = actual[match[i]] if i in match else None
        results.append(
            RequirementResult(
                expected=e["requirement"],
                matched_to=a.requirement if a else None,
                expected_verdict=e["verdict"],
                actual_verdict=a.verdict if a else None,
                expected_importance=e["importance"],
                actual_importance=a.importance if a else None,
            )
        )
    extra = [a.requirement for j, a in enumerate(actual) if j not in used]
    return results, extra


def run_case(path: Path, repeat: int, run_id: str, model: str, effort: str) -> CaseResult:
    golden = json.loads(path.read_text(encoding="utf-8"))
    expected = golden["expected"]
    result = CaseResult(case=path.stem, repeat=repeat)
    log = LLMLog(run_id=f"{run_id}-{path.stem}-{repeat}")
    try:
        agent = run_agent(
            cv_text=(PROJECT_DIR / golden["cv"]).read_text(encoding="utf-8"),
            spec_text=(PROJECT_DIR / golden["spec"]).read_text(encoding="utf-8"),
            log=log,
            model=model,
            effort=effort,
        )
    except AgentError as exc:
        result.error = str(exc)
        result.usage = log.totals()
        return result

    a: FitAssessment = agent.assessment
    result.requirements, result.extra_requirements = match_requirements(expected["requirements"], a.requirements)
    result.recommendation = a.recommendation
    result.recommendation_ok = a.recommendation in expected["recommendation"]
    result.fit_score = a.fit_score
    low, high = expected["fit_score_range"]
    result.fit_score_ok = low <= a.fit_score <= high
    result.failed_checks = [c.name for c in agent.checks if not c.passed]
    result.turns = agent.turns
    result.usage = agent.usage
    result.passed = (
        all(r.verdict_ok and r.importance_ok for r in result.requirements)
        and not result.extra_requirements
        and result.recommendation_ok
        and result.fit_score_ok
        and not result.failed_checks
    )
    return result


def write_report(results: list[CaseResult], model: str, effort: str, started: datetime, private: bool) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    name = f"{'private_' if private else ''}{started:%Y-%m-%d_%H%M}_{model}"
    total = lambda key: sum(r.usage.get(key, 0) for r in results)
    passed = sum(r.passed for r in results)
    reqs = [req for r in results for req in r.requirements]

    lines = [
        f"# Eval report: {started:%Y-%m-%d %H:%M}",
        "",
        f"- **Model:** {model} (effort {effort})",
        f"- **Passed:** {passed} of {len(results)} runs",
        f"- **Verdicts correct:** {sum(q.verdict_ok for q in reqs)} of {len(reqs)}",
        f"- **Cost:** ${total('cost_usd'):.4f}"
        + ("" if all(r.usage.get("cost_complete", True) for r in results) else " (incomplete: unpriced model)"),
        f"- **Tokens:** {total('input_tokens')} input, {total('output_tokens')} output, "
        f"{total('cache_read_tokens')} cache read, {total('cache_write_tokens')} cache write",
        f"- **Time:** {total('latency_ms') / 1000:.0f}s in model calls",
        "",
        "| Case | Run | Result | Verdicts | Recommendation | Score | Checks | Turns | Cost |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.error:
            lines.append(f"| {r.case} | {r.repeat} | ERROR | | | | | | ${r.usage.get('cost_usd', 0):.4f} |")
            continue
        lines.append(
            f"| {r.case} | {r.repeat} | {'PASS' if r.passed else 'FAIL'} "
            f"| {r.verdicts_correct}/{len(r.requirements)} "
            f"| {r.recommendation}{'' if r.recommendation_ok else ' ✗'} "
            f"| {r.fit_score}{'' if r.fit_score_ok else ' ✗'} "
            f"| {'all pass' if not r.failed_checks else ', '.join(r.failed_checks)} "
            f"| {r.turns} | ${r.usage.get('cost_usd', 0):.4f} |"
        )

    failures = [r for r in results if not r.passed]
    if failures:
        lines += ["", "## What went wrong"]
    for r in failures:
        lines += ["", f"### {r.case}, run {r.repeat}"]
        if r.error:
            lines.append(f"- Agent error: {r.error}")
            continue
        for q in r.requirements:
            if q.matched_to is None:
                lines.append(f"- Not assessed: {q.expected!r}")
            elif not q.verdict_ok:
                lines.append(f"- {q.expected!r}: expected **{q.expected_verdict}**, got **{q.actual_verdict}**")
            elif not q.importance_ok:
                lines.append(f"- {q.expected!r}: expected {q.expected_importance}, got {q.actual_importance}")
        for extra in r.extra_requirements:
            lines.append(f"- Not in the answer key: {extra!r}")
        if not r.recommendation_ok:
            lines.append(f"- Recommendation {r.recommendation!r} isn't an accepted one")
        if not r.fit_score_ok:
            lines.append(f"- Fit score {r.fit_score} is outside the accepted range")
        for check in r.failed_checks:
            lines.append(f"- Failed check: {check}")

    report = RESULTS_DIR / f"{name}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    raw = [asdict(r) | {"verdicts_correct": r.verdicts_correct} for r in results]
    (RESULTS_DIR / f"{name}.json").write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the golden eval set.")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--effort", default=EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--case", default="*", help="Golden file name pattern, e.g. demo_spec1 or demo_*.")
    parser.add_argument("--repeats", type=int, default=1, help="Runs per case, to measure consistency.")
    args = parser.parse_args()

    matched = sorted(GOLDEN_DIR.glob(f"{args.case}.json"))
    # A case without a reviewed answer key can't be graded, so it's skipped.
    cases = [p for p in matched if json.loads(p.read_text(encoding="utf-8")).get("expected")]
    for path in sorted(set(matched) - set(cases)):
        print(f"{path.stem}... SKIP (no answer key yet)")
    if not cases:
        sys.exit(f"No golden cases with an answer key match {args.case!r} in {GOLDEN_DIR}")

    started = datetime.now()
    run_id = f"eval-{started:%Y%m%d-%H%M}"
    results = []
    for path in cases:
        for repeat in range(1, args.repeats + 1):
            print(f"{path.stem} run {repeat}...", end=" ", flush=True)
            result = run_case(path, repeat, run_id, args.model, args.effort)
            print("ERROR" if result.error else "PASS" if result.passed else "FAIL")
            results.append(result)

    private = any(not p.stem.startswith("demo_") for p in cases)
    report = write_report(results, args.model, args.effort, started, private)
    print(f"\n{sum(r.passed for r in results)}/{len(results)} passed. Report: {report}")
    sys.exit(0 if all(r.passed for r in results) else 1)


if __name__ == "__main__":
    main()
