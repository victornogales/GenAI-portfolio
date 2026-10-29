"""Pydantic models for the fit assessment.

FitAssessment is the structured output the agent returns. Pass it to
client.messages.parse(output_format=FitAssessment) and read
response.parsed_output.

Field order matters: the model writes fields top to bottom, so the
per-requirement verdicts come before the overall score and recommendation.
That way the score is based on the evidence instead of the other way round.
"""

from typing import Literal

from pydantic import BaseModel, Field

Importance = Literal["must_have", "nice_to_have"]
Verdict = Literal["met", "partial", "missing"]
Recommendation = Literal["apply", "apply_with_caveats", "skip"]


class Evidence(BaseModel):
    quote: str = Field(
        description="Text copied exactly from the CV, word for word. "
        "checks.py rejects any quote that does not appear in the CV."
    )
    location: str = Field(
        description="Where the quote is in the CV, e.g. 'Experience: Acme Corp, 2021-2024'."
    )


class RequirementAssessment(BaseModel):
    requirement: str = Field(
        description="One requirement from the job spec, in the spec's own words."
    )
    importance: Importance = Field(
        description="must_have if the spec says required/essential or similar, "
        "otherwise nice_to_have."
    )
    verdict: Verdict = Field(
        description="met: the CV clearly shows it. partial: related or weaker "
        "experience. missing: nothing in the CV supports it."
    )
    evidence: list[Evidence] = Field(
        description="Quotes from the CV that support the verdict. "
        "At least one for met or partial; empty for missing."
    )
    reasoning: str = Field(
        description="One or two sentences explaining the verdict."
    )


class FitAssessment(BaseModel):
    role_title: str = Field(description="The job title from the spec.")
    company: str | None = Field(
        description="The hiring company, or null if the spec doesn't name it."
    )
    requirements: list[RequirementAssessment] = Field(
        description="Every requirement in the spec, one entry each, in the order they appear. "
        "Include nice-to-have, bonus and preferred items as well as required ones."
    )
    strengths: list[str] = Field(
        description="The CV's strongest selling points for this role, most important first."
    )
    gaps: list[str] = Field(
        description="What is missing or weak, most serious first. Empty if there are no gaps."
    )
    fit_score: int = Field(
        ge=0,
        le=100,
        description="Overall fit from 0 to 100. Missing must-haves weigh far more "
        "than missing nice-to-haves.",
    )
    recommendation: Recommendation = Field(
        description="apply: strong fit. apply_with_caveats: worth applying but "
        "address the gaps. skip: one or more must-haves are missing."
    )
    summary: str = Field(
        description="Two or three sentences a recruiter could read on their own."
    )
