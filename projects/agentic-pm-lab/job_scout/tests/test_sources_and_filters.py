"""Parsers on synthetic job-board data shaped like the real APIs (checked live on 2026-11-16), and the rules."""

from job_scout.filters import screen
from job_scout.sources import Posting, SourceError, fetch_all, html_to_text, parse_ashby, parse_greenhouse, parse_lever

GREENHOUSE = {"jobs": [{
    "id": 7647941003, "title": " Senior Product Manager, Platform ",
    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/7647941003",
    "location": {"name": "London, UK"}, "first_published": "2026-03-01T20:15:57-05:00",
    "content": "&lt;p&gt;&lt;strong&gt;About Acme&lt;/strong&gt;&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Own the API &amp;amp; SDK roadmap&lt;/li&gt;&lt;/ul&gt;",
}]}
LEVER = [{
    "id": "2a55d6c9", "text": "Product Lead, Integrations", "hostedUrl": "https://jobs.lever.co/beta/2a55d6c9",
    "categories": {"location": "London", "allLocations": ["London", "Berlin"]}, "workplaceType": "hybrid",
    "createdAt": 1792767640768, "descriptionPlain": "Beta builds integrations.",
    "lists": [{"text": "What you'll do", "content": "<li>Lead the partner API</li><li>Ship connectors</li>"}],
    "additionalPlain": "Hybrid, two days a week.",
}]
ASHBY = {"jobs": [
    {"id": "0c32", "title": "Group Product Manager", "jobUrl": "https://jobs.ashbyhq.com/gamma/0c32", "location": "London",
     "secondaryLocations": [{"location": "Remote (UK)"}], "workplaceType": "Hybrid", "isListed": True,
     "publishedAt": "2026-05-12T09:09:20.157+00:00", "descriptionPlain": "Gamma is hiring a GPM."},
    {"id": "hidden", "title": "Unlisted role", "jobUrl": "https://jobs.ashbyhq.com/gamma/hidden", "location": "London",
     "isListed": False, "descriptionPlain": "x"},
]}

PROFILE = {
    "titles": {"include": ["product manager", "product lead"], "exclude": ["intern", "product marketing"]},
    "locations": {"include": ["london", "uk", "remote"], "exclude": ["remote - us"]},
}


def test_parse_greenhouse_unescapes_and_strips_html():
    [p] = parse_greenhouse(GREENHOUSE, "Acme")
    assert (p.key, p.title, p.location, p.posted_at) == ("greenhouse:7647941003", "Senior Product Manager, Platform", "London, UK", "2026-03-01")
    assert "About Acme" in p.description and "- Own the API & SDK roadmap" in p.description and "<" not in p.description


def test_parse_lever_joins_sections_and_locations():
    [p] = parse_lever(LEVER, "Beta")
    assert p.location == "London / Berlin (hybrid)"
    assert p.description.startswith("Beta builds integrations.") and "- Lead the partner API" in p.description
    assert p.description.endswith("Hybrid, two days a week.")
    assert p.posted_at == "2026-10-23"


def test_parse_ashby_skips_unlisted_and_adds_secondary_locations():
    [p] = parse_ashby(ASHBY, "Gamma")
    assert (p.title, p.location, p.posted_at) == ("Group Product Manager", "London / Remote (UK) (Hybrid)", "2026-05-12")


def test_fetch_all_reports_a_failing_board_and_carries_on():
    def fake_get(url):
        if "boards-api.greenhouse.io" in url:
            return GREENHOUSE
        raise SourceError(f"{url} returned HTTP 404")

    postings, errors = fetch_all(
        [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}, {"name": "Gone", "ats": "lever", "slug": "gone"},
         {"name": "Odd", "ats": "workday", "slug": "odd"}],
        get_json=fake_get, pause=0,
    )
    assert [p.company for p in postings] == ["Acme"]
    assert len(errors) == 2 and "404" in errors[0] and "Unknown job board 'workday'" in errors[1]


def posting(title, location="London"):
    return Posting("greenhouse", "Acme", "1", title, location, "https://x.example/1", "")


def test_screen_rules():
    assert screen(posting("Senior Product Manager"), PROFILE).keep
    assert screen(posting("Product Lead", ""), PROFILE).keep  # no location: left to triage
    assert screen(posting("Product Manager", "Remote (UK)"), PROFILE).keep
    assert not screen(posting("Product Marketing Manager"), PROFILE).keep
    assert not screen(posting("Product Manager Intern"), PROFILE).keep
    assert not screen(posting("Account Executive"), PROFILE).keep
    assert "outside the target areas" in screen(posting("Product Manager", "New York, NY"), PROFILE).reason
    assert not screen(posting("Product Manager", "Remote - US"), PROFILE).keep
    assert not screen(posting("Product Manager", "Duke Street, Durham, NC"), PROFILE).keep  # "uk" is a whole word only


def test_html_to_text_handles_plain_text():
    assert html_to_text("Just text & more") == "Just text & more"
