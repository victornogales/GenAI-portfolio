"""SQLite access for the job pipeline. This is the only module that writes to the database.

Tables:
    roles             one row per role, keyed by the tracker's id (e.g. "telnyx-devex-plg")
    stage_history     every stage change, oldest first
    notes             dated free-text notes per role
    fit_assessments   fit assessments per role, newest by assessed date

Every write function takes an open connection and leaves committing to the
caller, so a whole import or tool call succeeds or fails as one transaction.
Read functions return plain dicts and lists, ready to send back as JSON.
Archived roles are left out of reads unless include_archived=True.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

STAGES = ("saved", "applied", "talking", "interviewing", "offer", "closed")
OPEN_STAGES = STAGES[:-1]
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


# Reads

# One row per role, with its latest fit assessment and its last activity date:
# the newest of its own update, a stage change or a note.
_ROLE_SUMMARY = """
SELECT r.id, r.company, r.title, r.stage, r.track, r.channel, r.date_added,
       r.next_action, r.next_due, r.flagged, r.archived_at,
       f.verdict AS fit_verdict, f.score AS fit_score,
       MAX(substr(r.updated_at, 1, 10),
           COALESCE((SELECT substr(MAX(h.changed_at), 1, 10) FROM stage_history h WHERE h.role_id = r.id), ''),
           COALESCE((SELECT substr(MAX(n.created_at), 1, 10) FROM notes n WHERE n.role_id = r.id), '')
       ) AS last_activity
FROM roles r
LEFT JOIN fit_assessments f ON f.id = (
    SELECT id FROM fit_assessments WHERE role_id = r.id ORDER BY assessed DESC, id DESC LIMIT 1
)
"""
MAX_LIMIT = 100


def list_roles(
    conn: sqlite3.Connection,
    stage: str | None = None,
    limit: int = 20,
    include_archived: bool = False,
) -> list[dict]:
    """Roles, flagged first, then most recently active. Optionally only one stage."""
    if stage is not None and stage not in STAGES:
        raise ValueError(f"Unknown stage {stage!r}. Use one of: {', '.join(STAGES)}")
    where, params = _visibility(include_archived)
    if stage:
        where.append("r.stage = ?")
        params.append(stage)
    return _summaries(conn, where, params, limit)


def search_roles(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    include_archived: bool = False,
) -> list[dict]:
    """Roles where every word of the query appears in the company, title, next action or a note."""
    words = query.split()
    if not words:
        raise ValueError("Query is empty.")
    where, params = _visibility(include_archived)
    for word in words:
        # "!" escapes LIKE's wildcards, so "50%" searches for a literal "50%".
        pattern = "%" + word.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        where.append(
            "(r.company LIKE ? ESCAPE '!' OR r.title LIKE ? ESCAPE '!' OR r.next_action LIKE ? ESCAPE '!'"
            " OR EXISTS (SELECT 1 FROM notes n WHERE n.role_id = r.id AND n.text LIKE ? ESCAPE '!'))"
        )
        params += [pattern] * 4
    return _summaries(conn, where, params, limit)


def get_role(conn: sqlite3.Connection, role_id: str) -> dict | None:
    """Everything about one role, archived or not: details, stage history, notes and fit assessments."""
    role = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    if role is None:
        return None
    result = _clean(dict(role))
    result["history"] = [
        dict(r)
        for r in conn.execute(
            "SELECT from_stage, to_stage, changed_at, note FROM stage_history WHERE role_id = ? ORDER BY id",
            (role_id,),
        )
    ]
    result["notes"] = [
        dict(r)
        for r in conn.execute(
            "SELECT id, text, created_at FROM notes WHERE role_id = ? ORDER BY created_at, id", (role_id,)
        )
    ]
    result["fit_assessments"] = [
        dict(r) | {"matches": json.loads(r["matches"]), "gaps": json.loads(r["gaps"])}
        for r in conn.execute(
            """SELECT verdict, score, summary, matches, gaps, assessed, source
               FROM fit_assessments WHERE role_id = ? ORDER BY assessed DESC, id DESC""",
            (role_id,),
        )
    ]
    return result


def pipeline_summary(conn: sqlite3.Connection, stale_days: int = 14, today: date | None = None) -> dict:
    """The pipeline at a glance: counts by stage, flagged roles, stale roles and due next actions.

    A role is stale when it is in an open stage (not closed) and has had no
    activity for stale_days or more.
    """
    today = today or date.today()
    cutoff = (today - timedelta(days=stale_days)).isoformat()
    roles = _summaries(conn, *_visibility(False), limit=None)
    open_roles = [r for r in roles if r["stage"] in OPEN_STAGES]
    due = sorted((r for r in open_roles if r["next_due"]), key=lambda r: r["next_due"])
    archived = conn.execute("SELECT COUNT(*) FROM roles WHERE archived_at IS NOT NULL").fetchone()[0]
    return {
        "today": today.isoformat(),
        "total": len(roles),
        "archived": archived,
        "by_stage": {s: n for s in STAGES if (n := sum(r["stage"] == s for r in roles))},
        "flagged": [_brief(r) for r in open_roles if r["flagged"]],
        "stale": [
            _brief(r) | {"last_activity": r["last_activity"]}
            for r in sorted(open_roles, key=lambda r: r["last_activity"])
            if r["last_activity"] < cutoff
        ],
        "due": [
            _brief(r) | {"next_action": r["next_action"], "next_due": r["next_due"],
                         "overdue": r["next_due"] < today.isoformat()}
            for r in due
        ],
    }


def _visibility(include_archived: bool) -> tuple[list[str], list]:
    return ([] if include_archived else ["r.archived_at IS NULL"]), []


def _summaries(conn, where: list[str], params: list, limit: int | None) -> list[dict]:
    sql = _ROLE_SUMMARY + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY r.flagged DESC, last_activity DESC, r.company"
    if limit is not None:
        sql += " LIMIT ?"
        params = [*params, max(1, min(limit, MAX_LIMIT))]
    return [_clean(dict(r)) for r in conn.execute(sql, params)]


def _clean(row: dict) -> dict:
    row["flagged"] = bool(row["flagged"])
    return row


def _brief(role: dict) -> dict:
    return {"id": role["id"], "company": role["company"], "title": role["title"], "stage": role["stage"]}
