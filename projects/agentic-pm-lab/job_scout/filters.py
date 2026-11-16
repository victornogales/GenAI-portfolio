"""Free, rule-based screening on title and location, before any model call.

Rules come from the profile file:

    [titles]
    include = ["product manager", "product lead"]   # at least one must appear
    exclude = ["intern", "product marketing"]       # none may appear
    [locations]
    include = ["london", "uk", "remote"]            # at least one must appear
    exclude = ["remote - us"]                       # none may appear

Matching ignores case and works on whole words, so "uk" doesn't match "duke".
A posting with no location at all is kept and left to the triage step.
"""

import re
from dataclasses import dataclass

from .sources import Posting


@dataclass
class FilterResult:
    keep: bool
    reason: str


def _matches(phrase: str, text: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)", text.lower()) is not None


def first_match(phrases: list[str], text: str) -> str | None:
    return next((p for p in phrases if _matches(p, text)), None)


def screen(posting: Posting, profile: dict) -> FilterResult:
    titles, locations = profile.get("titles", {}), profile.get("locations", {})
    if bad := first_match(titles.get("exclude", []), posting.title):
        return FilterResult(False, f"title contains '{bad}'")
    if titles.get("include") and not first_match(titles["include"], posting.title):
        return FilterResult(False, "title matches none of the target roles")
    if posting.location:
        if bad := first_match(locations.get("exclude", []), posting.location):
            return FilterResult(False, f"location contains '{bad}'")
        if locations.get("include") and not first_match(locations["include"], posting.location):
            return FilterResult(False, f"location '{posting.location}' is outside the target areas")
    return FilterResult(True, "title and location match")
