"""Build data_public/pipeline_demo.db: a synthetic job pipeline for the public demo.

Every company, role and person here is made up. Dates are relative to the day
the script runs, so a rebuilt demo always has recent activity, stale roles and
due or overdue next actions. All writes go through store.py.

Run from the agentic-pm-lab folder:

    uv run python -m build1_mcp_server.make_demo_db
"""

import argparse
from datetime import date, timedelta
from pathlib import Path

from . import store

DEFAULT_OUT = store.PROJECT_DIR / "data_public" / "pipeline_demo.db"


def fit(verdict, score, summary, matches, gaps, days_ago):
    return {
        "verdict": verdict, "score": score, "summary": summary,
        "matches": [{"title": t, "detail": d} for t, d in matches],
        "gaps": [{"title": t, "detail": d} for t, d in gaps],
        "days_ago": days_ago, "source": "Job spec vs demo CV",
    }


# Each role: tracker fields, its stage history as (days_ago, stage, note), and
# optional fit, notes (days_ago, text), next action (text, due in N days) and archive reason.
ROLES = [
    {
        "id": "harbourline-health-pm-integrations", "company": "Harbourline Health",
        "title": "Senior PM, Integrations", "track": "integrations", "channel": "Referral", "flagged": True,
        "history": [(24, "saved", "Referred by a former colleague"), (21, "applied", "Applied with tailored CV"),
                    (12, "talking", "Recruiter screen booked"), (4, "interviewing", "First round passed")],
        "fit": fit("strong", 86, "Integration platform work maps closely to their partner API roadmap.",
                   [("Integration platforms", "Built a partner API platform and marketplace from scratch."),
                    ("Regulated data", "Delivered compliance-heavy integrations end to end.")],
                   [("Healthcare standards", "No hands-on HL7 or FHIR experience.")], 22),
        "notes": [(12, "Hiring manager is Priya, Head of Platform. Cares about partner onboarding time."),
                  (4, "Second round is a case study on partner onboarding. 60 minutes, panel of three.")],
        "next": ("Prepare the partner onboarding case study", 1),
    },
    {
        "id": "tidewater-payments-principal-pm", "company": "Tidewater Payments",
        "title": "Principal Product Manager, Platform", "track": "general", "channel": "Recruiter", "flagged": True,
        "history": [(40, "applied", "Recruiter submitted CV"), (30, "talking", "Intro call"),
                    (18, "interviewing", "Onsite scheduled"), (2, "offer", "Verbal offer")],
        "fit": fit("strong", 88, "Platform ownership and pricing experience match the brief.",
                   [("Platform strategy", "Owned a shared platform used by several product teams."),
                    ("Monetisation", "Set pricing and packaging for API products.")],
                   [("Payments domain", "No card-scheme or acquiring background.")], 38),
        "notes": [(2, "Verbal offer. Written offer expected this week. Start date is the open question.")],
        "next": ("Reply on start date and notice period", -1),
    },
    {
        "id": "kestrel-cloud-group-pm-platform", "company": "Kestrel Cloud",
        "title": "Group Product Manager, Developer Platform", "track": "ai", "channel": "Recruiter",
        "history": [(15, "applied", "Applied via recruiter"), (6, "talking", "Recruiter call went well")],
        "fit": fit("good", 74, "Strong developer-platform match; their AI agent roadmap is a stretch.",
                   [("Developer platforms", "Shipped SDKs and provisioning APIs for third-party developers."),
                    ("Team leadership", "Led engineers and a PM across strategy and delivery.")],
                   [("AI agents in production", "Agent work so far is internal tooling, not customer-facing.")], 14),
        "notes": [(6, "Recruiter says the team is 3 squads, 22 engineers. Hiring manager joined from a hyperscaler.")],
        "next": ("Send portfolio links to the recruiter", 2),
    },
    {
        "id": "vellum-legal-ai-solutions-lead", "company": "Vellum Legal",
        "title": "AI Solutions Lead", "track": "solutions", "channel": "Inbound",
        "history": [(10, "saved", "Inbound message from founder"), (5, "talking", "Founder call")],
        "fit": fit("stretch", 55, "Strong customer-facing delivery; little legal-domain experience.",
                   [("Customer discovery", "Ran discovery workshops and POCs with enterprise clients.")],
                   [("Legal domain", "No legal-tech or document-review background."),
                    ("Hands-on ML", "Role expects prompt and evaluation work daily.")], 9),
        "notes": [(5, "Founder wants someone to run pilots with three law firms in the first quarter.")],
    },
    {
        "id": "lumen-data-senior-pm-ai-platform", "company": "Lumen Data",
        "title": "Senior Product Manager, AI Platform", "track": "ai", "channel": "Direct",
        "history": [(9, "saved", "Found on careers page"), (7, "applied", "Applied online")],
        "fit": fit("stretch", 45, "Good SaaS and data fit, but no shipped LLM features or PM management.",
                   [("B2B SaaS", "Eight years in analytics SaaS.")],
                   [("LLM features in production", "Only an AI pilot so far."),
                    ("Managing PMs", "Has not managed other product managers.")], 8),
        "next": ("Wait for a reply, then chase the recruiter", 5),
    },
    {
        "id": "granite-bank-pm-identity", "company": "Granite Bank",
        "title": "Product Manager, Identity and Access", "track": "integrations", "channel": "Direct",
        "history": [(13, "applied", "Applied online")],
        "fit": fit("good", 72, "Identity API delivery is directly relevant; banking regulation is new.",
                   [("Identity APIs", "Delivered OAuth and SAML identity APIs and a product-wide migration.")],
                   [("Banking regulation", "No PSD2 or FCA-regulated product experience.")], 12),
        "next": ("Follow up with the hiring manager on LinkedIn", -3),
    },
    {
        "id": "quillstack-staff-pm-developer-tools", "company": "Quillstack",
        "title": "Staff Product Manager, Developer Tools", "track": "ai", "channel": "Direct",
        "history": [(26, "applied", "Applied online")],
        "fit": fit("good", 70, "Developer-tools experience fits; the staff-level bar is high.",
                   [("Developer experience", "Built an integration framework that cut onboarding time by 30%.")],
                   [("Code review and CI/CD", "No direct ownership of CI/CD or code-review tooling.")], 25),
    },
    {
        "id": "pinecrest-hr-product-builder", "company": "Pinecrest HR",
        "title": "Senior PM, Product Builder", "track": "ai", "channel": "Direct",
        "history": [(33, "applied", "Applied online")],
        "fit": fit("good", 68, "Builder profile fits; HR domain and independent production delivery are thinner.",
                   [("AI-assisted building", "Builds prototypes and internal tools with AI coding tools.")],
                   [("HR tech", "No HR product experience.")], 32),
    },
    {
        "id": "saltmarsh-logistics-pm-api", "company": "Saltmarsh Logistics",
        "title": "Product Manager, Public API", "track": "integrations", "channel": "Inbound",
        "history": [(1, "saved", "Recruiter sent the spec")],
        "next": ("Read the spec and decide whether to apply", 3),
    },
    {
        "id": "corvid-robotics-ai-pm", "company": "Corvid Robotics",
        "title": "AI Product Manager, Fleet Autonomy", "track": "ai", "channel": "Direct",
        "history": [(45, "applied", "Applied online"), (20, "closed", "Rejected after screening")],
        "fit": fit("weak", 35, "Robotics and autonomy depth required; little overlap.",
                   [("Platform thinking", "Comfortable with complex technical platforms.")],
                   [("Robotics", "No robotics or autonomy experience."),
                    ("ML research", "Role partners daily with research scientists.")], 44),
    },
    {
        "id": "orbitly-senior-pm-growth", "company": "Orbitly",
        "title": "Senior Product Manager, Growth", "track": "general", "channel": "Recruiter",
        "history": [(38, "talking", "Recruiter intro"), (29, "closed", "Withdrew: consumer growth focus")],
    },
    {
        "id": "meadowsoft-pm-marketplace", "company": "Meadowsoft",
        "title": "Product Manager, Marketplace", "track": "integrations", "channel": "Direct",
        "history": [(11, "saved", "Saved from a job board")],
        "archive": "Duplicate of another listing",
    },
]


def iso(days_ago: int, today: date) -> str:
    return (today - timedelta(days=days_ago)).isoformat()


def build(out: Path, today: date | None = None) -> dict:
    today = today or date.today()
    if out.exists():
        out.unlink()
    with store.connect(out) as conn:
        for r in ROLES:
            fields = {
                "company": r["company"], "title": r["title"], "track": r["track"], "channel": r["channel"],
                "date_added": iso(r["history"][0][0], today), "next_action": None,
                "flagged": r.get("flagged", False), "job_url": None, "spec_url": None, "cv_url": None,
            }
            for days_ago, stage, note in r["history"]:
                store.upsert_role(conn, r["id"], fields | {"stage": stage}, updated_at=iso(days_ago, today), note=note, actor="demo")
            if "fit" in r:
                f = r["fit"]
                store.add_fit_assessment(conn, r["id"], f | {"assessed": iso(f["days_ago"], today)}, actor="demo")
            for days_ago, text in r.get("notes", []):
                store.add_note(conn, r["id"], text, created_at=iso(days_ago, today), actor="demo")
            if "next" in r:
                action, due_in = r["next"]
                store.set_next_action(conn, r["id"], action, due_date=(today + timedelta(days=due_in)).isoformat(), actor="demo")
            if "archive" in r:
                store.archive_role(conn, r["id"], r["archive"], actor="demo")
        return store.pipeline_summary(conn, today=today)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the synthetic demo pipeline database.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"Output file (default: {DEFAULT_OUT})")
    args = parser.parse_args()
    summary = build(args.out)
    print(f"Built {args.out}")
    print(f"{summary['total']} roles ({summary['archived']} archived): {summary['by_stage']}")
    print(f"{len(summary['flagged'])} flagged, {len(summary['stale'])} stale, {len(summary['due'])} with a due next action")


if __name__ == "__main__":
    main()
