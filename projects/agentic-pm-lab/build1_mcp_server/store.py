"""SQLite access for the job pipeline. This is the only module that writes to the database.

Tables:
    roles             one row per role, keyed by the tracker's id (e.g. "telnyx-devex-plg")
    stage_history     every stage change, oldest first
    notes             dated free-text notes per role
    fit_assessments   fit assessments per role, newest by assessed date

Every write function takes an open connection and leaves committing to the
caller, so a whole import or tool call succeeds or fails as one transaction.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

STAGES = ("saved", "applied", "talking", "interviewing", "offer", "closed")
TRACKS = ("ai", "integrations", "general", "solutions")
CHANNELS = ("Direct", "Recruiter", "Inbound", "Referral")
VERDICTS = ("strong", "good", "stretch", "weak")

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_DIR / "data_private" / "pipeline.db"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS roles (
    id              TEXT PRIMARY KEY,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    stage           TEXT NOT NULL CHECK (stage IN {STAGES}),
    track           TEXT CHECK (track IN {TRACKS}),
    channel         TEXT CHECK (channel IN {CHANNELS}),
    date_added      TEXT,
    next_action     TEXT,
    next_due        TEXT,
    flagged         INTEGER NOT NULL DEFAULT 0,
    job_url         TEXT,
    spec_url        TEXT,
    cv_url          TEXT,
    archived_at     TEXT,
    archive_reason  TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_history (
    id          INTEGER PRIMARY KEY,
    role_id     TEXT NOT NULL REFERENCES roles(id),
    from_stage  TEXT,
    to_stage    TEXT NOT NULL CHECK (to_stage IN {STAGES}),
    changed_at  TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    role_id     TEXT NOT NULL REFERENCES roles(id),
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fit_assessments (
    id          INTEGER PRIMARY KEY,
    role_id     TEXT NOT NULL REFERENCES roles(id),
    verdict     TEXT NOT NULL CHECK (verdict IN {VERDICTS}),
    score       INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    summary     TEXT,
    matches     TEXT NOT NULL DEFAULT '[]',
    gaps        TEXT NOT NULL DEFAULT '[]',
    assessed    TEXT NOT NULL,
    source      TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (role_id, assessed, source)
);

CREATE INDEX IF NOT EXISTS idx_roles_stage ON roles(stage);
CREATE INDEX IF NOT EXISTS idx_history_role ON stage_history(role_id);
CREATE INDEX IF NOT EXISTS idx_notes_role ON notes(role_id);
CREATE INDEX IF NOT EXISTS idx_fit_role ON fit_assessments(role_id);
"""

# Fields copied from the tracker as they are; the other columns are managed here.
ROLE_FIELDS = (
    "company", "title", "stage", "track", "channel", "date_added",
    "next_action", "flagged", "job_url", "spec_url", "cv_url",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path | str = DEFAULT_DB) -> Iterator[sqlite3.Connection]:
    """Open the database, create the tables if needed, and commit on success."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_role(conn: sqlite3.Connection, role_id: str, fields: dict, updated_at: str | None = None) -> str:
    """Insert or update a role. Returns "added", "updated" or "unchanged".

    A stage change is recorded in stage_history; a new role gets a first
    history row with no from_stage.
    """
    values = {k: fields.get(k) for k in ROLE_FIELDS}
    values["flagged"] = int(bool(values["flagged"]))
    stamp = updated_at or now()
    existing = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()

    if existing is None:
        columns = ", ".join(("id", *ROLE_FIELDS, "created_at", "updated_at"))
        placeholders = ", ".join("?" * (len(ROLE_FIELDS) + 3))
        conn.execute(
            f"INSERT INTO roles ({columns}) VALUES ({placeholders})",
            (role_id, *values.values(), stamp, stamp),
        )
        _record_stage(conn, role_id, None, values["stage"], stamp, "Imported")
        return "added"

    if all(existing[k] == v for k, v in values.items()):
        return "unchanged"
    assignments = ", ".join(f"{k} = ?" for k in values)
    conn.execute(
        f"UPDATE roles SET {assignments}, updated_at = ? WHERE id = ?",
        (*values.values(), stamp, role_id),
    )
    if existing["stage"] != values["stage"]:
        _record_stage(conn, role_id, existing["stage"], values["stage"], stamp, "Imported")
    return "updated"


def add_fit_assessment(conn: sqlite3.Connection, role_id: str, fit: dict) -> bool:
    """Store a fit assessment. Returns False if the same one (role, date, source) is already stored."""
    cursor = conn.execute(
        """INSERT OR IGNORE INTO fit_assessments
           (role_id, verdict, score, summary, matches, gaps, assessed, source, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            role_id,
            fit["verdict"],
            fit["score"],
            fit.get("summary"),
            json.dumps(fit.get("matches", []), ensure_ascii=False),
            json.dumps(fit.get("gaps", []), ensure_ascii=False),
            fit["assessed"],
            fit.get("source"),
            now(),
        ),
    )
    return cursor.rowcount == 1


def _record_stage(conn, role_id, from_stage, to_stage, changed_at, note):
    conn.execute(
        "INSERT INTO stage_history (role_id, from_stage, to_stage, changed_at, note) VALUES (?, ?, ?, ?, ?)",
        (role_id, from_stage, to_stage, changed_at, note),
    )
