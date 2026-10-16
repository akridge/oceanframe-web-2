"""
Create the schema, apply migrations, and run each feature's start-up hook.

Runs from the container entrypoint (``python -m appdb.bootstrap``) after the
database is reachable, and again from the app's lifespan — it is idempotent, so
running twice costs a few catalog queries.

New tables come from ``create_all``. Changes to existing tables are numbered
migrations recorded in ``oceanframe_schema_migrations``, the same ledger idea as
CAT's ``cat_schema_migrations``: each runs once, in order, and a database can
always say which ones it has.
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import Callable

from sqlalchemy import Column, DateTime, MetaData, Table, insert, inspect, select, text
from sqlalchemy.engine import Connection

from appdb.base import Base, Str, utcnow
from appdb.engine import get_engine

logger = logging.getLogger(__name__)

# Modules that declare tables on Base. Imported here so create_all sees them.
MODEL_MODULES = ("auth.models", "curation.models")

# Modules with a ``on_bootstrap()`` hook, run after the schema exists.
STARTUP_HOOKS = ("auth.service", "curation.importer")

_ledger_meta = MetaData()
_ledger = Table(
    "oceanframe_schema_migrations",
    _ledger_meta,
    Column("migration_id", Str(20), primary_key=True),
    Column("description", Str(200), nullable=False),
    Column("applied_at", DateTime, nullable=False),
)

Migration = tuple[str, str, Callable[[Connection], None]]

def _curation_statistics_index(conn: Connection) -> None:
    from curation.models import CurationItem

    index = next(index for index in CurationItem.__table__.indexes if index.name == "ix_cur_items_included_label")
    index.create(conn, checkfirst=True)


def _event_change_sets(conn: Connection) -> None:
    """curation_events.change_set_id (+ index) on databases created before P2."""
    from curation.models import CurationEvent

    if "change_set_id" not in {c["name"] for c in inspect(conn).get_columns("curation_events")}:
        conn.execute(text("ALTER TABLE curation_events ADD change_set_id INTEGER"))
    index = next(i for i in CurationEvent.__table__.indexes if i.name == "ix_cur_events_commit")
    index.create(conn, checkfirst=True)


def _item_flags(conn: Connection) -> None:
    """curation_items.removed / source_missing on databases created before P3a."""
    from curation.models import CurationItem

    existing = {c["name"] for c in inspect(conn).get_columns("curation_items")}
    for name in ("removed", "source_missing"):
        if name not in existing:
            column_type = CurationItem.__table__.c[name].type.compile(dialect=conn.dialect)
            conn.execute(text(f"ALTER TABLE curation_items ADD {name} {column_type} DEFAULT 0 NOT NULL"))


def _seed_project_members(conn: Connection) -> None:
    """Existing projects: the owner leads, everyone who edited an item curates."""
    from curation.models import CurationEvent, CurationProject, CurationProjectMember

    members = CurationProjectMember.__table__
    existing = set(conn.execute(select(members.c.project_id, members.c.user_id)).all())
    rows = []
    for project_id, owner in conn.execute(select(CurationProject.id, CurationProject.owner_user_id)).all():
        if owner is not None and (project_id, owner) not in existing:
            existing.add((project_id, owner))
            rows.append({"project_id": project_id, "user_id": owner, "role": "lead",
                         "added_by_name": "migration", "added_at": utcnow()})
    editors = conn.execute(select(CurationEvent.project_id, CurationEvent.user_id).where(
        CurationEvent.user_id.is_not(None), CurationEvent.item_id.is_not(None)).distinct()).all()
    for project_id, user_id in editors:
        if (project_id, user_id) not in existing:
            existing.add((project_id, user_id))
            rows.append({"project_id": project_id, "user_id": user_id, "role": "curator",
                         "added_by_name": "migration", "added_at": utcnow()})
    if rows:
        conn.execute(insert(members), rows)


# (id, description, fn). Append only; never renumber or edit one that shipped.
MIGRATIONS: list[Migration] = [
    ("20261002_01", "Cover curation class statistics by project and inclusion", _curation_statistics_index),
    ("20261011_01", "Curation events: change set column", _event_change_sets),
    ("20261011_02", "Curation projects: members from owners and editors", _seed_project_members),
    ("20261012_01", "Curation items: removed and source_missing flags", _item_flags),
]


def _import_models() -> None:
    for name in MODEL_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            # A feature package that is not part of this build is fine; a
            # missing dependency *inside* one is not.
            if exc.name != name.split(".")[0] and exc.name != name:
                raise


def _apply_migrations(conn: Connection) -> list[str]:
    done = set(conn.execute(select(_ledger.c.migration_id)).scalars())
    applied = []
    for migration_id, description, fn in MIGRATIONS:
        if migration_id in done:
            continue
        fn(conn)
        conn.execute(_ledger.insert().values(
            migration_id=migration_id, description=description, applied_at=utcnow(),
        ))
        applied.append(migration_id)
    return applied


def _run_hooks() -> None:
    for name in STARTUP_HOOKS:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name.split(".")[0] and exc.name != name:
                raise
            continue
        hook = getattr(module, "on_bootstrap", None)
        if hook:
            hook()


def require_oracle() -> bool:
    """OCEANFRAME_REQUIRE_ORACLE=on: refuse to start on anything but Oracle.

    The compose services set it, so a missing DSN fails loudly instead of
    quietly curating into a SQLite file nobody else can see.
    """
    return os.getenv("OCEANFRAME_REQUIRE_ORACLE", "").strip().lower() in {"1", "true", "yes", "on"}


def init_app_db() -> dict:
    """Bring the schema up to date and run start-up hooks. Safe to call repeatedly."""
    _import_models()
    engine = get_engine()
    if require_oracle() and engine.dialect.name != "oracle":
        raise RuntimeError(
            f"OCEANFRAME_REQUIRE_ORACLE is on but the app database is {engine.dialect.name}: "
            "set OCEANFRAME_DB_USER and OCEANFRAME_DB_DSN (or OCEANFRAME_DB_URL)."
        )
    with engine.begin() as conn:
        _ledger_meta.create_all(conn)
        Base.metadata.create_all(conn)
        applied = _apply_migrations(conn)
    _run_hooks()
    tables = sorted(inspect(engine).get_table_names())
    return {"dialect": engine.dialect.name, "tables": tables, "migrations_applied": applied}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = init_app_db()
    print(f"  ✓ App database ready ({result['dialect']}): {len(result['tables'])} tables")
    for migration_id in result["migrations_applied"]:
        print(f"    applied migration {migration_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
