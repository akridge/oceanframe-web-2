#!/usr/bin/env python3
"""
Bring curation work from an OASIS database into OceanFrame.

    python -m scripts.import_oasis_curation path/to/ai4me_jobs.db              # dry run: report only
    python -m scripts.import_oasis_curation path/to/ai4me_jobs.db --apply \\
        --user-map "Jane Doe=jdoe,Alleviate=alleviate"                         # import

Writes to whatever the app uses (OCEANFRAME_DB_URL / OCEANFRAME_DB_*, or the
local SQLite). In the Oracle stack, run it inside the app container:

    ./oceanframe-compose.sh cp ai4me_jobs.db oceanframe-app:/tmp/
    ./oceanframe-compose.sh exec oceanframe-app python -m scripts.import_oasis_curation /tmp/ai4me_jobs.db

Guarantees:

* The source is never written to. The database (and its -wal/-shm) is copied
  to a temporary folder and the copy is opened read-only.
* Re-running is safe. Projects match by name, items by (project, source_key),
  and every imported event and import operation is recorded in
  curation_import_map, so a second run adds only what is new.
* Nothing is guessed silently. The dry run reports orphans, values too long
  for the target columns, blanks that become NULL, name collisions and every
  person named in the history, before anything is written.

People: OASIS stored free-text names. ``--user-map "Name=username"`` attributes
a name to an existing OceanFrame account; any other name becomes an inactive,
password-less placeholder (``legacy.<name>``) an admin can later rename or
activate. The original text is always kept in ``curation_events.legacy_actor``.
Note that OASIS's page stamped the *project's* curator name on every edit, so
in most databases the history says who owned the project, not who clicked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

TABLES = ("curation_projects", "curation_items", "curation_events", "curation_import_operations")

# Target column sizes (curation/models.py) that OASIS text has to fit into.
TEXT_LIMITS = {
    "curation_projects": {"name": 200, "curator_name": 200, "source_kind": 40, "status": 20},
    "curation_items": {
        "source_key": 400, "source_image_uri": 1000, "crop_uri": 1000, "file_name": 300,
        "source_label": 100, "current_label": 100, "review_state": 20, "region": 120, "site": 120,
        "split": 10, "model_label": 100, "quality_decision": 20, "quality_reviewed_by": 200,
    },
    "curation_events": {"event_type": 40, "curator_name": 200, "operation_id": 36},
}
# Columns where OASIS may hold '' that the target stores as NULL.
BLANK_TO_NULL = {"curation_items": ("region", "site", "split", "model_label", "quality_decision",
                                    "quality_reviewed_by")}


# ── Reading the source ──────────────────────────────────────────────────────────

def open_copy(source: Path) -> tuple[sqlite3.Connection, str, Path]:
    """Copy the database (with -wal/-shm) somewhere private and open the copy read-only."""
    if not source.is_file():
        raise SystemExit(f"No such database: {source}")
    workdir = Path(tempfile.mkdtemp(prefix="oasis-import-"))
    digest = hashlib.sha256()
    for suffix in ("", "-wal", "-shm"):
        part = source.with_name(source.name + suffix)
        if part.is_file():
            shutil.copy2(part, workdir / part.name)
            if suffix != "-shm":
                digest.update(part.read_bytes())
    copy = workdir / source.name
    # Applying the WAL needs a writable copy, so open read-write once to
    # checkpoint it, then reopen read-only for the import itself.
    writable = sqlite3.connect(copy)
    try:
        writable.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        writable.close()
    conn = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn, digest.hexdigest()[:16], workdir


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def rows(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> list[dict]:
    if not columns(conn, table):
        return []
    return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}" {where} ORDER BY rowid', params)]


def as_json(value: Any, default):
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def as_datetime(value: Any) -> Optional[datetime]:
    """OASIS wrote timezone-aware UTC through SQLite; the target keeps naive UTC."""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def blank(value: Any, limit: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return (text[:limit] if limit else text) or None


# ── Dry run ─────────────────────────────────────────────────────────────────────

def survey(conn: sqlite3.Connection) -> dict:
    report: dict[str, Any] = {"counts": {}, "orphans": {}, "too_long": {}, "blank_to_null": {}, "actors": {}}
    for table in TABLES:
        report["counts"][table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] \
            if columns(conn, table) else 0

    def count(sql: str) -> int:
        try:
            return conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    report["orphans"] = {
        "items_without_project": count(
            "SELECT COUNT(*) FROM curation_items i LEFT JOIN curation_projects p ON p.id = i.project_id WHERE p.id IS NULL"),
        "events_without_project": count(
            "SELECT COUNT(*) FROM curation_events e LEFT JOIN curation_projects p ON p.id = e.project_id WHERE p.id IS NULL"),
        "events_without_item": count(
            "SELECT COUNT(*) FROM curation_events e LEFT JOIN curation_items i ON i.id = e.item_id "
            "WHERE e.item_id IS NOT NULL AND i.id IS NULL"),
    }
    for table, limits in TEXT_LIMITS.items():
        present = columns(conn, table)
        for column, limit in limits.items():
            if column in present:
                over = count(f'SELECT COUNT(*) FROM "{table}" WHERE LENGTH("{column}") > {limit}')
                if over:
                    report["too_long"][f"{table}.{column} > {limit}"] = over
    for table, cols in BLANK_TO_NULL.items():
        present = columns(conn, table)
        for column in cols:
            if column in present:
                blanks = count(f'SELECT COUNT(*) FROM "{table}" WHERE TRIM("{column}") = \'\'')
                if blanks:
                    report["blank_to_null"][f"{table}.{column}"] = blanks

    actors: Counter = Counter()
    for row in rows(conn, "curation_projects"):
        if blank(row.get("curator_name")):
            actors[row["curator_name"].strip()] += 0
    for row in rows(conn, "curation_events"):
        if blank(row.get("curator_name")):
            actors[row["curator_name"].strip()] += 1
    for row in rows(conn, "curation_items"):
        if blank(row.get("quality_reviewed_by")):
            actors[row["quality_reviewed_by"].strip()] += 1
    report["actors"] = dict(sorted(actors.items(), key=lambda kv: (-kv[1], kv[0])))
    return report


# ── Import ──────────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:100] or "unknown"


class Importer:
    def __init__(self, conn: sqlite3.Connection, source_db: str, user_map: dict[str, str],
                 rename_suffix: Optional[str]) -> None:
        from appdb.engine import new_session

        self.conn = conn
        self.source_db = source_db
        self.user_map = user_map
        self.rename_suffix = rename_suffix
        self.db = new_session()
        self.users: dict[str, Any] = {}
        self.stats = Counter()
        self.problems: list[str] = []

    # bookkeeping ---------------------------------------------------------------
    def mapped(self, kind: str, legacy_id: Any) -> Optional[str]:
        from curation.models import CurationImportMap
        return self.db.query(CurationImportMap.new_id).filter_by(
            source_db=self.source_db, kind=kind, legacy_id=str(legacy_id)).scalar()

    def remember(self, kind: str, legacy_id: Any, new_id: Any) -> None:
        from curation.models import CurationImportMap
        self.db.add(CurationImportMap(source_db=self.source_db, kind=kind,
                                      legacy_id=str(legacy_id), new_id=str(new_id)))

    def person(self, name: Optional[str]):
        """The OceanFrame user a free-text OASIS name stands for (creating a placeholder)."""
        from auth.models import User
        name = blank(name)
        if not name:
            return None
        if name in self.users:
            return self.users[name]
        username = self.user_map.get(name)
        if username:
            user = self.db.query(User).filter_by(username=username).one_or_none()
            if user is None:
                raise SystemExit(f"--user-map names {username!r} for {name!r}, but there is no such user")
        else:
            username = f"legacy.{_slug(name)}"
            user = self.db.query(User).filter_by(username=username).one_or_none()
            if user is None:
                user = User(username=username, display_name=name[:120], role="viewer",
                            is_active=False, password_hash=None)
                self.db.add(user)
                self.db.flush()
                self.stats["placeholder_users"] += 1
        self.users[name] = user
        return user

    # projects ------------------------------------------------------------------
    def project(self, row: dict):
        from curation.models import CurationProject
        existing_id = self.mapped("project", row["id"])
        if existing_id:
            return self.db.get(CurationProject, existing_id), False
        name = str(row["name"]).strip()[:200]
        if self.db.query(CurationProject).filter_by(name=name).one_or_none() is not None:
            if not self.rename_suffix:
                self.problems.append(f"project {name!r} already exists here; skipped (use --rename-suffix)")
                return None, False
            name = f"{name}{self.rename_suffix}"[:200]
        owner = self.person(row.get("curator_name"))
        config = as_json(row.get("source_config"), {})
        project = CurationProject(
            name=name, description=blank(row.get("description")),
            source_kind=blank(row.get("source_kind")) or "regional_csv", source_config=config,
            curator_name=blank(row.get("curator_name")), owner_user_id=owner.id if owner else None,
            status=blank(row.get("status")) or "draft",
            created_at=as_datetime(row.get("created_at")) or datetime.utcnow(),
            updated_at=as_datetime(row.get("updated_at")) or datetime.utcnow(),
        )
        self.db.add(project)
        self.db.flush()
        self.remember("project", row["id"], project.id)
        self.stats["projects"] += 1
        return project, True

    def items(self, legacy_project_id: str, project) -> dict[int, int]:
        from curation.models import CurationItem
        from curation.splits import normalize_split
        id_map: dict[int, int] = {}
        existing = dict(self.db.query(CurationItem.source_key, CurationItem.id)
                        .filter(CurationItem.project_id == project.id).all())
        pending: list[tuple[int, CurationItem]] = []
        pending_keys: set[str] = set()

        def flush_items() -> None:
            if not pending:
                return
            self.db.add_all([item for _, item in pending])
            self.db.flush()
            for legacy_id, item in pending:
                id_map[legacy_id] = item.id
                existing[item.source_key] = item.id
            self.stats["items"] += len(pending)
            pending.clear()
            pending_keys.clear()
            if self.stats["items"] % 10000 == 0:
                print(f"  imported {self.stats['items']:,} annotation points", flush=True)

        for row in rows(self.conn, "curation_items", "WHERE project_id = ?", (legacy_project_id,)):
            key = str(row["source_key"])
            if key in pending_keys:
                flush_items()
            if key in existing:
                id_map[row["id"]] = existing[key]
                self.stats["items_already_present"] += 1
                continue
            too_long = [c for c, limit in TEXT_LIMITS["curation_items"].items()
                        if row.get(c) is not None and len(str(row[c])) > limit]
            if too_long:
                self.problems.append(f"item {row['id']} skipped: {', '.join(too_long)} too long")
                self.stats["items_skipped"] += 1
                continue
            metadata = as_json(row.get("metadata_json"), {})
            try:
                original_split = normalize_split(row.get("split"))
            except ValueError:
                self.problems.append(f"item {row['id']} skipped: invalid original split {row.get('split')!r}")
                self.stats["items_skipped"] += 1
                continue
            metadata.setdefault("split_origin", "legacy")
            reviewer = blank(row.get("quality_reviewed_by"))
            item = CurationItem(
                project_id=project.id, source_key=key, source_image_uri=row["source_image_uri"],
                crop_uri=row["crop_uri"], file_name=row["file_name"], source_label=row["source_label"],
                current_label=row["current_label"], included=bool(row.get("included", 1)),
                review_state=blank(row.get("review_state")) or "unreviewed",
                row_px=row.get("row_px"), column_px=row.get("column_px"), crop_size=row.get("crop_size") or 224,
                region=blank(row.get("region")), island=blank(metadata.get("island"), 120),
                site=blank(row.get("site")), survey_year=row.get("survey_year"), split=original_split,
                annotator=blank(metadata.get("annotator"), 200),
                model_label=blank(row.get("model_label")), model_confidence=row.get("model_confidence"),
                top_predictions=as_json(row.get("top_predictions"), []),
                quality_score=row.get("quality_score"), quality_flags=as_json(row.get("quality_flags"), []),
                quality_decision=blank(row.get("quality_decision")), quality_reviewed_by=reviewer,
                quality_reviewed_at=as_datetime(row.get("quality_reviewed_at")),
                metadata_json=metadata, revision=row.get("revision") or 1,
                updated_by_name=reviewer,
                created_at=as_datetime(row.get("created_at")) or datetime.utcnow(),
                updated_at=as_datetime(row.get("updated_at")) or datetime.utcnow(),
            )
            pending.append((row["id"], item))
            pending_keys.add(key)
            if len(pending) >= 1000:
                flush_items()
        flush_items()
        return id_map

    def events(self, legacy_project_id: str, project, item_ids: dict[int, int]) -> None:
        from curation.models import CurationEvent
        for row in rows(self.conn, "curation_events", "WHERE project_id = ?", (legacy_project_id,)):
            if self.mapped("event", row["id"]):
                self.stats["events_already_present"] += 1
                continue
            raw_name = blank(row.get("curator_name")) or "unknown"
            user = self.person(raw_name)
            event = CurationEvent(
                project_id=project.id, item_id=item_ids.get(row.get("item_id")),
                operation_id=blank(row.get("operation_id")), event_type=str(row["event_type"])[:40],
                user_id=user.id if user else None, curator_name=(user.label if user else raw_name)[:200],
                legacy_actor=raw_name[:200], reason=blank(row.get("reason")),
                previous_value=as_json(row.get("previous_value"), None),
                new_value=as_json(row.get("new_value"), None),
                created_at=as_datetime(row.get("created_at")) or datetime.utcnow(),
            )
            self.db.add(event)
            self.db.flush()
            self.remember("event", row["id"], event.id)
            self.stats["events"] += 1

    def import_operations(self, legacy_project_id: str, project) -> None:
        from curation.models import CurationImportOperation
        for row in rows(self.conn, "curation_import_operations", "WHERE project_id = ?", (legacy_project_id,)):
            if self.mapped("import", row["id"]):
                continue
            status = blank(row.get("status")) or "completed"
            if status in {"queued", "starting", "running"}:
                status = "interrupted"  # never resume another system's half-finished import
            operation = CurationImportOperation(
                project_id=project.id, status=status, sources=as_json(row.get("sources"), []),
                batch_size=row.get("batch_size") or 2000, processed_rows=row.get("processed_rows") or 0,
                imported_rows=row.get("imported_rows") or 0, skipped_rows=row.get("skipped_rows") or 0,
                rejected_rows=row.get("rejected_rows") or 0, error_message=blank(row.get("error_message")),
                created_by_name="OASIS import",
                created_at=as_datetime(row.get("created_at")) or datetime.utcnow(),
                started_at=as_datetime(row.get("started_at")), completed_at=as_datetime(row.get("completed_at")),
            )
            self.db.add(operation)
            self.db.flush()
            self.remember("import", row["id"], operation.id)
            self.stats["import_operations"] += 1

    def run(self) -> dict:
        from sqlalchemy import func

        from curation.models import CurationItem
        try:
            for row in rows(self.conn, "curation_projects"):
                project, _ = self.project(row)
                if project is None:
                    continue
                item_ids = self.items(row["id"], project)
                self.events(row["id"], project, item_ids)
                self.import_operations(row["id"], project)
                project.item_count = self.db.query(func.count(CurationItem.id)) \
                    .filter(CurationItem.project_id == project.id).scalar() or 0
                # One transaction per project: a failure leaves earlier projects in.
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        finally:
            self.db.close()
        return {"imported": dict(self.stats), "problems": self.problems}


# ── CLI ─────────────────────────────────────────────────────────────────────────

def parse_user_map(text: str) -> dict[str, str]:
    mapping = {}
    for pair in filter(None, (part.strip() for part in (text or "").split(","))):
        name, sep, username = pair.partition("=")
        if not sep or not name.strip() or not username.strip():
            raise SystemExit(f"--user-map entries look like 'Name=username' (got {pair!r})")
        mapping[name.strip()] = username.strip()
    return mapping


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="the OASIS database (e.g. workspace/ai4me_jobs.db)")
    parser.add_argument("--apply", action="store_true", help="write to OceanFrame (default: report only)")
    parser.add_argument("--user-map", default="", help='"OASIS name=oceanframe username,…"')
    parser.add_argument("--rename-suffix", default=None,
                        help="import a project whose name is already taken under name+SUFFIX instead of skipping it")
    parser.add_argument("--report", type=Path, help="also write the report as JSON here")
    args = parser.parse_args(argv)

    conn, source_db, workdir = open_copy(args.source)
    try:
        report = {"source": str(args.source), "source_fingerprint": source_db, "survey": survey(conn)}
        user_map = parse_user_map(args.user_map)
        report["user_map"] = user_map

        from appdb.bootstrap import init_app_db
        init_app_db()
        from curation.models import CurationProject
        from appdb.engine import new_session
        with new_session() as db:
            taken = {name for (name,) in db.query(CurationProject.name)}
        report["survey"]["name_collisions"] = sorted(
            row["name"] for row in rows(conn, "curation_projects") if str(row["name"]).strip() in taken)

        if args.apply:
            report["result"] = Importer(conn, source_db, user_map, args.rename_suffix).run()
    finally:
        conn.close()
        shutil.rmtree(workdir, ignore_errors=True)

    survey_ = report["survey"]
    print(f"OASIS database: {args.source}  (fingerprint {source_db})")
    print("  rows:      " + ", ".join(f"{t.replace('curation_', '')} {n:,}" for t, n in survey_["counts"].items()))
    if any(survey_["orphans"].values()):
        print("  orphans:   " + ", ".join(f"{k} {v:,}" for k, v in survey_["orphans"].items() if v))
    for key, value in survey_["too_long"].items():
        print(f"  too long:  {key}: {value:,} rows (skipped on import)")
    for key, value in survey_["blank_to_null"].items():
        print(f"  blank→NULL {key}: {value:,} rows")
    if survey_["name_collisions"]:
        print("  already here: " + ", ".join(survey_["name_collisions"]))
    if survey_["actors"]:
        print("  people named in the history (map them with --user-map \"Name=username\"):")
        for name, uses in survey_["actors"].items():
            target = report["user_map"].get(name, f"legacy.{_slug(name)} (placeholder)")
            print(f"    {name!r:30} {uses:>6,} uses  → {target}")
        print("  note: OASIS stamped the project's curator on every edit, so these may be owners, not editors.")
    if not sum(survey_["counts"].values()):
        print("  This database has no curation data to import.")
    if args.apply:
        print("  imported:  " + (", ".join(f"{k} {v:,}" for k, v in report["result"]["imported"].items()) or "nothing new"))
        for problem in report["result"]["problems"]:
            print(f"  ⚠ {problem}")
    else:
        print("  Dry run — nothing written. Re-run with --apply to import.")
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
