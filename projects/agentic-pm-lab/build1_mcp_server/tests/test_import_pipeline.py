import json
import sqlite3

import pytest

from build1_mcp_server.import_pipeline import import_apps, load

FIT = {
    "verdict": "good",
    "score": 72,
    "summary": "Solid platform match.",
    "matches": [{"title": "APIs", "detail": "Built REST APIs."}],
    "gaps": [{"title": "Domain", "detail": "No telecoms."}],
    "assessed": "2026-10-23",
    "source": "spec vs CV",
}


def app(**overrides):
    entry = {
        "id": "acme-pm",
        "company": "Acme",
        "role": "Product Manager",
        "status": "applied",
        "track": "ai",
        "channel": "Direct",
        "date": "2026-10-20",
        "next": "Wait for reply.",
        "flag": False,
        "fit": FIT,
    }
    return entry | overrides


def write_export(tmp_path, entries):
    path = tmp_path / "export.json"
    path.write_text(json.dumps({"applications": entries}), encoding="utf-8")
    return path


def rows(db, sql):
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchall()


def test_import_adds_roles_history_and_fit(tmp_path):
    db = tmp_path / "p.db"
    counts = import_apps(load(write_export(tmp_path, [app(), app(id="beta-pm", fit=None)])), db)
    assert counts == {"added": 2, "updated": 0, "unchanged": 0, "fits_added": 1}
    assert rows(db, "SELECT id, title, stage FROM roles ORDER BY id") == [
        ("acme-pm", "Product Manager", "applied"),
        ("beta-pm", "Product Manager", "applied"),
    ]
    assert len(rows(db, "SELECT * FROM stage_history")) == 2
    assert rows(db, "SELECT score, json_array_length(gaps) FROM fit_assessments") == [(72, 1)]


def test_reimport_changes_nothing(tmp_path):
    db = tmp_path / "p.db"
    export = write_export(tmp_path, [app()])
    import_apps(load(export), db)
    counts = import_apps(load(export), db)
    assert counts == {"added": 0, "updated": 0, "unchanged": 1, "fits_added": 0}
    assert len(rows(db, "SELECT * FROM fit_assessments")) == 1


def test_stage_change_is_recorded_in_history(tmp_path):
    db = tmp_path / "p.db"
    import_apps(load(write_export(tmp_path, [app()])), db)
    counts = import_apps(load(write_export(tmp_path, [app(status="talking")])), db)
    assert counts["updated"] == 1
    assert rows(db, "SELECT from_stage, to_stage FROM stage_history ORDER BY id") == [
        (None, "applied"),
        ("applied", "talking"),
    ]


def test_new_fit_assessment_is_added_alongside_the_old_one(tmp_path):
    db = tmp_path / "p.db"
    import_apps(load(write_export(tmp_path, [app()])), db)
    newer = FIT | {"assessed": "2026-10-29", "score": 80}
    counts = import_apps(load(write_export(tmp_path, [app(fit=newer)])), db)
    assert counts["fits_added"] == 1
    assert rows(db, "SELECT score FROM fit_assessments ORDER BY assessed") == [(72,), (80,)]


def test_invalid_entry_stops_the_whole_import(tmp_path):
    export = write_export(tmp_path, [app(), app(id="bad", status="ghosted")])
    with pytest.raises(ValueError, match="bad"):
        load(export)


def test_duplicate_ids_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="Duplicate"):
        load(write_export(tmp_path, [app(), app()]))


def test_unknown_tracker_fields_are_ignored(tmp_path):
    db = tmp_path / "p.db"
    counts = import_apps(load(write_export(tmp_path, [app(salary="£100k")])), db)
    assert counts["added"] == 1
