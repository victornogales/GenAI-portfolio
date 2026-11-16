"""Fetch open roles from company job boards on Greenhouse, Lever and Ashby.

All three publish a free, public, read-only JSON API for each company's board,
meant for exactly this kind of use. Each fetcher returns a list of Posting in
one common shape. Uses only the standard library.
"""

import html
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

USER_AGENT = "agentic-pm-lab job scout (personal job search)"
TIMEOUT_SECONDS = 20
PAUSE_SECONDS = 1.0  # between companies, to be polite to the boards


@dataclass
class Posting:
    source: str  # "greenhouse" | "lever" | "ashby"
    company: str  # display name from the watchlist
    external_id: str
    title: str
    location: str
    url: str
    description: str  # plain text
    posted_at: str | None = None  # ISO date, when the board gives one

    @property
    def key(self) -> str:
        """Stable id across runs: board, company slug is in the url, plus the board's own job id."""
        return f"{self.source}:{self.external_id}"


class SourceError(Exception):
    pass


def html_to_text(raw: str) -> str:
    """Turn job-description HTML (possibly entity-escaped, as Greenhouse sends it) into plain text."""
    text = html.unescape(raw or "")
    if "<" in text and ">" in text and "&lt;" in (raw or ""):
        text = html.unescape(text)
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise SourceError(f"{url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SourceError(f"{url} failed: {exc}") from exc


def _iso_date(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):  # Lever: milliseconds since the epoch
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
    return str(value)[:10]


def parse_greenhouse(data: dict, company: str) -> list[Posting]:
    return [
        Posting(
            source="greenhouse",
            company=company,
            external_id=str(j["id"]),
            title=j["title"].strip(),
            location=(j.get("location") or {}).get("name", ""),
            url=j["absolute_url"],
            description=html_to_text(j.get("content", "")),
            posted_at=_iso_date(j.get("first_published") or j.get("updated_at")),
        )
        for j in data.get("jobs", [])
    ]


def parse_lever(data: list, company: str) -> list[Posting]:
    postings = []
    for j in data:
        categories = j.get("categories") or {}
        locations = categories.get("allLocations") or [categories.get("location", "")]
        parts = [j.get("descriptionPlain", "")]
        for section in j.get("lists", []):
            parts.append(f"{section.get('text', '')}\n{html_to_text(section.get('content', ''))}")
        parts.append(j.get("additionalPlain", ""))
        postings.append(
            Posting(
                source="lever",
                company=company,
                external_id=str(j["id"]),
                title=j["text"].strip(),
                location=" / ".join(filter(None, locations)) + (f" ({j['workplaceType']})" if j.get("workplaceType") else ""),
                url=j["hostedUrl"],
                description="\n\n".join(p.strip() for p in parts if p and p.strip()),
                posted_at=_iso_date(j.get("createdAt")),
            )
        )
    return postings


def parse_ashby(data: dict, company: str) -> list[Posting]:
    postings = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        places = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        workplace = j.get("workplaceType") or ("Remote" if j.get("isRemote") else "")
        postings.append(
            Posting(
                source="ashby",
                company=company,
                external_id=str(j["id"]),
                title=j["title"].strip(),
                location=" / ".join(filter(None, places)) + (f" ({workplace})" if workplace else ""),
                url=j["jobUrl"],
                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
                posted_at=_iso_date(j.get("publishedAt")),
            )
        )
    return postings


BOARDS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", parse_greenhouse),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json", parse_lever),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{slug}", parse_ashby),
}


def fetch_company(ats: str, slug: str, company: str, get_json=_get_json) -> list[Posting]:
    if ats not in BOARDS:
        raise SourceError(f"Unknown job board {ats!r}. Use one of: {', '.join(BOARDS)}")
    url_template, parse = BOARDS[ats]
    return parse(get_json(url_template.format(slug=slug)), company)


def fetch_all(watchlist: list[dict], get_json=_get_json, pause: float = PAUSE_SECONDS) -> tuple[list[Posting], list[str]]:
    """Fetch every company on the watchlist. A failing company is reported, not fatal."""
    postings, errors = [], []
    for i, entry in enumerate(watchlist):
        if i and pause:
            time.sleep(pause)
        try:
            postings += fetch_company(entry["ats"], entry["slug"], entry.get("name", entry["slug"]), get_json)
        except (SourceError, KeyError, TypeError) as exc:
            errors.append(f"{entry.get('name', entry.get('slug'))}: {exc}")
    return postings, errors
