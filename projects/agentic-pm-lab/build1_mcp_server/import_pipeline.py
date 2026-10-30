"""One-off import of the job pipeline from a JSON export into SQLite.

The export comes from the Product Manager Job Pipeline tracker (the
"applications" collection): {"applications": [{"id": ..., "company": ...}, ...]}.
Each entry is validated first; if any entry is invalid nothing is written.
Running the import again is safe: unchanged roles are skipped, changed ones
are updated (stage changes go into stage_history), and a fit assessment is
only added once per role, date and source.

Run from the agentic-pm-lab folder:

    uv run python -m build1_mcp_server.import_pipeline data_private/pipeline_export.json
    uv run python -m build1_mcp_server.import_pipeline data_private/pipeline_export.json --dry-run
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import store


class FitItem(BaseModel):
    title: str
    detail: str


class Fit(BaseModel):
    verdict: Literal[store.VERDICTS]
    score: int = Field(ge=0, le=100)
    summary: str = ""
    matches: list[FitItem] = []
    gaps: list[FitItem] = []
    assessed: str
    source: str | None = None


class Application(BaseModel):
    """One tracker entry. Unknown fields are ignored so new tracker fields don't break the import."""

    model_config = ConfigDict(extra="ignore")

    id: str
    company: str
    role: str
    status: Literal[store.STAGES]
    track: Literal[store.TRACKS] | None = None
    channel: Literal[store.CHANNELS] | None = None
    date: str | None = None
    next: str | None = None
    flag: bool = False
    job_url: str | None = None
    spec_url: str | None = None
    cv_url: str | None = None
    updated: str | None = None
    fit: Fit | None = None

    def role_fields(self) -> dict:
        """Map tracker field names onto store column names."""
        return {
            "company": self.company,
            "title": self.role,
            "stage": self.status,
            "track": self.track,
            "channel": self.channel,
            "date_added": self.date,
            "next_action": self.next,
            "flagged": self.flag,
            "job_url": self.job_url,
            "spec_url": self.spec_url,
            "cv_url": self.cv_url,
        }


def load(path: Path) -> list[Application]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw["applications"] if isinstance(raw, dict) else raw
    apps, errors = [], []
    for i, entry in enumerate(entries):
        try:
            apps.append(Application.model_validate(entry))
        except ValidationError as exc:
            errors.append(f"entry {i} ({entry.get('id', '?')}): {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}")
    if errors:
        raise ValueError("Invalid entries, nothing imported:\n  " + "\n  ".join(errors))
    duplicates = {a.id for a in apps if sum(b.id == a.id for b in apps) > 1}
    if duplicates:
        raise ValueError(f"Duplicate ids, nothing imported: {sorted(duplicates)}")
    return apps


def import_apps(apps: list[Application], db_path: Path) -> dict:
    counts = {"added": 0, "updated": 0, "unchanged": 0, "fits_added": 0}
    with store.connect(db_path) as conn:
        for app in apps:
            # Roles never edited in the tracker have no "updated"; their tracker date is the last activity.
            stamp = app.updated or app.date
            counts[store.upsert_role(conn, app.id, app.role_fields(), updated_at=stamp)] += 1
            if app.fit and store.add_fit_assessment(conn, app.id, app.fit.model_dump()):
                counts["fits_added"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Import the job pipeline from a JSON export.")
    parser.add_argument("export", type=Path, help="JSON export of the tracker's applications.")
    parser.add_argument("--db", type=Path, default=store.DEFAULT_DB, help=f"SQLite file (default: {store.DEFAULT_DB})")
    parser.add_argument("--dry-run", action="store_true", help="Validate and summarise without writing.")
    args = parser.parse_args()

    try:
        apps = load(args.export)
    except ValueError as exc:
        sys.exit(str(exc))

    by_stage = {s: sum(a.status == s for a in apps) for s in store.STAGES}
    print(f"{len(apps)} roles, {sum(a.fit is not None for a in apps)} with a fit assessment")
    print("By stage: " + ", ".join(f"{s} {n}" for s, n in by_stage.items() if n))
    if args.dry_run:
        print("Dry run: nothing written.")
        return

    counts = import_apps(apps, args.db)
    print(
        f"Imported into {args.db}: {counts['added']} added, {counts['updated']} updated, "
        f"{counts['unchanged']} unchanged, {counts['fits_added']} fit assessments added"
    )


if __name__ == "__main__":
    main()
