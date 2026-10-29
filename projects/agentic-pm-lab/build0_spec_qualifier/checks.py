"""Hard, deterministic checks run on the agent's output.

No LLM involved: each check is a rule that a correct FitAssessment must
satisfy whatever the CV and spec are. The schema already guarantees types,
allowed values and the 0-100 score range, so these cover what it can't.

    results = run_checks(assessment, cv_text)
    if not all_passed(results): ...
"""

from collections import Counter
from dataclasses import dataclass

from .schema import FitAssessment
from .tools import _normalise


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


def run_checks(assessment: FitAssessment, cv_text: str) -> list[CheckResult]:
    return [
        check_has_requirements(assessment),
        check_no_duplicate_requirements(assessment),
        check_quotes_in_cv(assessment, cv_text),
        check_evidence_matches_verdict(assessment),
        check_no_apply_with_missing_must_have(assessment),
        check_skip_has_reason(assessment),
        check_gaps_listed(assessment),
    ]


def all_passed(results: list[CheckResult]) -> bool:
    return all(r.passed for r in results)


def check_has_requirements(a: FitAssessment) -> CheckResult:
    return CheckResult("has_requirements", bool(a.requirements), "" if a.requirements else "No requirements assessed.")


def check_no_duplicate_requirements(a: FitAssessment) -> CheckResult:
    counts = Counter(_normalise(r.requirement).lower() for r in a.requirements)
    dupes = [req for req, n in counts.items() if n > 1]
    return CheckResult("no_duplicate_requirements", not dupes, f"Assessed more than once: {dupes}" if dupes else "")


def check_quotes_in_cv(a: FitAssessment, cv_text: str) -> CheckResult:
    """Every quote must appear word for word in the CV (whitespace differences ignored)."""
    cv = _normalise(cv_text)
    bad = [
        f"{r.requirement!r}: {e.quote!r}"
        for r in a.requirements
        for e in r.evidence
        if _normalise(e.quote) not in cv
    ]
    return CheckResult("quotes_in_cv", not bad, "Not in CV: " + "; ".join(bad) if bad else "")


def check_evidence_matches_verdict(a: FitAssessment) -> CheckResult:
    """met and partial need at least one quote; missing must have none."""
    bad = [
        f"{r.requirement!r} is {r.verdict} with {len(r.evidence)} quotes"
        for r in a.requirements
        if (r.verdict == "missing") == bool(r.evidence)
    ]
    return CheckResult("evidence_matches_verdict", not bad, "; ".join(bad))


def check_no_apply_with_missing_must_have(a: FitAssessment) -> CheckResult:
    missing = [r.requirement for r in a.requirements if r.importance == "must_have" and r.verdict == "missing"]
    failed = a.recommendation == "apply" and bool(missing)
    return CheckResult(
        "no_apply_with_missing_must_have",
        not failed,
        f"Recommends apply but must-haves are missing: {missing}" if failed else "",
    )


def check_skip_has_reason(a: FitAssessment) -> CheckResult:
    """skip needs at least one must-have that isn't fully met."""
    weak = [r for r in a.requirements if r.importance == "must_have" and r.verdict != "met"]
    failed = a.recommendation == "skip" and not weak
    return CheckResult(
        "skip_has_reason", not failed, "Recommends skip but every must-have is met." if failed else ""
    )


def check_gaps_listed(a: FitAssessment) -> CheckResult:
    """If any requirement isn't fully met, gaps can't be empty."""
    not_met = [r for r in a.requirements if r.verdict != "met"]
    failed = bool(not_met) and not a.gaps
    return CheckResult(
        "gaps_listed", not failed, f"{len(not_met)} requirements not met but gaps is empty." if failed else ""
    )
