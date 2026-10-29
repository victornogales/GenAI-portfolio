"""Tools the agent can call to read and inspect a CV.

The CV is a Markdown or plain-text file. Markdown headings (#, ##, ...) split
it into sections, and a section includes its sub-sections. A CV without
headings is treated as one section called "CV".

    cv = CVTools.from_file("data_public/demo_cv.md")
    client.messages.create(..., tools=TOOLS)
    result = cv.run(block.name, block.input)   # for each tool_use block

run() returns a string for the tool_result. It raises ToolError for bad
input; send that back with is_error=True so the model can correct itself.
"""

import difflib
import re
from datetime import date
from pathlib import Path


class ToolError(Exception):
    pass


TOOLS = [
    {
        "name": "list_sections",
        "description": "Lists the section headings in the CV, in order, with how many lines each "
        "has. Call this first to see how the CV is organised before reading sections.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "read_section",
        "description": "Returns the full text of one CV section, with line numbers. Use the exact "
        "heading from list_sections (matching ignores case). Read the sections relevant to a "
        "requirement before giving it a verdict.",
        "input_schema": {
            "type": "object",
            "properties": {
                "heading": {"type": "string", "description": "Section heading, as listed by list_sections."}
            },
            "required": ["heading"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "search_cv",
        "description": "Finds CV lines that contain every word in the query (ignoring case; words "
        "match from their start, so 'manag' finds 'managed'), with their line numbers and sections. Use it to look for a skill, tool, domain or job title "
        "anywhere in the CV, e.g. 'stakeholder management' or 'SQL'. No results means those exact "
        "words are absent, not that the experience is: try synonyms before marking a requirement missing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "One or more words to look for."}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "verify_quote",
        "description": "Checks that a quote appears word for word in the CV (whitespace differences "
        "are ignored, case is not). Call it on every quote before using it as evidence. If the "
        "quote isn't found, it returns the closest CV line so you can copy it exactly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "quote": {"type": "string", "description": "The exact text you intend to quote."}
            },
            "required": ["quote"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "calculate_experience",
        "description": "Works out years of experience from date ranges. Returns the length of each "
        "period and a total in which overlapping periods are counted once. Use it for any "
        "'N+ years of X' requirement instead of doing date arithmetic yourself.",
        "input_schema": {
            "type": "object",
            "properties": {
                "periods": {
                    "type": "array",
                    "description": "The relevant roles or periods from the CV.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "e.g. 'Senior PM, Acme'."},
                            "start": {"type": "string", "description": "YYYY-MM or YYYY."},
                            "end": {"type": "string", "description": "YYYY-MM, YYYY or 'present'."},
                        },
                        "required": ["label", "start", "end"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["periods"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

MAX_SEARCH_RESULTS = 20
HEADING = re.compile(r"^#{1,6}\s+(.*\S)\s*$")


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _parse_month(value: str, is_end: bool) -> int:
    """'2021-03' -> months since year 0. A bare year means Jan (start) or Dec (end)."""
    value = value.strip().lower()
    if value in ("present", "current", "now"):
        today = date.today()
        return today.year * 12 + today.month
    match = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?", value)
    if not match:
        raise ToolError(f"Can't read date {value!r}. Use YYYY-MM, YYYY or 'present'.")
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else (12 if is_end else 1)
    if not 1 <= month <= 12:
        raise ToolError(f"Month out of range in {value!r}.")
    return year * 12 + month


class CVTools:
    def __init__(self, cv_text: str):
        self.lines = cv_text.splitlines()
        self.sections = self._split_sections()

    @classmethod
    def from_file(cls, path: str | Path) -> "CVTools":
        return cls(Path(path).read_text(encoding="utf-8"))

    def run(self, name: str, tool_input: dict) -> str:
        handlers = {
            "list_sections": lambda: self.list_sections(),
            "read_section": lambda: self.read_section(tool_input["heading"]),
            "search_cv": lambda: self.search_cv(tool_input["query"]),
            "verify_quote": lambda: self.verify_quote(tool_input["quote"]),
            "calculate_experience": lambda: self.calculate_experience(tool_input["periods"]),
        }
        if name not in handlers:
            raise ToolError(f"Unknown tool {name!r}.")
        return handlers[name]()

    # Tools

    def list_sections(self) -> str:
        return "\n".join(
            f"{heading} ({end - start} lines)" for heading, start, end in self.sections
        )

    def read_section(self, heading: str) -> str:
        for name, start, end in self.sections:
            if name.lower() == heading.strip().lower():
                return "\n".join(f"{i + 1}: {self.lines[i]}" for i in range(start, end))
        available = ", ".join(name for name, _, _ in self.sections)
        raise ToolError(f"No section called {heading!r}. Sections: {available}")

    def search_cv(self, query: str) -> str:
        words = query.lower().split()
        if not words:
            raise ToolError("Query is empty.")
        # Match from the start of a word, so "AI" doesn't hit "trial" but "manag" hits "managed".
        patterns = [re.compile(r"(?<!\w)" + re.escape(word), re.IGNORECASE) for word in words]
        hits = [
            f"{i + 1} [{self._section_of(i)}]: {line.strip()}"
            for i, line in enumerate(self.lines)
            if all(p.search(line) for p in patterns)
        ]
        if not hits:
            return f"No lines contain all of: {', '.join(words)}"
        shown = hits[:MAX_SEARCH_RESULTS]
        more = len(hits) - len(shown)
        return "\n".join(shown) + (f"\n... and {more} more. Narrow the query." if more else "")

    def verify_quote(self, quote: str) -> str:
        target = _normalise(quote)
        if not target:
            raise ToolError("Quote is empty.")
        # Search line by line first, then across the whole text for quotes spanning lines.
        for i, line in enumerate(self.lines):
            if target in _normalise(line):
                return f"FOUND on line {i + 1} [{self._section_of(i)}]"
        if target in _normalise("\n".join(self.lines)):
            return "FOUND (spans more than one line)"
        candidates = [_normalise(line) for line in self.lines if line.strip()]
        closest = difflib.get_close_matches(target, candidates, n=1, cutoff=0.0)
        hint = f" Closest CV line: {closest[0]!r}" if closest else ""
        return f"NOT FOUND: this text is not in the CV.{hint}"

    def calculate_experience(self, periods: list[dict]) -> str:
        if not periods:
            raise ToolError("Give at least one period.")
        spans, out = [], []
        for p in periods:
            start = _parse_month(p["start"], is_end=False)
            end = _parse_month(p["end"], is_end=True)
            if end < start:
                raise ToolError(f"{p['label']}: end is before start.")
            months = end - start + 1
            spans.append((start, end))
            out.append(f"{p['label']}: {months / 12:.1f} years")
        # Merge overlapping or touching periods so time is counted once.
        merged: list[list[int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        total = sum(end - start + 1 for start, end in merged)
        out.append(f"Total, overlaps counted once: {total / 12:.1f} years")
        return "\n".join(out)

    # Helpers

    def _split_sections(self) -> list[tuple[str, int, int]]:
        """(heading, first line index after it, end index) for each section.

        A section runs until the next heading of the same or higher level, so
        "## Experience" includes the "### Role" sub-sections under it.
        """
        starts = [
            (i, len(line) - len(line.lstrip("#")), m.group(1))
            for i, line in enumerate(self.lines)
            if (m := HEADING.match(line))
        ]
        if not starts:
            return [("CV", 0, len(self.lines))]
        sections = []
        if starts[0][0] > 0:
            sections.append(("Header", 0, starts[0][0]))
        for n, (i, level, heading) in enumerate(starts):
            end = next(
                (j for j, lvl, _ in starts[n + 1 :] if lvl <= level), len(self.lines)
            )
            sections.append((heading, i + 1, end))
        return sections

    def _section_of(self, line_index: int) -> str:
        """The most specific section containing this line."""
        matches = [
            (end - start, heading)
            for heading, start, end in self.sections
            if start - 1 <= line_index < end
        ]
        return min(matches)[1] if matches else "CV"
