"""Migrating curation work out of an OASIS database (scripts/import_oasis_curation.py)."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

# Column for column what a real OASIS ai4me_jobs.db holds (read with
# PRAGMA table_info from examples/oasis/workspace/ai4me_jobs.db).
OASIS_SCHEMA = """
CREATE TABLE curation_projects (id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL UNIQUE, description TEXT,
    source_kind VARCHAR NOT NULL, source_config JSON NOT NULL, taxonomy JSON NOT NULL, curator_name VARCHAR NOT NULL,
    status VARCHAR NOT NULL, item_count INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
CREATE TABLE curation_items (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id VARCHAR NOT NULL, source_key VARCHAR NOT NULL,
    source_image_uri VARCHAR NOT NULL, crop_uri VARCHAR NOT NULL, file_name VARCHAR NOT NULL, source_label VARCHAR NOT NULL,
    current_label VARCHAR NOT NULL, included BOOLEAN NOT NULL, review_state VARCHAR NOT NULL, row_px INTEGER,
    column_px INTEGER, crop_size INTEGER NOT NULL, region VARCHAR, site VARCHAR, survey_year INTEGER, split VARCHAR,
    model_label VARCHAR, model_confidence FLOAT, top_predictions JSON NOT NULL, quality_score INTEGER,
    quality_flags JSON NOT NULL, quality_decision VARCHAR, quality_reviewed_by VARCHAR, quality_reviewed_at DATETIME,
    metadata_json JSON NOT NULL, revision INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
CREATE TABLE curation_events (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id VARCHAR NOT NULL, item_id INTEGER,
    operation_id VARCHAR, event_type VARCHAR NOT NULL, curator_name VARCHAR NOT NULL, reason TEXT,
    previous_value JSON, new_value JSON, created_at DATETIME NOT NULL);
CREATE TABLE curation_import_operations (id VARCHAR PRIMARY KEY, project_id VARCHAR NOT NULL, status VARCHAR NOT NULL,
    sources JSON NOT NULL, batch_size INTEGER NOT NULL, processed_rows INTEGER NOT NULL, imported_rows INTEGER NOT NULL,
    skipped_rows INTEGER NOT NULL, rejected_rows INTEGER NOT NULL, current_source VARCHAR, error_message TEXT,
    created_at DATETIME NOT NULL, started_at DATETIME, completed_at DATETIME);
CREATE TABLE curation_dataset_versions (id VARCHAR PRIMARY KEY, project_id VARCHAR NOT NULL, version VARCHAR NOT NULL,
    status VARCHAR NOT NULL, event_watermark INTEGER NOT NULL, split_seed INTEGER NOT NULL, item_count INTEGER NOT NULL,
    output_prefix VARCHAR, artifacts JSON NOT NULL, statistics JSON NOT NULL, created_at DATETIME NOT NULL,
    published_at DATETIME);
CREATE TABLE jobs (id VARCHAR PRIMARY KEY, name VARCHAR);
"""
WHEN = "2026-07-23 09:17:07.123456"


def _item(item_id, key, label, *, region="NWHI", uri=None, metadata=None, reviewed_by=None):
    return (item_id, "p-1", key, uri or f"gs://bucket/images/{key}.JPG", f"gs://bucket/crops/{label}/{key}.png",
            f"{key}.png", label, label, 1, "reviewed" if reviewed_by else "unreviewed", 100, 200, 224, region,
            "SITE-1", 2024, None, label, 0.8, json.dumps([{"label": label, "confidence": 0.8, "rank": 1}]),
            None, "[]", "reject" if reviewed_by else None, reviewed_by, WHEN if reviewed_by else None,
            json.dumps(metadata or {"island": "Kure", "annotator": "Alleviate"}), 2, WHEN, WHEN)


@pytest.fixture
def oasis_db(tmp_path):
    path = tmp_path / "ai4me_jobs.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(OASIS_SCHEMA)
    conn.execute("INSERT INTO curation_projects VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
        "p-1", "Pacific T3", "from OASIS", "regional_csv", json.dumps({"dataset_mode": "T3"}), "[]",
        "Project Owner", "draft", 3, WHEN, WHEN))
    items = [
        _item(1, "Hawaii:A.JPG:1:100:200", "TURFH"),
        _item(2, "Hawaii:A.JPG:2:300:400", "ACBR", region="", reviewed_by="Jane Smith"),
        _item(3, "Hawaii:B.JPG:1:5:5", "SAND", uri="gs://bucket/" + "x" * 1200),
    ]
    conn.executemany(f"INSERT INTO curation_items VALUES ({','.join('?' * 29)})", items)
    orphan = list(_item(4, "orphan", "SAND"))
    orphan[1] = "gone"
    conn.execute(f"INSERT INTO curation_items VALUES ({','.join('?' * 29)})", orphan)
    conn.executemany("INSERT INTO curation_events VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (1, "p-1", 1, None, "item_updated", "Project Owner", "fix",
         json.dumps({"current_label": "EMA"}), json.dumps({"current_label": "TURFH"}), WHEN),
        (2, "p-1", 2, "op-1", "bulk_item_updated", "Project Owner", None, None, None, WHEN),
        (3, "p-1", None, "imp-1", "source_import_completed", "Jane Smith", None, None, json.dumps({"imported": 3}), WHEN),
    ])
    conn.execute("INSERT INTO curation_import_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        "imp-1", "p-1", "running", json.dumps([{"name": "Hawaii"}]), 2000, 3, 3, 0, 0, "Hawaii", None, WHEN, WHEN, None))
    conn.commit()
    # Leave the WAL un-checkpointed, as a live OASIS would: the copy must still
    # see every row. (Closing would checkpoint it, so copy the files instead.)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    for suffix in ("", "-wal", "-shm"):
        part = path.with_name(path.name + suffix)
        if part.exists():
            (snapshot / part.name).write_bytes(part.read_bytes())
    conn.close()
    for part in snapshot.iterdir():
        (tmp_path / part.name).write_bytes(part.read_bytes())
    return path


def _digest(path):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.parent.iterdir() if p.name.startswith(path.name)}


def _run(*args):
    from scripts import import_oasis_curation

    return import_oasis_curation.main([str(a) for a in args])


def test_dry_run_reports_without_writing_and_leaves_the_source_alone(library, oasis_db, tmp_path, capsys):
    from appdb import session_scope
    from curation.models import CurationProject

    before = _digest(oasis_db)
    _run(oasis_db, "--report", tmp_path / "report.json")
    report = json.loads((tmp_path / "report.json").read_text())["survey"]

    assert report["counts"] == {"curation_projects": 1, "curation_items": 4, "curation_events": 3,
                                "curation_import_operations": 1}
    assert report["orphans"]["items_without_project"] == 1
    assert report["too_long"] == {"curation_items.source_image_uri > 1000": 1}
    assert report["blank_to_null"] == {"curation_items.region": 1}
    assert set(report["actors"]) == {"Project Owner", "Jane Smith"}
    assert "Dry run" in capsys.readouterr().out
    with session_scope() as s:
        assert s.query(CurationProject).count() == 0
    assert _digest(oasis_db) == before


def test_apply_maps_people_preserves_history_and_is_safe_to_rerun(library, oasis_db):
    from appdb import session_scope
    from auth import service
    from auth.models import User
    from curation.models import CurationEvent, CurationImportOperation, CurationItem, CurationProject

    with session_scope() as s:
        service.create_user(s, username="jane", password="pass-word-1", display_name="Jane S.")

    _run(oasis_db, "--apply", "--user-map", "Jane Smith=jane")

    with session_scope() as s:
        project = s.query(CurationProject).one()
        assert project.name == "Pacific T3" and project.item_count == 2  # the over-long one is skipped
        owner = s.get(User, project.owner_user_id)
        assert owner.username == "legacy.project-owner" and owner.is_active is False and owner.password_hash is None

        items = {i.source_key: i for i in s.query(CurationItem)}
        reviewed = items["Hawaii:A.JPG:2:300:400"]
        assert reviewed.region is None                      # '' became NULL
        assert reviewed.island == "Kure" and reviewed.annotator == "Alleviate"  # promoted from metadata
        assert reviewed.revision == 2 and reviewed.quality_decision == "reject"
        assert reviewed.created_at.year == 2026 and reviewed.created_at.tzinfo is None

        events = {e.event_type: e for e in s.query(CurationEvent)}
        assert events["source_import_completed"].curator_name == "Jane S."   # mapped to the real account
        assert events["source_import_completed"].legacy_actor == "Jane Smith"
        assert events["item_updated"].legacy_actor == "Project Owner"
        assert events["item_updated"].item_id == items["Hawaii:A.JPG:1:100:200"].id
        assert events["item_updated"].previous_value == {"current_label": "EMA"}

        # A half-finished OASIS import is never resumed here.
        assert s.query(CurationImportOperation).one().status == "interrupted"
        counts = (s.query(CurationItem).count(), s.query(CurationEvent).count(), s.query(User).count())

    _run(oasis_db, "--apply", "--user-map", "Jane Smith=jane")
    with session_scope() as s:
        assert (s.query(CurationItem).count(), s.query(CurationEvent).count(), s.query(User).count()) == counts
        assert s.query(CurationProject).count() == 1


def test_oasis_original_splits_are_preserved_and_validated(library, oasis_db):
    from appdb import session_scope
    from curation.models import CurationItem

    with sqlite3.connect(oasis_db) as source:
        source.execute("UPDATE curation_items SET split = 'TEST' WHERE id = 1")
        source.execute("UPDATE curation_items SET split = 'validation' WHERE id = 2")
    _run(oasis_db, "--apply")
    with session_scope() as db:
        assert {item.split for item in db.query(CurationItem)} == {"test", "val"}


def test_oasis_invalid_original_split_is_reported_and_skipped(library, oasis_db):
    from appdb import session_scope
    from curation.models import CurationItem

    with sqlite3.connect(oasis_db) as source:
        source.execute("UPDATE curation_items SET split = 'unknown' WHERE id = 2")
    _run(oasis_db, "--apply")
    with session_scope() as db:
        assert db.query(CurationItem).count() == 1


def test_batch_import_preserves_unsplit_membership_and_event_links(library, oasis_db):
    from appdb import session_scope
    from curation.models import CurationEvent, CurationItem

    with sqlite3.connect(oasis_db) as source:
        source.executemany(f"INSERT INTO curation_items VALUES ({','.join('?' * 29)})",
                           [_item(item_id, f"batch-{item_id}", "ACBR") for item_id in range(10, 1011)])
    _run(oasis_db, "--apply")
    with session_scope() as db:
        assert db.query(CurationItem).count() == 1003
        assert db.query(CurationItem).filter(CurationItem.split.is_not(None)).count() == 0
        original = db.query(CurationItem).filter_by(source_key="Hawaii:A.JPG:1:100:200").one()
        event = db.query(CurationEvent).filter_by(event_type="item_updated").one()
        assert event.item_id == original.id
    _run(oasis_db, "--apply")
    with session_scope() as db:
        assert db.query(CurationItem).count() == 1003


def test_a_name_collision_is_skipped_unless_renamed(library, oasis_db, capsys):
    from appdb import session_scope
    from curation.models import CurationProject

    with session_scope() as s:
        s.add(CurationProject(name="Pacific T3", source_config={"dataset_mode": "T3"}))

    _run(oasis_db, "--apply")
    assert "already exists here" in capsys.readouterr().out
    with session_scope() as s:
        assert s.query(CurationProject).count() == 1

    _run(oasis_db, "--apply", "--rename-suffix", " (OASIS)")
    with session_scope() as s:
        assert {p.name for p in s.query(CurationProject)} == {"Pacific T3", "Pacific T3 (OASIS)"}


def test_an_unknown_mapped_account_stops_the_import(library, oasis_db):
    with pytest.raises(SystemExit, match="no such user"):
        _run(oasis_db, "--apply", "--user-map", "Jane Smith=nobody")
