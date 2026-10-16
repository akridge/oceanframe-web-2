"""P2 schema: change-set column on events, membership seeding from history."""
from __future__ import annotations

from test_curation import P, _add, _item_payload, _project, _test_buckets, client, db_session  # noqa: F401


def test_new_tables_and_event_column_exist(client):
    from sqlalchemy import inspect

    from appdb.engine import get_engine

    inspector = inspect(get_engine())
    tables = set(inspector.get_table_names())
    assert {"curation_project_members", "curation_change_sets", "curation_releases"} <= tables
    assert "change_set_id" in {c["name"] for c in inspector.get_columns("curation_events")}
    assert "ix_cur_events_commit" in {i["name"] for i in inspector.get_indexes("curation_events")}


def test_event_column_migration_is_idempotent(client):
    from appdb.bootstrap import _event_change_sets
    from appdb.engine import get_engine

    with get_engine().begin() as conn:  # column already there from create_all: no error, no change
        _event_change_sets(conn)
        _event_change_sets(conn)


def test_member_seeding_from_owner_and_editors(client, db_session):
    from sqlalchemy import delete

    from appdb.bootstrap import _ledger, init_app_db
    from appdb.engine import get_engine
    from auth.models import User
    from curation.models import CurationEvent, CurationProjectMember

    project = _project(client, "Seeded")
    item = _add(client, project["id"], _item_payload("seed-1", "ACBR"))
    editor = User(username="seed-editor", display_name="Seed Editor", role="curator", password_hash="x")
    db_session.add(editor)
    db_session.commit()
    db_session.add(CurationEvent(project_id=project["id"], item_id=item["id"], event_type="item_updated",
                                 user_id=editor.id, curator_name="Seed Editor"))
    db_session.commit()
    with get_engine().begin() as conn:
        conn.execute(delete(CurationProjectMember.__table__))
        conn.execute(delete(_ledger).where(_ledger.c.migration_id == "20261011_02"))

    assert init_app_db()["migrations_applied"] == ["20261011_02"]
    db_session.expire_all()
    roles = {m.user_id: m.role for m in db_session.query(CurationProjectMember).filter_by(project_id=project["id"])}
    assert roles[project["owner_user_id"]] == "lead"
    assert roles[editor.id] == "curator"
    assert init_app_db()["migrations_applied"] == []  # recorded: never runs twice


def test_event_column_upgrade_path_restores_column_and_index(client):
    """Simulate a database created before P2: drop the index and column, then migrate."""
    from sqlalchemy import inspect, text

    from appdb.bootstrap import _event_change_sets
    from appdb.engine import get_engine

    engine = get_engine()

    def columns_and_indexes():
        inspector = inspect(engine)
        return ({c["name"] for c in inspector.get_columns("curation_events")},
                {i["name"] for i in inspector.get_indexes("curation_events")})

    try:
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_cur_events_commit"))
            conn.execute(text("ALTER TABLE curation_events DROP COLUMN change_set_id"))
        columns, indexes = columns_and_indexes()
        assert "change_set_id" not in columns
        assert "ix_cur_events_commit" not in indexes

        with engine.begin() as conn:
            _event_change_sets(conn)

        columns, indexes = columns_and_indexes()
        assert "change_set_id" in columns
        assert "ix_cur_events_commit" in indexes
    finally:
        # Leave the schema complete for later tests, even if an assertion failed.
        with engine.begin() as conn:
            _event_change_sets(conn)
