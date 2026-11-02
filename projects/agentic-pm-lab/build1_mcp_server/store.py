"""SQLite access for the job pipeline. This is the only module that writes to the database.

Tables:
    roles             one row per role, keyed by the tracker's id (e.g. "telnyx-devex-plg")
    stage_history     every stage change, oldest first
    notes             dated free-text notes per role
    fit_assessments   fit assessments per role, newest by assessed date
    audit             one row per change: when, who (actor), what and to which role

Every write function takes an open connection and leaves committing to the
caller, so a whole import or tool call succeeds or fails as one transaction.
Each write that changes something adds its audit row in that same
transaction, so a change and its audit row are saved together or not at all.
`actor` says who made it: "mcp" (a server tool), "import" or "demo".
Read functions return plain dicts and lists, ready to send back as JSON.
Archived roles are left out of reads unless include_archived=True.
"""

import json
import re
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

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY,
    at       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    role_id  TEXT,
    details  TEXT NOT NULL DEFAULT '{{}}'
);

CREATE INDEX IF NOT EXISTS idx_roles_stage ON roles(stage);
CREATE INDEX IF NOT EXISTS idx_audit_role ON audit(role_id);
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


def upsert_role(
    conn: sqlite3.Connection,
    role_id: str,
    fields: dict,
    updated_at: str | None = None,
    note: str = "Imported",
    actor: str = "import",
) -> str:
    """Insert or update a role. Returns "added", "updated" or "unchanged".

    A stage change is recorded in stage_history, with `note`; a new role gets
    a first history row with no from_stage.
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
        _record_stage(conn, role_id, None, values["stage"], stamp, note)
        _audit(conn, actor, "add_role", role_id, {"fields": values})
        return "added"

    if all(existing[k] == v for k, v in values.items()):
        return "unchanged"
    assignments = ", ".join(f"{k} = ?" for k in values)
    conn.execute(
        f"UPDATE roles SET {assignments}, updated_at = ? WHERE id = ?",
        (*values.values(), stamp, role_id),
    )
    if existing["stage"] != values["stage"]:
        _record_stage(conn, role_id, existing["stage"], values["stage"], stamp, note)
    changed = {k: [existing[k], v] for k, v in values.items() if existing[k] != v}
    _audit(conn, actor, "update_role", role_id, {"changed": changed})
    return "updated"


def add_fit_assessment(conn: sqlite3.Connection, role_id: str, fit: dict, actor: str = "import") -> bool:
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
    if cursor.rowcount != 1:
        return False
    _audit(conn, actor, "record_fit_assessment", role_id,
           {k: fit.get(k) for k in ("verdict", "score", "assessed", "source")})
    return True


# Writes used by the server's tools. Each validates its input, raises
# ValueError with a message the model can act on, and returns the role's full
# record (as get_role) so the caller sees the result. Archived roles can only
# be read or restored.

MAX_NOTE_LENGTH = 5000


def add_role(
    conn: sqlite3.Connection,
    company: str,
    title: str,
    stage: str = "saved",
    track: str | None = None,
    channel: str | None = None,
    job_url: str | None = None,
    spec_url: str | None = None,
    next_action: str | None = None,
    today: date | None = None,
    actor: str = "mcp",
) -> dict:
    """Add a new role. Its id is made from the company and title, e.g. "acme-platform-pm"."""
    company, title = _required(company, "company"), _required(title, "title")
    _check_choice(stage, STAGES, "stage")
    _check_choice(track, TRACKS, "track")
    _check_choice(channel, CHANNELS, "channel")
    for name, url in (("job_url", job_url), ("spec_url", spec_url)):
        if url and not url.startswith(("https://", "http://")):
            raise ValueError(f"{name} must be a web link starting with https://")
    duplicate = conn.execute(
        "SELECT id FROM roles WHERE lower(company) = lower(?) AND lower(title) = lower(?) AND archived_at IS NULL",
        (company, title),
    ).fetchone()
    if duplicate:
        raise ValueError(f"{company} / {title} is already in the pipeline as {duplicate['id']!r}.")

    role_id = _unique_id(conn, f"{company} {title}")
    stamp = now()
    fields = {
        "company": company, "title": title, "stage": stage, "track": track, "channel": channel,
        "date_added": (today or date.today()).isoformat(), "next_action": _optional(next_action),
        "flagged": False, "job_url": job_url or None, "spec_url": spec_url or None, "cv_url": None,
    }
    columns = ", ".join(("id", *ROLE_FIELDS, "created_at", "updated_at"))
    conn.execute(
        f"INSERT INTO roles ({columns}) VALUES ({', '.join('?' * (len(ROLE_FIELDS) + 3))})",
        (role_id, *(int(fields[k]) if k == "flagged" else fields[k] for k in ROLE_FIELDS), stamp, stamp),
    )
    _record_stage(conn, role_id, None, stage, stamp, "Added")
    _audit(conn, actor, "add_role", role_id, {"fields": {k: v for k, v in fields.items() if v not in (None, False)}})
    return get_role(conn, role_id)


def update_stage(
    conn: sqlite3.Connection, role_id: str, stage: str, note: str | None = None, actor: str = "mcp"
) -> dict:
    """Move a role to a new stage and record it in the history. Moving to the current stage changes nothing."""
    _check_choice(stage, STAGES, "stage")
    role = _active_role(conn, role_id)
    if role["stage"] != stage:
        stamp = now()
        conn.execute("UPDATE roles SET stage = ?, updated_at = ? WHERE id = ?", (stage, stamp, role_id))
        _record_stage(conn, role_id, role["stage"], stage, stamp, _optional(note))
        _audit(conn, actor, "update_stage", role_id, {"from": role["stage"], "to": stage, "note": _optional(note)})
    return get_role(conn, role_id)


def add_note(
    conn: sqlite3.Connection, role_id: str, text: str, created_at: str | None = None, actor: str = "mcp"
) -> dict:
    """Add a dated note to a role, dated now unless created_at is given (used for imports and demo data)."""
    _active_role(conn, role_id)
    text = _required(text, "text")
    if len(text) > MAX_NOTE_LENGTH:
        raise ValueError(f"Note is {len(text)} characters; the limit is {MAX_NOTE_LENGTH}.")
    conn.execute("INSERT INTO notes (role_id, text, created_at) VALUES (?, ?, ?)", (role_id, text, created_at or now()))
    _audit(conn, actor, "add_note", role_id, {"text": text})
    return get_role(conn, role_id)


def set_next_action(
    conn: sqlite3.Connection, role_id: str, action: str, due_date: str | None = None, actor: str = "mcp"
) -> dict:
    """Set the role's next step and, optionally, when it's due (YYYY-MM-DD). An empty action clears both.
    Setting the same action and date again changes nothing."""
    role = _active_role(conn, role_id)
    action = _optional(action)
    if due_date:
        try:
            due_date = date.fromisoformat(due_date).isoformat()
        except ValueError:
            raise ValueError(f"due_date {due_date!r} isn't a date. Use YYYY-MM-DD.") from None
    if action is None:
        due_date = None
    before = {"action": role["next_action"], "due_date": role["next_due"]}
    after = {"action": action, "due_date": due_date or None}
    if after != before:
        conn.execute(
            "UPDATE roles SET next_action = ?, next_due = ?, updated_at = ? WHERE id = ?",
            (after["action"], after["due_date"], now(), role_id),
        )
        _audit(conn, actor, "set_next_action", role_id, {"before": before, "after": after})
    return get_role(conn, role_id)


def archive_role(conn: sqlite3.Connection, role_id: str, reason: str, actor: str = "mcp") -> dict:
    """Hide a role from lists, search and the summary. Nothing is deleted; restore_role brings it back."""
    _active_role(conn, role_id)
    reason = _required(reason, "reason")
    stamp = now()
    conn.execute(
        "UPDATE roles SET archived_at = ?, archive_reason = ?, updated_at = ? WHERE id = ?",
        (stamp, reason, stamp, role_id),
    )
    _audit(conn, actor, "archive_role", role_id, {"reason": reason})
    return get_role(conn, role_id)


def restore_role(conn: sqlite3.Connection, role_id: str, actor: str = "mcp") -> dict:
    """Undo archive_role."""
    role = _existing_role(conn, role_id)
    if role["archived_at"] is None:
        raise ValueError(f"Role {role_id!r} isn't archived.")
    conn.execute(
        "UPDATE roles SET archived_at = NULL, archive_reason = NULL, updated_at = ? WHERE id = ?",
        (now(), role_id),
    )
    _audit(conn, actor, "restore_role", role_id, {"archive_reason": role["archive_reason"]})
    return get_role(conn, role_id)


def record_fit_assessment(
    conn: sqlite3.Connection,
    role_id: str,
    verdict: str,
    score: int,
    summary: str,
    source: str,
    matches: list[dict] | None = None,
    gaps: list[dict] | None = None,
    assessed: str | None = None,
    actor: str = "mcp",
    today: date | None = None,
) -> dict:
    """Save a fit assessment made elsewhere (by Claude in a chat, or a Build 0 run).

    Saving the same one again (same role, assessed date and source) changes nothing.
    """
    _active_role(conn, role_id)
    _check_choice(verdict, VERDICTS, "verdict")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError(f"score must be a whole number from 0 to 100, not {score!r}.")
    summary, source = _required(summary, "summary"), _required(source, "source")
    items = {}
    for name, value in (("matches", matches), ("gaps", gaps)):
        items[name] = []
        for item in value or []:
            title, detail = (item.get("title") or "").strip(), (item.get("detail") or "").strip()
            if not title or not detail:
                raise ValueError(f"Each of {name} needs a title and a detail.")
            items[name].append({"title": title, "detail": detail})
    try:
        assessed = date.fromisoformat(assessed).isoformat() if assessed else (today or date.today()).isoformat()
    except ValueError:
        raise ValueError(f"assessed {assessed!r} isn't a date. Use YYYY-MM-DD.") from None
    fit = {"verdict": verdict, "score": score, "summary": summary, "assessed": assessed, "source": source, **items}
    add_fit_assessment(conn, role_id, fit, actor=actor)
    return get_role(conn, role_id)


def _existing_role(conn, role_id: str) -> sqlite3.Row:
    role = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    if role is None:
        raise ValueError(f"No role with id {role_id!r}. Use search_roles to find the right id.")
    return role


def _active_role(conn, role_id: str) -> sqlite3.Row:
    role = _existing_role(conn, role_id)
    if role["archived_at"] is not None:
        raise ValueError(f"Role {role_id!r} is archived. Restore it before changing it.")
    return role


def _unique_id(conn, text: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].rstrip("-") or "role"
    role_id, n = base, 2
    while conn.execute("SELECT 1 FROM roles WHERE id = ?", (role_id,)).fetchone():
        role_id, n = f"{base}-{n}", n + 1
    return role_id


def _check_choice(value, allowed: tuple, name: str) -> None:
    if value is not None and value not in allowed:
        raise ValueError(f"Unknown {name} {value!r}. Use one of: {', '.join(allowed)}")


def _required(value: str | None, name: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{name} can't be empty.")
    return value


def _optional(value: str | None) -> str | None:
    return (value or "").strip() or None


def _audit(conn, actor: str, action: str, role_id: str | None, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit (at, actor, action, role_id, details) VALUES (?, ?, ?, ?, ?)",
        (now(), actor, action, role_id, json.dumps(details, ensure_ascii=False, default=str)),
    )


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


def audit_log(conn: sqlite3.Connection, limit: int = 20, role_id: str | None = None) -> list[dict]:
    """The most recent changes, newest first, optionally for one role."""
    sql, params = "SELECT id, at, actor, action, role_id, details FROM audit", []
    if role_id:
        sql += " WHERE role_id = ?"
        params.append(role_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, MAX_LIMIT)))
    return [dict(r) | {"details": json.loads(r["details"])} for r in conn.execute(sql, params)]


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
