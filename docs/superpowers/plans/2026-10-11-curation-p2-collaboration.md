# Curation P2 Collaboration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let several people curate one project at once, with:
- per-project members;
- commit-style change sets;
- history;
- comparisons of two people and of two projects;
- combined releases built from several projects' published versions.

**Architecture:**
- **Server modules** under `curation/`, one per concern:
  - `members.py` — roles and the guard dependency;
  - `change_sets.py` — commit, history, windows;
  - `people.py` — person comparison;
  - `project_compare.py`;
  - `releases.py` — build, files, recovery.
- **Routers:** two new routers, `routers/collab.py` and `routers/releases.py`, so `routers/curation.py` doesn't grow further.
- **Schema:** three new tables (`create_all`) and one new event column (a migration).
- **UI:** a new classic script, `static/curation/collab.js`, loaded after `hub.js`. `curation.js` gains only project-role gating and three small hooks.

**Tech Stack:** FastAPI, SQLAlchemy 2 (Oracle via python-oracledb thin in deployment, SQLite in tests), Jinja2, vanilla JS, pytest in Docker.

**Spec:** `docs/superpowers/specs/2026-10-11-curation-p2-collaboration-design.md`

## Global Constraints

- **Schema:**
  - New tables only via `create_all`. Changes to existing tables go only through numbered `MIGRATIONS` in `appdb/bootstrap.py`: append only, never edit one that shipped.
  - Columns use `Str(n)`/`JSONText`. Every identifier (table, index, constraint) is at most 30 characters.
  - SQL must run on Oracle: never put the same bound literal in both SELECT and GROUP BY. Group by raw columns and map in Python.
- **Committable event types:** `item_created`, `item_updated`, `bulk_item_updated`, `manual_point_created`, `quality_reviewed`. Never `source_import_completed`, `hub_link`, `split_adopted`.
- **Project roles:** `curator` | `lead`. An app-wide `admin` counts as `lead` on every project. Members can be any active user, whatever their app-wide role.
- **Change-set messages:** 1–500 characters after trimming.
- **Tests:**
  - Docker only, always with `--build`: `MSYS_NO_PATHCONV=1 docker compose --env-file "$TMP/of-dummy.env" run --build --rm test sh -c "pip install -q --no-cache-dir -r requirements-dev.txt && python -m pytest -q <target>"`. Below, **TEST `<target>`**; the full suite is **TEST `tests`**.
  - **Every new DB-backed test file is added to the `test-oracle` command list in `docker-compose.stack.yml` in the same task that creates it.** Run it once on Oracle: `docker compose -f docker-compose.stack.yml --profile test run --build --rm test-oracle sh -c "pip install --quiet --no-cache-dir --user -r requirements-dev.txt && python -m pytest -q <files>"`.
- **JS syntax check** after any JS change: `MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD/static:/s:ro" node:20-alpine sh -c 'node --check /s/curation/curation.js && node --check /s/curation/hub.js && node --check /s/curation/collab.js && echo SYNTAX-OK'`. Before Task 8 creates `collab.js`, drop that part.
- **Page integrity:**
  - Every `byId('…')` in `curation.js` must exist in `templates/curation.html`, as `test_curation_page_renders_and_every_scripted_element_exists` enforces. Task 8 extends that test to `collab.js`.
  - All data text inserted into HTML goes through `escapeHtml()`.
- **Docker safety:** never run `docker compose … down`, never pass `-v`, never touch `.env`, `.env copy` or `docs/*.png`.
- **Commits:** end each with a `Co-Authored-By: <the model that wrote it> <noreply@anthropic.com>` trailer.

## Review Focus

1. A curator removed from a project while they still have uncommitted changes. Their pending edits stay visible as "uncommitted", and a project lead can still commit them for that person. Test in Task 3.
2. Two tabs committing at once, or an edit arriving while a commit runs. Exactly one change set is created, and the late edit stays uncommitted for next time. Test in Task 3.
3. A combined release where the same crop shows up in three sources, two agreeing and one disagreeing. The key is a conflict, all three rows are dropped, and `conflicts.csv` lists all three. Test in Task 7.
4. Comparing people when one of them made no changes in the window. That person gets an empty side; overlap and disagreements are 0, with no error. Test in Task 5.
5. Comparing projects where crops repeat inside one side (same file name and label twice). Counts stay honest: `matched` counts A's rows that have a match, never a join's row product. Test in Task 6.

---

### Task 1: Schema — members, change sets, releases, and the event column

**Files:**
- Modify: `curation/models.py` (the new classes; a `change_set_id` column and index on `CurationEvent`)
- Modify: `appdb/bootstrap.py` (two migrations)
- Test: `tests/test_collab_schema.py` (new; add it to `test-oracle`)

**Interfaces:**
- Produces:
  - `CurationProjectMember(project_id, user_id, role, added_by_name, added_at)`;
  - `CurationChangeSet(id, project_id, author_user_id, author_name, committed_by_user_id, committed_by_name, message, change_count, summary, created_at)`;
  - `CurationRelease(id, name, number, dataset_mode, status, notes, sources, statistics, item_count, output_uri, error_message, created_by_user_id, created_by_name, created_at, published_at)`;
  - `CurationEvent.change_set_id: int | None`;
  - migration ids `"20261011_01"` (event column) and `"20261011_02"` (member seeding).

- [ ] **Step 1: Write the failing tests** in `tests/test_collab_schema.py`

```python
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
```

Before running, check how `User` rows are created in `auth/models.py` (`grep -n "password" auth/models.py`). Set whatever NOT NULL columns it needs.

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_collab_schema.py`
Expected: FAIL; the tables and `_event_change_sets` don't exist.

- [ ] **Step 3: Models.** In `curation/models.py`:
- add `Index("ix_cur_events_commit", "project_id", "user_id", "change_set_id")` to `CurationEvent.__table_args__`, and the column below after `new_value`;
- add the three classes after `CurationEvent`.

```python
    # Set when the author commits; NULL while the change is still "uncommitted".
    # No database FK: the column is added to existing tables by a migration.
    change_set_id: Mapped[int | None] = mapped_column(Integer)
```

```python
class CurationProjectMember(Base):
    """Who works on a project, and as what. App-wide admins need no row."""

    __tablename__ = "curation_project_members"

    project_id: Mapped[str] = mapped_column(ForeignKey("curation_projects.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("of_users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(Str(10), nullable=False, default="curator")
    added_by_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class CurationChangeSet(Base):
    """One person's edits, committed together under a message."""

    __tablename__ = "curation_change_sets"
    __table_args__ = (Index("ix_cur_change_sets_project", "project_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("curation_projects.id", ondelete="CASCADE"), nullable=False)
    author_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    author_name: Mapped[str] = mapped_column(Str(NAME_LEN), nullable=False)
    committed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    committed_by_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    message: Mapped[str] = mapped_column(Str(500), nullable=False)
    change_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class CurationRelease(Base):
    """One dataset built from published versions of several projects."""

    __tablename__ = "curation_releases"
    __table_args__ = (UniqueConstraint("name", "number", name="uq_cur_release_number"),)

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Str(200), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_mode: Mapped[str] = mapped_column(Str(10), nullable=False)
    status: Mapped[str] = mapped_column(Str(20), nullable=False, default="building")
    notes: Mapped[str | None] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSONText, nullable=False, default=list)
    statistics: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_uri: Mapped[str | None] = mapped_column(Str(URI_LEN))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    created_by_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
```

- [ ] **Step 4: Migrations.** In `appdb/bootstrap.py`:
- extend `from sqlalchemy import …` with `insert, text`;
- add the two functions before `MIGRATIONS`;
- append the two entries.

```python
def _event_change_sets(conn: Connection) -> None:
    """curation_events.change_set_id (+ index) on databases created before P2."""
    from curation.models import CurationEvent

    if "change_set_id" not in {c["name"] for c in inspect(conn).get_columns("curation_events")}:
        conn.execute(text("ALTER TABLE curation_events ADD change_set_id INTEGER"))
    index = next(i for i in CurationEvent.__table__.indexes if i.name == "ix_cur_events_commit")
    index.create(conn, checkfirst=True)


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
```

```python
    ("20261011_01", "Curation events: change set column", _event_change_sets),
    ("20261011_02", "Curation projects: members from owners and editors", _seed_project_members),
```

- [ ] **Step 5: Oracle list.** Add `tests/test_collab_schema.py` to the `test-oracle` command in `docker-compose.stack.yml`.

- [ ] **Step 6: Run on SQLite, then on Oracle**

Run: TEST `tests/test_collab_schema.py tests/test_curation.py`, then the Oracle command with `tests/test_collab_schema.py`.
Expected: PASS on both.

- [ ] **Step 7: Commit**

```bash
git add curation/models.py appdb/bootstrap.py tests/test_collab_schema.py docker-compose.stack.yml
git commit -m "Schema for collaboration: project members, change sets, releases, change-set column on events"
```

---

### Task 2: Project members and project-role permissions

**Files:**
- Create: `curation/members.py`
- Create: `routers/collab.py` (members routes, and a user list for the add-member form)
- Modify: `main.py` (include `collab.router` with prefix `/api`)
- Modify: `routers/curation.py` (route guards; `my_role` on project responses)
- Modify: `routers/registry.py` (HF resume and link-apply guards)
- Modify: `curation/service.py` `create_project`, and the HF `create_project_from_dataset` path in `curation/registry.py`: the creator becomes lead
- Modify: `curation/schemas.py` (`CurationProjectResponse.my_role`)
- Test: `tests/test_members.py` (new; add it to `test-oracle`). Also update existing tests that relied on app-wide curator rights (see Step 7).

**Interfaces:**
- Produces in `curation/members.py`:
  - `ROLES = ("curator", "lead")`
  - `project_role(db, project_id: str, user: CurrentUser) -> str | None`
  - `roles_for(db, user: CurrentUser) -> dict[str, str]`, for every project
  - `lead_names(db, project_id) -> list[str]`
  - `require_project_role(minimum: str)`, a FastAPI dependency factory returning the `CurrentUser`; it reads the path parameter `project_id`
  - `ensure_member(db, project_id, user_id: int, role: str, added_by: str) -> None`, idempotent, never downgrades
  - `add_member`, `set_role`, `remove_member`, `list_members`
- Produces routes:
  - `GET /api/curation/projects/{project_id}/members`
  - `POST …/members {username, role}`
  - `PATCH …/members/{user_id} {role}`
  - `DELETE …/members/{user_id}`
  - `GET /api/curation/users?query=`, returning `[{id, username, display_name}]` (active users, at most 50)
- Produces: `CurationProjectResponse.my_role: Optional[str]` (`lead` | `curator` | null).

- [ ] **Step 1: Write the failing tests** in `tests/test_members.py`

```python
"""Per-project roles: members curate, leads publish and manage members; admins do anything."""
from __future__ import annotations

from test_curation import P, _add, _item_payload, _test_buckets, team  # noqa: F401


def _project_as(client, name):
    response = client.post(f"{P}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _user_id(team, name):
    me = team[name].get("/api/auth/me").json()["user"]
    return me["id"]


def test_creator_leads_and_non_members_are_read_only(team):
    project = _project_as(team["ana"], "Ana's reef")  # ana is an app-wide curator: may create
    assert project["my_role"] == "lead"
    pid = project["id"]
    item = _add(team["ana"], pid, _item_payload("m-1", "ACBR"))
    # Ben (curator app-wide) is not a member: can read, cannot edit
    assert team["ben"].get(f"{P}/projects/{pid}/items").status_code == 200
    edit = team["ben"].patch(f"{P}/projects/{pid}/items/{item['id']}", json={"expected_revision": 1, "included": False})
    assert edit.status_code == 403
    assert "Ana" in edit.json()["detail"]  # names a lead to ask
    listed = {p["id"]: p["my_role"] for p in team["ben"].get(f"{P}/projects").json()}
    assert listed[pid] is None


def test_lead_adds_members_and_project_roles_override_app_roles(team):
    pid = _project_as(team["ana"], "Shared")["id"]
    item = _add(team["ana"], pid, _item_payload("m-2", "ACBR"))
    # vic is an app-wide viewer; as a project curator she may curate this project
    assert team["ana"].post(f"{P}/projects/{pid}/members", json={"username": "vic", "role": "curator"}).status_code == 201
    assert team["vic"].patch(f"{P}/projects/{pid}/items/{item['id']}",
                             json={"expected_revision": 1, "included": False}).status_code == 200
    # but not publish: that needs the project lead role
    assert team["vic"].post(f"{P}/projects/{pid}/versions", json={}).status_code == 403
    assert team["ana"].post(f"{P}/projects/{pid}/members", json={"username": "vic", "role": "curator"}).status_code == 409
    assert team["ana"].post(f"{P}/projects/{pid}/members", json={"username": "nobody", "role": "curator"}).status_code == 404
    members = {m["username"]: m["role"] for m in team["ben"].get(f"{P}/projects/{pid}/members").json()}
    assert members == {"ana": "lead", "vic": "curator"}


def test_only_leads_manage_members_and_the_last_lead_stays(team):
    pid = _project_as(team["ana"], "Guarded")["id"]
    team["ana"].post(f"{P}/projects/{pid}/members", json={"username": "ben", "role": "curator"})
    ana, ben = _user_id(team, "ana"), _user_id(team, "ben")
    assert team["ben"].post(f"{P}/projects/{pid}/members", json={"username": "vic", "role": "curator"}).status_code == 403
    demote = team["ana"].patch(f"{P}/projects/{pid}/members/{ana}", json={"role": "curator"})
    assert demote.status_code == 409 and "lead" in demote.json()["detail"]
    assert team["ana"].delete(f"{P}/projects/{pid}/members/{ana}").status_code == 409
    assert team["ana"].patch(f"{P}/projects/{pid}/members/{ben}", json={"role": "lead"}).status_code == 200
    assert team["ana"].patch(f"{P}/projects/{pid}/members/{ana}", json={"role": "curator"}).status_code == 200
    assert team["ben"].delete(f"{P}/projects/{pid}/members/{ana}").status_code == 200


def test_admins_act_as_lead_everywhere_and_app_leads_do_not(team):
    pid = _project_as(team["ana"], "Admin reach")["id"]
    assert {p["id"]: p["my_role"] for p in team["admin"].get(f"{P}/projects").json()}[pid] == "lead"
    assert team["admin"].post(f"{P}/projects/{pid}/members", json={"username": "ben", "role": "curator"}).status_code == 201


def test_user_search_for_the_add_member_form(team):
    found = team["ana"].get(f"{P}/users?query=be").json()
    assert {u["username"] for u in found} == {"ben"}
    assert team["vic"].get(f"{P}/users?query=").status_code == 200
```

`team` (in `tests/test_curation.py`) has `admin` (app-wide admin), `ana` and `ben` (curators) and `vic` (viewer). Check that `/api/auth/me` returns `{"user": {"id": …}}`; adjust `_user_id` if the shape differs.

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_members.py`
Expected: FAIL; there is no `my_role` and no members routes.

- [ ] **Step 3: Create `curation/members.py`**

```python
"""
Project membership: who may change a project, and who leads it.

Project roles replace the app-wide curator/lead checks on project work: a
member curates (or leads) that project whatever their app-wide role, and a
non-member only reads it. App-wide admins count as lead everywhere and need
no row.
"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from appdb.base import utcnow
from appdb.engine import get_db
from auth.deps import current_user
from auth.models import User
from auth.service import CurrentUser
from curation.models import CurationProject, CurationProjectMember

ROLES = ("curator", "lead")


def project_role(db: Session, project_id: str, user: CurrentUser) -> Optional[str]:
    if user.has_role("admin"):
        return "lead"
    return db.scalar(select(CurationProjectMember.role).where(
        CurationProjectMember.project_id == project_id, CurationProjectMember.user_id == user.id))


def roles_for(db: Session, user: CurrentUser) -> dict[str, str]:
    if user.has_role("admin"):
        return {project_id: "lead" for project_id in db.scalars(select(CurationProject.id))}
    return dict(db.execute(select(CurationProjectMember.project_id, CurationProjectMember.role)
                           .where(CurationProjectMember.user_id == user.id)).all())


def lead_names(db: Session, project_id: str) -> list[str]:
    rows = db.execute(select(User.display_name, User.username).join(
        CurationProjectMember, CurationProjectMember.user_id == User.id).where(
        CurationProjectMember.project_id == project_id, CurationProjectMember.role == "lead")
        .order_by(User.username)).all()
    return [display or username for display, username in rows]


def require_project_role(minimum: str) -> Callable:
    if minimum not in ROLES:
        raise ValueError(f"Unknown project role {minimum!r}")

    def dependency(project_id: str, user: CurrentUser = Depends(current_user),
                   db: Session = Depends(get_db)) -> CurrentUser:
        if db.get(CurationProject, project_id) is None:
            raise HTTPException(status_code=404, detail="Curation project not found")
        role = project_role(db, project_id, user)
        if role is None:
            leads = lead_names(db, project_id)
            ask = f" — ask a project lead ({', '.join(leads)}) to add you" if leads else ""
            raise HTTPException(status_code=403, detail=f"You are not a member of this project{ask}")
        if minimum == "lead" and role != "lead":
            raise HTTPException(status_code=403, detail="This needs the project lead role")
        return user

    return dependency


require_project_curator = require_project_role("curator")
require_project_lead = require_project_role("lead")


def ensure_member(db: Session, project_id: str, user_id: int, role: str, added_by: str) -> None:
    """Add a member if missing; never downgrades an existing one. Caller commits."""
    existing = db.get(CurationProjectMember, (project_id, user_id))
    if existing is None:
        db.add(CurationProjectMember(project_id=project_id, user_id=user_id, role=role,
                                     added_by_name=added_by, added_at=utcnow()))
    elif role == "lead" and existing.role != "lead":
        existing.role = "lead"


def list_members(db: Session, project_id: str) -> list[dict]:
    rows = db.execute(select(CurationProjectMember, User.username, User.display_name).join(
        User, User.id == CurationProjectMember.user_id).where(
        CurationProjectMember.project_id == project_id).order_by(User.username)).all()
    return [{"user_id": member.user_id, "username": username, "display_name": display,
             "role": member.role, "added_by_name": member.added_by_name, "added_at": member.added_at}
            for member, username, display in rows]


def _lead_count(db: Session, project_id: str) -> int:
    return db.scalar(select(func.count()).select_from(CurationProjectMember).where(
        CurationProjectMember.project_id == project_id, CurationProjectMember.role == "lead")) or 0


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise HTTPException(status_code=422, detail="Role must be curator or lead")


def add_member(db: Session, project_id: str, username: str, role: str, actor: CurrentUser) -> dict:
    _check_role(role)
    user = db.scalar(select(User).where(User.username == username.strip(), User.is_active == True))  # noqa: E712
    if user is None:
        raise HTTPException(status_code=404, detail=f"No active user named {username!r}")
    if db.get(CurationProjectMember, (project_id, user.id)) is not None:
        raise HTTPException(status_code=409, detail=f"{user.display_name or user.username} is already a member")
    db.add(CurationProjectMember(project_id=project_id, user_id=user.id, role=role,
                                 added_by_name=actor.label, added_at=utcnow()))
    db.commit()
    return next(m for m in list_members(db, project_id) if m["user_id"] == user.id)


def _member_or_404(db: Session, project_id: str, user_id: int) -> CurationProjectMember:
    member = db.get(CurationProjectMember, (project_id, user_id))
    if member is None:
        raise HTTPException(status_code=404, detail="Not a member of this project")
    return member


def _keep_a_lead(db: Session, project_id: str, member: CurationProjectMember, actor: CurrentUser) -> None:
    if member.role == "lead" and _lead_count(db, project_id) <= 1 and not actor.has_role("admin"):
        raise HTTPException(status_code=409, detail="A project needs at least one lead; make someone else lead first")


def set_role(db: Session, project_id: str, user_id: int, role: str, actor: CurrentUser) -> dict:
    _check_role(role)
    member = _member_or_404(db, project_id, user_id)
    if role != "lead":
        _keep_a_lead(db, project_id, member, actor)
    member.role = role
    db.commit()
    return next(m for m in list_members(db, project_id) if m["user_id"] == user_id)


def remove_member(db: Session, project_id: str, user_id: int, actor: CurrentUser) -> None:
    member = _member_or_404(db, project_id, user_id)
    _keep_a_lead(db, project_id, member, actor)
    db.delete(member)
    db.commit()
```

Check `current_user` is exported by `auth/deps.py` and that `User` has `.label`. If not, use `display_name or username`.

- [ ] **Step 4: Create `routers/collab.py`**, and register it in `main.py` after `curation.router`: import `collab` in the `from routers import …` line, and add `app.include_router(collab.router, prefix="/api")`.

```python
"""
Collaboration API — /api/curation/*: project members, change sets, comparing people.

Kept apart from routers/curation.py, which is already large. Reading is open
to anyone signed in; changing membership needs the project lead role.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from appdb.engine import get_db
from auth.deps import current_user
from auth.models import User
from auth.service import CurrentUser
from curation import members, service

router = APIRouter(prefix="/curation", tags=["curation-collab"])


class MemberIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    role: str = Field(default="curator", pattern="^(curator|lead)$")


class RoleIn(BaseModel):
    role: str = Field(pattern="^(curator|lead)$")


@router.get("/projects/{project_id}/members")
def list_members(project_id: str, db: Session = Depends(get_db), _user: CurrentUser = Depends(current_user)):
    service.get_project_or_404(db, project_id)
    return members.list_members(db, project_id)


@router.post("/projects/{project_id}/members", status_code=201)
def add_member(project_id: str, body: MemberIn, user: CurrentUser = Depends(members.require_project_lead),
               db: Session = Depends(get_db)):
    return members.add_member(db, project_id, body.username, body.role, user)


@router.patch("/projects/{project_id}/members/{user_id}")
def set_member_role(project_id: str, user_id: int, body: RoleIn,
                    user: CurrentUser = Depends(members.require_project_lead), db: Session = Depends(get_db)):
    return members.set_role(db, project_id, user_id, body.role, user)


@router.delete("/projects/{project_id}/members/{user_id}")
def remove_member(project_id: str, user_id: int, user: CurrentUser = Depends(members.require_project_lead),
                  db: Session = Depends(get_db)):
    members.remove_member(db, project_id, user_id, user)
    return {"ok": True}


@router.get("/users")
def find_users(query: str = Query("", max_length=120), db: Session = Depends(get_db),
               _user: CurrentUser = Depends(current_user)):
    """Active people, for the add-member form. Names only — nothing private."""
    stmt = select(User.id, User.username, User.display_name).where(User.is_active == True)  # noqa: E712
    text = query.strip().lower()
    if text:
        like = f"%{text}%"
        stmt = stmt.where(or_(User.username.ilike(like), User.display_name.ilike(like)))
    rows = db.execute(stmt.order_by(User.username).limit(50)).all()
    return [{"id": i, "username": u, "display_name": d} for i, u, d in rows]
```

- [ ] **Step 5: Creators become lead.**
- In `curation/service.py` `create_project`, after `db.refresh(project)`, add the lines below. Import `members` locally (`from curation import members  # noqa: PLC0415`) to avoid an import cycle.

```python
    members.ensure_member(db, project.id, user.id, "lead", user.label)
    db.commit()
```

- In `curation/registry.py`, confirm `create_project_from_dataset` creates the project through `service.create_project`. If it does, nothing more is needed. If it builds `CurationProject` itself, add the same two lines there.

- [ ] **Step 6: Guards and `my_role`.**
- In `routers/curation.py`, add `from curation import members` and replace guards exactly as below. Keep the handler parameter name `user` where the body uses it.
- In `curation/schemas.py`, add `my_role: Optional[str] = None` to `CurationProjectResponse`.

| Route | Old guard | New guard |
|---|---|---|
| `POST /projects/{project_id}/items` | `require_curator` | `members.require_project_curator` |
| `PATCH …/items/{item_id}` | `require_curator` | `members.require_project_curator` |
| `POST …/items/bulk` | `require_curator` | `members.require_project_curator` |
| `POST …/items/{item_id}/quality` | `require_curator` | `members.require_project_curator` |
| `POST …/quality/bulk` | `require_curator` | `members.require_project_curator` |
| `POST …/source-images/{anchor_item_id}/quality` | `require_curator` | `members.require_project_curator` |
| `POST …/source-images/{anchor_item_id}/points` | `require_curator` | `members.require_project_curator` |
| `POST …/source-images/{anchor_item_id}/classify` | `require_curator` | `members.require_project_curator` |
| `PATCH …/evaluations/{run_id}/predictions/{prediction_id}/item` | `require_curator` | `members.require_project_curator` |
| `PATCH …/evaluations/{run_id}/inputs/{item_id}/item` | `require_curator` | `members.require_project_curator` |
| `POST /projects/{project_id}/imports` | `require_creator` | `members.require_project_curator` |
| `POST …/imports/{operation_id}/cancel` | `require_creator` (in `dependencies=`) | `members.require_project_curator` |
| `POST /projects/{project_id}/evaluations` | `require_creator` | `members.require_project_curator` |
| `POST …/evaluations/{run_id}/cancel` | `require_creator` | `members.require_project_curator` |
| `POST /projects/{project_id}/versions` | `require_lead` | `members.require_project_lead` |
| `POST …/versions/{number}/images` | `require_lead` | `members.require_project_lead` |

Leave the global ones unchanged:
- `POST /projects` (`require_creator`);
- `DELETE /projects/{id}` (`require_lead`, plus the owner check in the service);
- evaluation models (`require_creator`).

`my_role` on project responses:

```python
@router.post("/projects", response_model=CurationProjectResponse, status_code=201)
def create_project(request: CurationProjectCreate, user: CurrentUser = Depends(require_creator),
                   db: Session = Depends(get_db)):
    project = service.create_project(db, request, user)
    return {**CurationProjectResponse.model_validate(project).model_dump(), "my_role": "lead"}


@router.get("/projects", response_model=list[CurationProjectResponse])
def list_projects(user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    roles = members.roles_for(db, user)
    rows = db.scalars(select(CurationProject).order_by(CurationProject.updated_at.desc())).all()
    return [{**CurationProjectResponse.model_validate(p).model_dump(), "my_role": roles.get(p.id)} for p in rows]


@router.get("/projects/{project_id}", response_model=CurationProjectResponse)
def get_project(project_id: str, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    project = service.get_project_or_404(db, project_id)
    return {**CurationProjectResponse.model_validate(project).model_dump(),
            "my_role": members.project_role(db, project_id, user)}
```

Import `current_user` from `auth.deps`. `CurationProjectResponse` is an `_ORM` model, so `model_validate(orm_object)` works. If `dataset_mode` is a property on the ORM, it still validates.

**`routers/registry.py`:**
- The resume route `/datasets/{entry_id}/projects/{project_id}/imports` changes from `require_creator` to `members.require_project_curator`.
- In `link_dataset`, replace the app-wide lead check (`if not body.dry_run and not user.has_role("lead")`) with:

```python
    if not body.dry_run and members.project_role(db, body.project_id, user) != "lead":
        raise HTTPException(status_code=403, detail="Applying a link needs the project lead role; a dry run does not")
```

- [ ] **Step 7: Update existing tests.** Run TEST `tests`. Tests that now get 403 do so because they used a non-member to edit a project someone else created (typically `team["ana"]`/`team["ben"]` on a project made by `team["admin"]`).
- Fix them by **adding the membership first**, through the API as the project's creator or admin, with a small helper in `tests/test_curation.py`:

```python
def _join(team, project_id, name, role="curator"):
    response = team["admin"].post(f"{P}/projects/{project_id}/members", json={"username": name, "role": role})
    assert response.status_code in (201, 409), response.text
```

- Where a test **asserts** that a curator *can* do something on someone else's project, add the membership; never weaken the assertion.
- Where a test asserts the old app-wide-lead rule (e.g. "only leads publish"), rewrite it to the project-lead rule and keep its intent.
- List every test you changed in your report.

- [ ] **Step 8: Run**

Run: TEST `tests`, then the Oracle command with `tests/test_members.py tests/test_curation.py tests/test_registry.py`.
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add curation/members.py routers/collab.py main.py routers/curation.py routers/registry.py curation/service.py curation/registry.py curation/schemas.py tests/ docker-compose.stack.yml
git commit -m "Project members: per-project curator and lead roles guard project work; creators lead"
```

---

### Task 3: Change sets — commit, history, uncommitted

**Files:**
- Create: `curation/change_sets.py`
- Modify: `routers/collab.py` (change-set routes)
- Test: `tests/test_change_sets.py` (new; add it to `test-oracle`)

**Interfaces:**
- Consumes: `CurationChangeSet` and `CurationEvent.change_set_id` (Task 1); `members.project_role` and `members.require_project_curator` (Task 2).
- Produces in `curation/change_sets.py`:
  - `COMMITTABLE: tuple[str, ...]`
  - `pending(db, project_id, user_id) -> dict` `{count, summary, recent}`
  - `commit(db, project_id, author_id: int, message: str, committer: CurrentUser) -> CurationChangeSet`
  - `history(db, project_id, *, user_id=None, since_version=None, limit=50, before=None) -> list[dict]`
  - `detail(db, project_id, change_set_id, offset=0, limit=200) -> dict`
  - `uncommitted(db, project_id) -> list[dict]` `[{user_id, name, count}]`
  - `in_window(db, project_id, after_event: int, up_to_event: int | None) -> dict` `{change_sets: [{id, author, message, change_count}], uncommitted: [{name, count}]}`
  - `as_dict(cs) -> dict`
- Produces routes, under `/api/curation/projects/{project_id}/change-sets`:
  - `GET /pending?user=me|<id>`
  - `POST {message, user_id?}`
  - `GET ?user_id=&since_version=&limit=&before=`
  - `GET /uncommitted`
  - `GET /{change_set_id}?offset=&limit=`

- [ ] **Step 1: Write the failing tests** in `tests/test_change_sets.py`

```python
"""Change sets: edits pile up as uncommitted; Commit groups them under a message."""
from __future__ import annotations

from test_curation import P, _add, _item_payload, _join, _test_buckets, team  # noqa: F401

CS = "/change-sets"


def _setup(team):
    pid = team["admin"].post(f"{P}/projects", json={"name": "Commits"}).json()["id"]
    _join(team, pid, "ana")
    _join(team, pid, "ben")
    items = [_add(team["admin"], pid, _item_payload(f"cs-{n}", "ACBR")) for n in range(3)]
    return pid, items


def _relabel(client, pid, item, label="POCS"):
    current = client.get(f"{P}/projects/{pid}/items/{item['id']}").json()
    response = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                            json={"expected_revision": current["revision"], "current_label": label})
    assert response.status_code == 200, response.text


def test_edits_are_uncommitted_until_committed(team):
    pid, items = _setup(team)
    _relabel(team["ana"], pid, items[0])
    _relabel(team["ana"], pid, items[1])
    _relabel(team["ben"], pid, items[2])
    mine = team["ana"].get(f"{P}/projects/{pid}{CS}/pending?user=me").json()
    assert mine["count"] == 2 and mine["summary"]["kinds"] == {"relabel": 2}
    assert mine["summary"]["classes"] == [{"label": "POCS", "items": 2}] and len(mine["recent"]) == 2

    committed = team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "  Relabelled POCS on Hawaii  "})
    assert committed.status_code == 201, committed.text
    body = committed.json()
    assert (body["message"], body["change_count"], body["author_name"]) == ("Relabelled POCS on Hawaii", 2, "Ana")
    assert team["ana"].get(f"{P}/projects/{pid}{CS}/pending?user=me").json()["count"] == 0
    assert team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "again"}).status_code == 409
    # ben's edit is untouched and shows as in progress; admin's item creations are admin's own
    ben_id = team["ben"].get("/api/auth/me").json()["user"]["id"]
    open_work = {row["user_id"]: row for row in team["admin"].get(f"{P}/projects/{pid}{CS}/uncommitted").json()}
    assert open_work[ben_id] == {"user_id": ben_id, "name": "Ben", "count": 1}


def test_imports_and_admin_item_creation_are_not_ana_s(team):
    pid, _ = _setup(team)  # admin created the items: those are admin's uncommitted changes, not ana's
    assert team["ana"].get(f"{P}/projects/{pid}{CS}/pending?user=me").json()["count"] == 0
    assert team["admin"].get(f"{P}/projects/{pid}{CS}/pending?user=me").json()["summary"]["kinds"] == {"added": 3}


def test_history_detail_and_show_these_items(team):
    pid, items = _setup(team)
    _relabel(team["ana"], pid, items[0])
    first = team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "one"}).json()
    _relabel(team["ben"], pid, items[1])
    team["ben"].post(f"{P}/projects/{pid}{CS}", json={"message": "two"})
    history = team["vic"].get(f"{P}/projects/{pid}{CS}").json()
    assert [cs["message"] for cs in history] == ["two", "one"]
    only_ana = team["vic"].get(f"{P}/projects/{pid}{CS}?user_id={first['author_user_id']}").json()
    assert [cs["message"] for cs in only_ana] == ["one"]
    detail = team["vic"].get(f"{P}/projects/{pid}{CS}/{first['id']}").json()
    assert detail["item_ids"] == [items[0]["id"]]
    assert detail["events"][0]["previous_value"]["current_label"] == "ACBR"
    assert detail["events"][0]["new_value"]["current_label"] == "POCS"


def test_message_rules_and_membership(team):
    pid, items = _setup(team)
    _relabel(team["ana"], pid, items[0])
    assert team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "   "}).status_code == 422
    assert team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "x" * 501}).status_code == 422
    assert team["vic"].post(f"{P}/projects/{pid}{CS}", json={"message": "not mine"}).status_code == 403


def test_a_lead_commits_for_a_member_who_left_and_curators_cannot(team):
    pid, items = _setup(team)
    _relabel(team["ana"], pid, items[0])
    ana_id = team["ana"].get("/api/auth/me").json()["user"]["id"]
    assert team["ben"].post(f"{P}/projects/{pid}{CS}", json={"message": "for ana", "user_id": ana_id}).status_code == 403
    assert team["admin"].delete(f"{P}/projects/{pid}/members/{ana_id}").status_code == 200  # ana leaves
    assert team["admin"].get(f"{P}/projects/{pid}{CS}/uncommitted").json()[0]["name"] == "Ana"
    done = team["admin"].post(f"{P}/projects/{pid}{CS}", json={"message": "Ana's last edits", "user_id": ana_id})
    assert done.status_code == 201
    assert (done.json()["author_name"], done.json()["committed_by_name"]) == ("Ana", "Admin")


def test_an_edit_landing_during_a_commit_stays_uncommitted(team, monkeypatch):
    from curation import change_sets

    pid, items = _setup(team)
    _relabel(team["ana"], pid, items[0])
    real = change_sets.summarize

    def summarize_then_edit(db, where):
        result = real(db, where)  # the summary has been read: now another edit lands
        _relabel(team["ana"], pid, items[1], "SAND")
        return result

    monkeypatch.setattr(change_sets, "summarize", summarize_then_edit)
    body = team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "first"}).json()
    assert body["change_count"] == 1
    monkeypatch.setattr(change_sets, "summarize", real)
    assert team["ana"].get(f"{P}/projects/{pid}{CS}/pending?user=me").json()["count"] == 1
```

Also: the `team` fixture's admin display name may not be "Admin". Read `ADMIN` in `tests/test_curation.py` and use the real label in the commit-for test.

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_change_sets.py`
Expected: FAIL with 404 on `/change-sets`.

- [ ] **Step 3: Create `curation/change_sets.py`**

```python
"""
Change sets: a person's edits, committed together under a message.

Edits apply to the shared draft the moment they are made; until their author
commits, they are that author's "uncommitted changes". Committing stamps them
with a change set in one conditional UPDATE, so two tabs committing at once
make exactly one change set, and an edit landing mid-commit waits for the next.
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from auth.models import User
from auth.service import CurrentUser
from curation.models import CurationChangeSet, CurationDatasetVersion, CurationEvent, CurationItem
from curation.service import in_chunks

COMMITTABLE = ("item_created", "item_updated", "bulk_item_updated", "manual_point_created", "quality_reviewed")
MESSAGE_MAX = 500


def _pending_where(project_id: str, user_id: int) -> list:
    return [CurationEvent.project_id == project_id, CurationEvent.user_id == user_id,
            CurationEvent.change_set_id.is_(None), CurationEvent.event_type.in_(COMMITTABLE)]


def kinds_of(event: CurationEvent) -> set[str]:
    if event.event_type == "item_created":
        return {"added"}
    if event.event_type == "manual_point_created":
        return {"point"}
    before, after = event.previous_value or {}, event.new_value or {}
    kinds = {"quality"} if event.event_type == "quality_reviewed" else set()
    for field, kind in (("current_label", "relabel"), ("included", "inclusion"), ("review_state", "review")):
        if field in before and before.get(field) != after.get(field):
            kinds.add(kind)
    return kinds or {"other"}


def _top_classes(db: Session, item_ids: set[int], limit: int = 10) -> list[dict]:
    counts: Counter = Counter()
    ids = list(item_ids)
    for start in range(0, len(ids), 1000):
        counts.update(dict(db.execute(
            select(CurationItem.current_label, func.count()).where(in_chunks(CurationItem.id, ids[start:start + 1000]))
            .group_by(CurationItem.current_label)).all()))
    return [{"label": label, "items": n} for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def summarize(db: Session, where: list) -> tuple[dict, int, int]:
    """(summary, event count, highest event id) for the events matching ``where``."""
    kinds: Counter = Counter()
    items: set[int] = set()
    count = max_id = 0
    for event in db.scalars(select(CurationEvent).where(*where).order_by(CurationEvent.id)
                            .execution_options(yield_per=2000)):
        count += 1
        max_id = event.id
        kinds.update(kinds_of(event))
        if event.item_id is not None:
            items.add(event.item_id)
    return {"kinds": dict(kinds), "classes": _top_classes(db, items), "items": len(items)}, count, max_id


def _event_dict(event: CurationEvent) -> dict:
    return {"id": event.id, "item_id": event.item_id, "event_type": event.event_type,
            "curator_name": event.curator_name, "reason": event.reason,
            "previous_value": event.previous_value, "new_value": event.new_value, "created_at": event.created_at}


def as_dict(cs: CurationChangeSet) -> dict:
    return {"id": cs.id, "project_id": cs.project_id, "author_user_id": cs.author_user_id, "author_name": cs.author_name,
            "committed_by_user_id": cs.committed_by_user_id, "committed_by_name": cs.committed_by_name,
            "message": cs.message, "change_count": cs.change_count, "summary": cs.summary or {},
            "created_at": cs.created_at}


def pending(db: Session, project_id: str, user_id: int) -> dict:
    where = _pending_where(project_id, user_id)
    summary, count, _ = summarize(db, where)
    recent = db.scalars(select(CurationEvent).where(*where).order_by(CurationEvent.id.desc()).limit(20)).all()
    return {"count": count, "summary": summary, "recent": [_event_dict(e) for e in recent]}


def commit(db: Session, project_id: str, author_id: int, message: str, committer: CurrentUser) -> CurationChangeSet:
    text = (message or "").strip()
    if not 1 <= len(text) <= MESSAGE_MAX:
        raise HTTPException(status_code=422, detail=f"Write a message of 1–{MESSAGE_MAX} characters")
    author = db.get(User, author_id)
    if author is None:
        raise HTTPException(status_code=404, detail="No such person")
    where = _pending_where(project_id, author_id)
    summary, count, max_id = summarize(db, where)
    if not count:
        raise HTTPException(status_code=409, detail="Nothing left to commit")
    change_set = CurationChangeSet(
        project_id=project_id, author_user_id=author_id, author_name=author.display_name or author.username,
        committed_by_user_id=committer.id if committer.id != author_id else None,
        committed_by_name=committer.label if committer.id != author_id else None,
        message=text, change_count=0, summary=summary)
    db.add(change_set)
    db.flush()
    stamped = db.execute(update(CurationEvent).where(*where, CurationEvent.id <= max_id)
                         .values(change_set_id=change_set.id)
                         .execution_options(synchronize_session=False)).rowcount
    if not stamped:  # another tab committed them in between
        db.rollback()
        raise HTTPException(status_code=409, detail="Nothing left to commit")
    change_set.change_count = stamped
    db.commit()
    db.refresh(change_set)
    return change_set


def _watermark(db: Session, project_id: str, version: int) -> int:
    row = db.scalar(select(CurationDatasetVersion).where(
        CurationDatasetVersion.project_id == project_id, CurationDatasetVersion.version == version))
    if row is None:
        raise HTTPException(status_code=404, detail=f"No version v{version}")
    return row.event_watermark or 0


def history(db: Session, project_id: str, *, user_id: Optional[int] = None, since_version: Optional[int] = None,
            limit: int = 50, before: Optional[int] = None) -> list[dict]:
    stmt = select(CurationChangeSet).where(CurationChangeSet.project_id == project_id)
    if user_id is not None:
        stmt = stmt.where(CurationChangeSet.author_user_id == user_id)
    if before is not None:
        stmt = stmt.where(CurationChangeSet.id < before)
    if since_version is not None:
        after = _watermark(db, project_id, since_version)
        stmt = stmt.where(CurationChangeSet.id.in_(select(CurationEvent.change_set_id).where(
            CurationEvent.project_id == project_id, CurationEvent.id > after,
            CurationEvent.change_set_id.is_not(None)).distinct()))
    rows = db.scalars(stmt.order_by(CurationChangeSet.id.desc()).limit(min(max(limit, 1), 200))).all()
    return [as_dict(cs) for cs in rows]


def detail(db: Session, project_id: str, change_set_id: int, offset: int = 0, limit: int = 200) -> dict:
    change_set = db.get(CurationChangeSet, change_set_id)
    if change_set is None or change_set.project_id != project_id:
        raise HTTPException(status_code=404, detail="Change set not found")
    where = [CurationEvent.project_id == project_id, CurationEvent.change_set_id == change_set_id]
    events = db.scalars(select(CurationEvent).where(*where).order_by(CurationEvent.id)
                        .offset(max(offset, 0)).limit(min(max(limit, 1), 1000))).all()
    item_ids = sorted(i for i in db.scalars(select(CurationEvent.item_id).where(
        *where, CurationEvent.item_id.is_not(None)).distinct()))
    return {**as_dict(change_set), "events": [_event_dict(e) for e in events], "item_ids": item_ids}


def uncommitted(db: Session, project_id: str) -> list[dict]:
    rows = db.execute(select(CurationEvent.user_id, func.max(CurationEvent.curator_name), func.count()).where(
        CurationEvent.project_id == project_id, CurationEvent.change_set_id.is_(None),
        CurationEvent.event_type.in_(COMMITTABLE), CurationEvent.user_id.is_not(None))
        .group_by(CurationEvent.user_id)).all()
    return sorted(({"user_id": u, "name": n, "count": c} for u, n, c in rows), key=lambda r: (-r["count"], r["name"]))


def in_window(db: Session, project_id: str, after_event: int, up_to_event: Optional[int]) -> dict:
    """Change sets with events in (after, up_to], and what was left uncommitted there — for change logs."""
    where = [CurationEvent.project_id == project_id, CurationEvent.id > after_event,
             CurationEvent.event_type.in_(COMMITTABLE)]
    if up_to_event is not None:
        where.append(CurationEvent.id <= up_to_event)
    ids = [i for i in db.scalars(select(CurationEvent.change_set_id).where(
        *where, CurationEvent.change_set_id.is_not(None)).distinct())]
    sets = db.scalars(select(CurationChangeSet).where(CurationChangeSet.id.in_(ids))
                      .order_by(CurationChangeSet.id)).all() if ids else []
    left = db.execute(select(func.max(CurationEvent.curator_name), func.count()).where(
        *where, CurationEvent.change_set_id.is_(None)).group_by(CurationEvent.user_id)).all()
    return {"change_sets": [{"id": cs.id, "author": cs.author_name, "message": cs.message,
                             "change_count": cs.change_count} for cs in sets],
            "uncommitted": [{"name": name, "count": count} for name, count in left]}
```

`in_chunks` comes from `curation/service.py`. It takes a column and a list and returns an IN expression (it is used with `CurationItem.id` there). Check its signature before relying on it.

- [ ] **Step 4: Routes.** Append to `routers/collab.py`, adding `change_sets` to the `from curation import …` line and `HTTPException` to the fastapi import.
- `/uncommitted` and `/pending` must be declared **before** `/{change_set_id}`.
- Viewing is open to anyone signed in. Pending for another person needs project lead.

```python
class CommitIn(BaseModel):
    message: str = Field(max_length=2000)
    user_id: Optional[int] = None


@router.get("/projects/{project_id}/change-sets/pending")
def pending_changes(project_id: str, user: str = Query("me"), me: CurrentUser = Depends(current_user),
                    db: Session = Depends(get_db)):
    service.get_project_or_404(db, project_id)
    target = me.id if user == "me" else int(user) if user.isdigit() else None
    if target is None:
        raise HTTPException(status_code=422, detail="user must be 'me' or a user id")
    if target != me.id and members.project_role(db, project_id, me) != "lead":
        raise HTTPException(status_code=403, detail="Only a project lead can see someone else's uncommitted changes")
    return change_sets.pending(db, project_id, target)


@router.get("/projects/{project_id}/change-sets/uncommitted")
def uncommitted_changes(project_id: str, db: Session = Depends(get_db), _me: CurrentUser = Depends(current_user)):
    service.get_project_or_404(db, project_id)
    return change_sets.uncommitted(db, project_id)


@router.post("/projects/{project_id}/change-sets", status_code=201)
def commit_changes(project_id: str, body: CommitIn, me: CurrentUser = Depends(members.require_project_curator),
                   db: Session = Depends(get_db)):
    author = body.user_id if body.user_id is not None else me.id
    if author != me.id and members.project_role(db, project_id, me) != "lead":
        raise HTTPException(status_code=403, detail="Only a project lead can commit someone else's changes")
    return change_sets.as_dict(change_sets.commit(db, project_id, author, body.message, me))


@router.get("/projects/{project_id}/change-sets")
def list_change_sets(project_id: str, user_id: Optional[int] = None, since_version: Optional[int] = None,
                     limit: int = Query(50, ge=1, le=200), before: Optional[int] = None,
                     db: Session = Depends(get_db), _me: CurrentUser = Depends(current_user)):
    service.get_project_or_404(db, project_id)
    return change_sets.history(db, project_id, user_id=user_id, since_version=since_version, limit=limit, before=before)


@router.get("/projects/{project_id}/change-sets/{change_set_id}")
def change_set_detail(project_id: str, change_set_id: int, offset: int = Query(0, ge=0),
                      limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db),
                      _me: CurrentUser = Depends(current_user)):
    return change_sets.detail(db, project_id, change_set_id, offset, limit)
```

- [ ] **Step 5: Oracle list and run**

Add `tests/test_change_sets.py` to `test-oracle`. Run TEST `tests/test_change_sets.py`, then the Oracle command with that file. Expected: PASS on both.

- [ ] **Step 6: Commit**

```bash
git add curation/change_sets.py routers/collab.py tests/test_change_sets.py docker-compose.stack.yml
git commit -m "Change sets: uncommitted edits, commit with a message, history and detail"
```

---

### Task 4: Change sets in the publish preview and version change logs

**Files:**
- Modify: `curation/versions.py` (`preview`, `_build`, `_write_files`, `changelog_markdown`)
- Test: `tests/test_change_sets.py` (append)

**Interfaces:**
- Consumes: `change_sets.in_window(db, project_id, after_event, up_to_event) -> {change_sets, uncommitted}` from Task 3.
- Produces:
  - `preview()` returns extra keys `change_sets` and `uncommitted`;
  - a version's stored `changes` dict gains `change_sets` and `uncommitted`, which are additive;
  - `manifest.json` gains top-level `change_sets` and `uncommitted`;
  - `CHANGELOG.md` gains a "## Change sets" section and an "Uncommitted:" line.

- [ ] **Step 1: Write the failing test** (append to `tests/test_change_sets.py`)

```python
def test_versions_list_their_change_sets_and_uncommitted_work(team):
    import time

    pid, items = _setup(team)
    team["admin"].post(f"{P}/projects/{pid}{CS}", json={"message": "Imported the first three"})
    _relabel(team["ana"], pid, items[0])
    team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "Ana fixed a POCS"})
    _relabel(team["ben"], pid, items[1])  # left uncommitted

    preview = team["admin"].get(f"{P}/projects/{pid}/versions/preview").json()
    assert [cs["message"] for cs in preview["change_sets"]] == ["Imported the first three", "Ana fixed a POCS"]
    assert preview["uncommitted"] == [{"name": "Ben", "count": 1}]

    assert team["admin"].post(f"{P}/projects/{pid}/versions", json={"notes": "v1"}).status_code == 202
    deadline = time.time() + 30
    while team["admin"].get(f"{P}/projects/{pid}/versions/1").json()["status"] == "building" and time.time() < deadline:
        time.sleep(0.1)
    version = team["admin"].get(f"{P}/projects/{pid}/versions/1").json()
    assert [cs["message"] for cs in version["changes"]["change_sets"]] == ["Imported the first three", "Ana fixed a POCS"]
    changelog = team["admin"].get(f"{P}/projects/{pid}/versions/1/files/CHANGELOG.md").text
    assert "## Change sets" in changelog and "Ana fixed a POCS" in changelog and "Uncommitted: Ben 1" in changelog
    manifest = team["admin"].get(f"{P}/projects/{pid}/versions/1/files/manifest.json").json()
    assert len(manifest["change_sets"]) == 2 and manifest["uncommitted"] == [{"name": "Ben", "count": 1}]
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_change_sets.py::test_versions_list_their_change_sets_and_uncommitted_work`
Expected: FAIL with KeyError `change_sets`.

- [ ] **Step 3: Implement.** In `curation/versions.py`, import `change_sets` locally inside the functions (`from curation import change_sets  # noqa: PLC0415`) to avoid an import cycle.
- `preview()`: build the dict as now, then add `**change_sets.in_window(db, project_id, watermark, None)`.
- `_build()`: right after `version.contributors = …`:

```python
        window = change_sets.in_window(db, project.id, previous.event_watermark if previous else 0, watermark)
        version.changes = {**version.changes, **window}
```

- `_write_files()` summary dict: add `"change_sets": (version.changes or {}).get("change_sets", []), "uncommitted": (version.changes or {}).get("uncommitted", []),`.
- `changelog_markdown()`, after the Contributors block and before `return`:

```python
    committed = changes.get("change_sets") or []
    if committed:
        lines += ["", "## Change sets", ""] + [
            f"- **{cs['author']}** — {cs['message']} ({cs['change_count']:,} changes)" for cs in committed]
    left = changes.get("uncommitted") or []
    if left:
        lines += ["", "Uncommitted: " + " · ".join(f"{u['name']} {u['count']:,}" for u in left)]
```

- [ ] **Step 4: Run this test and the version suites**

Run: TEST `tests/test_change_sets.py tests/test_versions.py tests/test_version_compare.py`
Expected: PASS. Old versions without the keys still render, because every read uses `.get(…, [])`.

- [ ] **Step 5: Commit**

```bash
git add curation/versions.py tests/test_change_sets.py
git commit -m "Versions: preview, manifest and change log list the change sets and uncommitted work they contain"
```

---

### Task 5: Comparing two people

**Files:**
- Create: `curation/people.py`
- Modify: `routers/collab.py` (route)
- Test: `tests/test_people_compare.py` (new; add it to `test-oracle`)

**Interfaces:**
- Consumes: `change_sets.COMMITTABLE`, `change_sets.kinds_of`, `change_sets._top_classes` and `change_sets._watermark` (Task 3; use them, don't re-implement).
- Produces: `people.compare(db, project_id, a_id: int, b_id: int, since_version: int | None) -> dict`, and `GET /api/curation/projects/{project_id}/people/compare?a=&b=&since=`. The shape:
  - `{people: [{user_id, name, change_sets, changes, kinds, classes}] ×2, overlap: int, disagreements: {count, items: [{item_id, file_name, field, a_value, a_at, b_value, b_at, current}], item_ids}}`

- [ ] **Step 1: Write the failing tests** in `tests/test_people_compare.py`

```python
"""Comparing two people's work: what each did, where they overlapped, where they disagree."""
from __future__ import annotations

from test_change_sets import CS, _relabel, _setup
from test_curation import P, _join, _test_buckets, team  # noqa: F401


def _ids(team):
    return {n: team[n].get("/api/auth/me").json()["user"]["id"] for n in ("ana", "ben")}


def test_compare_two_people(team):
    pid, items = _setup(team)
    ids = _ids(team)
    _relabel(team["ana"], pid, items[0], "POCS")
    _relabel(team["ben"], pid, items[0], "SAND")      # disagreement on items[0]
    _relabel(team["ana"], pid, items[1], "POCS")
    _relabel(team["ben"], pid, items[1], "POCS")      # overlap, agreement
    _relabel(team["ben"], pid, items[2], "TURFH")     # ben only
    team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "Ana's pass"})
    body = team["vic"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b={ids['ben']}").json()
    ana, ben = body["people"]
    assert (ana["name"], ana["changes"], [c["message"] for c in ana["change_sets"]]) == ("Ana", 2, ["Ana's pass"])
    assert (ben["changes"], ben["change_sets"], ben["kinds"]) == (3, [], {"relabel": 3})
    assert body["overlap"] == 2
    assert body["disagreements"]["count"] == 1
    row = body["disagreements"]["items"][0]
    assert (row["item_id"], row["field"], row["a_value"], row["b_value"], row["current"]) == (
        items[0]["id"], "current_label", "POCS", "SAND", "SAND")


def test_someone_with_no_changes_gives_an_empty_side(team):
    pid, items = _setup(team)
    ids = _ids(team)
    _relabel(team["ana"], pid, items[0])
    body = team["ana"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b={ids['ben']}").json()
    assert body["people"][1]["changes"] == 0 and body["overlap"] == 0 and body["disagreements"]["count"] == 0


def test_since_a_version_and_bad_requests(team):
    import time

    pid, items = _setup(team)
    ids = _ids(team)
    _relabel(team["ana"], pid, items[0])
    team["admin"].post(f"{P}/projects/{pid}/versions", json={})
    deadline = time.time() + 30
    while team["admin"].get(f"{P}/projects/{pid}/versions/1").json()["status"] == "building" and time.time() < deadline:
        time.sleep(0.1)
    _relabel(team["ana"], pid, items[1])
    since = team["ana"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b={ids['ben']}&since=1").json()
    assert since["people"][0]["changes"] == 1
    assert team["ana"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b={ids['ana']}").status_code == 422
    assert team["ana"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b=999999").status_code == 404
    assert team["ana"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b={ids['ben']}&since=9").status_code == 404
```

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_people_compare.py`
Expected: FAIL with 404.

- [ ] **Step 3: Create `curation/people.py`**

```python
"""Comparing two people's work in one project: what each did, overlap, disagreements."""
from __future__ import annotations

from collections import Counter
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth.models import User
from curation.change_sets import COMMITTABLE, _top_classes, _watermark, kinds_of
from curation.models import CurationChangeSet, CurationEvent, CurationItem

FIELDS = ("current_label", "included")
MAX_ROWS = 500


def compare(db: Session, project_id: str, a_id: int, b_id: int, since_version: Optional[int]) -> dict:
    if a_id == b_id:
        raise HTTPException(status_code=422, detail="Choose two different people")
    people = {u.id: u for u in db.scalars(select(User).where(User.id.in_([a_id, b_id])))}
    if len(people) != 2:
        raise HTTPException(status_code=404, detail="No such person")
    after = _watermark(db, project_id, since_version) if since_version is not None else 0

    sides, items, latest = {}, {a_id: set(), b_id: set()}, {}
    for uid in (a_id, b_id):
        sides[uid] = {"kinds": Counter(), "changes": 0, "sets": set()}
    events = db.scalars(select(CurationEvent).where(
        CurationEvent.project_id == project_id, CurationEvent.user_id.in_([a_id, b_id]),
        CurationEvent.id > after, CurationEvent.event_type.in_(COMMITTABLE)).order_by(CurationEvent.id)
        .execution_options(yield_per=2000))
    for event in events:
        side = sides[event.user_id]
        side["changes"] += 1
        side["kinds"].update(kinds_of(event))
        if event.change_set_id is not None:
            side["sets"].add(event.change_set_id)
        if event.item_id is None:
            continue
        items[event.user_id].add(event.item_id)
        before, after_value = event.previous_value or {}, event.new_value or {}
        for field in FIELDS:
            if field in before and before.get(field) != after_value.get(field):
                latest[(event.user_id, event.item_id, field)] = (after_value.get(field), event.created_at)

    overlap = items[a_id] & items[b_id]
    rows = []
    for item_id in sorted(overlap):
        for field in FIELDS:
            a, b = latest.get((a_id, item_id, field)), latest.get((b_id, item_id, field))
            if a and b and a[0] != b[0]:
                rows.append({"item_id": item_id, "field": field, "a_value": a[0], "a_at": a[1],
                             "b_value": b[0], "b_at": b[1]})
    current = {}
    if rows:
        wanted = sorted({r["item_id"] for r in rows})
        for item in db.scalars(select(CurationItem).where(CurationItem.id.in_(wanted[:MAX_ROWS]))):
            current[item.id] = item
    for row in rows:
        item = current.get(row["item_id"])
        row["file_name"] = item.file_name if item else None
        row["current"] = (getattr(item, row["field"]) if item else None)

    def person(uid: int) -> dict:
        user, side = people[uid], sides[uid]
        sets = db.scalars(select(CurationChangeSet).where(CurationChangeSet.id.in_(side["sets"]))
                          .order_by(CurationChangeSet.id)).all() if side["sets"] else []
        return {"user_id": uid, "name": user.display_name or user.username, "changes": side["changes"],
                "kinds": dict(side["kinds"]), "classes": _top_classes(db, items[uid]),
                "change_sets": [{"id": cs.id, "message": cs.message, "change_count": cs.change_count,
                                 "created_at": cs.created_at} for cs in sets]}

    return {"people": [person(a_id), person(b_id)], "overlap": len(overlap),
            "disagreements": {"count": len(rows), "items": rows[:MAX_ROWS],
                              "item_ids": sorted({r["item_id"] for r in rows})}}
```

Use `CurationItem.id.in_` on at most 500 ids (bounded by `MAX_ROWS`); that is fine on Oracle.

- [ ] **Step 4: Route.** Append to `routers/collab.py` (add `people` to the import):

```python
@router.get("/projects/{project_id}/people/compare")
def compare_people(project_id: str, a: int, b: int, since: Optional[int] = None,
                   db: Session = Depends(get_db), _me: CurrentUser = Depends(current_user)):
    service.get_project_or_404(db, project_id)
    return people.compare(db, project_id, a, b, since)
```

- [ ] **Step 5: Oracle list and run**

Add `tests/test_people_compare.py` to `test-oracle`. Run TEST `tests/test_people_compare.py` and the Oracle command for it. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add curation/people.py routers/collab.py tests/test_people_compare.py docker-compose.stack.yml
git commit -m "Compare two people's work in a project: changes, overlap and disagreements"
```

---

### Task 6: Comparing two projects

**Files:**
- Create: `curation/project_compare.py`
- Create: `routers/releases.py` (holds this route now; Task 7 adds release routes), and register it in `main.py`
- Test: `tests/test_project_compare.py` (new; add it to `test-oracle`)

**Interfaces:**
- Produces: `project_compare.compare(db, a_id, a_ref: str, b_id, b_ref: str) -> dict`, where `ref` is `"live"` or a version number as text, and `GET /api/curation/compare-projects?a=&a_version=live&b=&b_version=live`. The shape:
  - `{a: {project, ref, count}, b: {...}, matched, only_a, only_b, classes: [{label, a, b, diff}], label_disagreements: {count, pairs: [{a_label, b_label, count}], a_item_ids}, split_disagreements: {count, pairs: [{a_split, b_split, count}]}}`

- [ ] **Step 1: Write the failing tests** in `tests/test_project_compare.py`

```python
"""Comparing two projects, live or published, matched by crop file name and label."""
from __future__ import annotations

from test_curation import P, _add, _item_payload, _project, _test_buckets, client  # noqa: F401
from test_versions import _publish


def _crop(key, label, split, file_name=None):
    payload = {**_item_payload(key, label), "split": split}
    if file_name:
        payload["file_name"] = file_name
    return payload


def test_compare_two_live_projects(client):
    a = _project(client, "Side A")["id"]
    b = _project(client, "Side B")["id"]
    _add(client, a, _crop("a1", "ACBR", "train", "x1.png"))
    _add(client, a, _crop("a2", "ACBR", "train", "x2.png"))
    _add(client, a, _crop("a3", "POCS", "val", "x3.png"))
    _add(client, b, _crop("b1", "ACBR", "test", "x1.png"))     # same crop, other split
    a2_in_b = _add(client, b, _crop("b2", "ACBR", "train", "x2.png"))
    _add(client, b, _crop("b4", "SAND", "train", "x4.png"))    # only in B
    # relabel B's copy of x2 → label disagreement (source_label still ACBR, so it still matches)
    client.patch(f"{P}/projects/{b}/items/{a2_in_b['id']}", json={"expected_revision": a2_in_b["revision"], "current_label": "POCS"})
    body = client.get(f"{P}/compare-projects?a={a}&a_version=live&b={b}&b_version=live").json()
    assert (body["a"]["count"], body["b"]["count"], body["matched"], body["only_a"], body["only_b"]) == (3, 3, 2, 1, 1)
    assert body["label_disagreements"]["count"] == 1
    assert body["label_disagreements"]["pairs"] == [{"a_label": "ACBR", "b_label": "POCS", "count": 1}]
    assert body["split_disagreements"]["pairs"] == [{"a_split": "train", "b_split": "test", "count": 1}]
    classes = {row["label"]: row for row in body["classes"]}
    assert classes["SAND"] == {"label": "SAND", "a": 0, "b": 1, "diff": 1}


def test_repeats_inside_one_side_do_not_inflate_counts(client):
    a = _project(client, "Repeat A")["id"]
    b = _project(client, "Repeat B")["id"]
    _add(client, a, _crop("r1", "ACBR", "train", "same.png"))
    _add(client, b, _crop("r2", "ACBR", "train", "same.png"))
    _add(client, b, _crop("r3", "ACBR", "train", "same.png"))  # B holds the crop twice
    body = client.get(f"{P}/compare-projects?a={a}&a_version=live&b={b}&b_version=live").json()
    assert (body["matched"], body["only_a"], body["only_b"]) == (1, 0, 0)


def test_compare_a_published_version_and_bad_pairs(client):
    a = _project(client, "Versioned A")["id"]
    b = _project(client, "T1 B", mode="T1")["id"]
    _add(client, a, _crop("v1", "ACBR", "train", "v1.png"))
    _publish(client, a)
    ok = client.get(f"{P}/compare-projects?a={a}&a_version=1&b={a}&b_version=live")
    assert ok.status_code == 200 and ok.json()["matched"] == 1
    assert client.get(f"{P}/compare-projects?a={a}&a_version=live&b={a}&b_version=live").status_code == 422
    assert client.get(f"{P}/compare-projects?a={a}&a_version=live&b={b}&b_version=live").status_code == 422  # modes differ
    assert client.get(f"{P}/compare-projects?a={a}&a_version=7&b={a}&b_version=live").status_code == 404
```

Check that `CurationItemCreate` accepts `file_name`; `_item_payload` already sets it.

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_project_compare.py`
Expected: FAIL with 404.

- [ ] **Step 3: Create `curation/project_compare.py`**

```python
"""
Comparing two projects — each live (included items) or a published version —
matched by crop (file_name, source_label), the rule that matched every HF row.

Counts come from EXISTS, not from the join's row product, so a crop held twice
on one side cannot inflate them. Splits are grouped raw and mapped in Python:
a bound default in both SELECT and GROUP BY fails on Oracle.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from curation.models import CurationDatasetVersion, CurationItem, CurationProject, CurationVersionInput

MAX_IDS = 5000


def _side(db: Session, project_id: str, ref: str):
    project = db.get(CurationProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Curation project not found")
    if ref == "live":
        rows = (select(CurationItem.id.label("item_id"), CurationItem.file_name, CurationItem.source_label,
                       CurationItem.current_label.label("label"), CurationItem.split)
                .where(CurationItem.project_id == project_id, CurationItem.included == True)  # noqa: E712
                .subquery())
        return project, rows, "live"
    if not ref.isdigit():
        raise HTTPException(status_code=422, detail=f"'{ref}' is neither 'live' nor a version number")
    version = db.scalar(select(CurationDatasetVersion).where(
        CurationDatasetVersion.project_id == project_id, CurationDatasetVersion.version == int(ref)))
    if version is None:
        raise HTTPException(status_code=404, detail=f"No version v{ref} of {project.name}")
    if version.status != "published":
        raise HTTPException(status_code=422, detail=f"v{ref} of {project.name} is not published")
    rows = (select(CurationVersionInput.item_id, CurationVersionInput.file_name, CurationVersionInput.source_label,
                   CurationVersionInput.label, CurationVersionInput.split)
            .where(CurationVersionInput.version_id == version.id).subquery())
    return project, rows, f"v{ref}"


def compare(db: Session, a_id: str, a_ref: str, b_id: str, b_ref: str) -> dict:
    if a_id == b_id and a_ref == b_ref:
        raise HTTPException(status_code=422, detail="Choose two different sides to compare")
    project_a, a, a_name = _side(db, a_id, a_ref)
    project_b, b, b_name = _side(db, b_id, b_ref)
    if project_a.dataset_mode != project_b.dataset_mode:
        raise HTTPException(status_code=422, detail="Both projects must use the same dataset mode (T1 or T3)")
    same = and_(a.c.file_name == b.c.file_name, a.c.source_label == b.c.source_label)

    a_count = db.scalar(select(func.count()).select_from(a)) or 0
    b_count = db.scalar(select(func.count()).select_from(b)) or 0
    matched_a = db.scalar(select(func.count()).select_from(a).where(exists().where(same))) or 0
    matched_b = db.scalar(select(func.count()).select_from(b).where(exists().where(same))) or 0

    per_a = dict(db.execute(select(a.c.label, func.count()).group_by(a.c.label)).all())
    per_b = dict(db.execute(select(b.c.label, func.count()).group_by(b.c.label)).all())
    classes = sorted(({"label": label, "a": per_a.get(label, 0), "b": per_b.get(label, 0),
                       "diff": per_b.get(label, 0) - per_a.get(label, 0)} for label in set(per_a) | set(per_b)),
                     key=lambda row: (-abs(row["diff"]), row["label"]))

    joined = a.join(b, same)
    label_pairs = db.execute(select(a.c.label, b.c.label, func.count()).select_from(joined)
                             .where(a.c.label != b.c.label).group_by(a.c.label, b.c.label)
                             .order_by(func.count().desc())).all()
    a_item_ids = [i for i in db.scalars(select(a.c.item_id).select_from(joined).where(a.c.label != b.c.label)
                                        .distinct().order_by(a.c.item_id).limit(MAX_IDS))]
    split_rows = db.execute(select(a.c.split, b.c.split, func.count()).select_from(joined)
                            .group_by(a.c.split, b.c.split)).all()
    split_pairs: dict[tuple[str, str], int] = {}
    for a_split, b_split, count in split_rows:
        pair = (a_split or "unassigned", b_split or "unassigned")
        if pair[0] != pair[1]:
            split_pairs[pair] = split_pairs.get(pair, 0) + count

    return {
        "a": {"project_id": project_a.id, "project": project_a.name, "ref": a_name, "count": a_count},
        "b": {"project_id": project_b.id, "project": project_b.name, "ref": b_name, "count": b_count},
        "matched": matched_a, "only_a": a_count - matched_a, "only_b": b_count - matched_b,
        "classes": classes,
        "label_disagreements": {"count": sum(c for _, _, c in label_pairs),
                                "pairs": [{"a_label": x, "b_label": y, "count": c} for x, y, c in label_pairs[:50]],
                                "a_item_ids": a_item_ids},
        "split_disagreements": {"count": sum(split_pairs.values()),
                                "pairs": [{"a_split": x, "b_split": y, "count": c}
                                          for (x, y), c in sorted(split_pairs.items(), key=lambda kv: -kv[1])]},
    }
```

**Note on `matched`:** the EXISTS inside `select_from(a)` must correlate to `a`. If SQLAlchemy reports a correlation problem, write it as `exists(select(1).select_from(b).where(same))`, and the reverse for B (`exists(select(1).select_from(a).where(same))`).

- [ ] **Step 4: Create `routers/releases.py`** with the compare route now, and register it in `main.py` (`from routers import … releases`; `app.include_router(releases.router, prefix="/api")`):

```python
"""Releases and project comparison — /api/curation/releases, /api/curation/compare-projects."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from appdb.engine import get_db
from auth.deps import current_user
from auth.service import CurrentUser
from curation import project_compare

router = APIRouter(prefix="/curation", tags=["curation-releases"])


@router.get("/compare-projects")
def compare_projects(a: str, b: str, a_version: str = Query("live", max_length=12),
                     b_version: str = Query("live", max_length=12), db: Session = Depends(get_db),
                     _me: CurrentUser = Depends(current_user)):
    return project_compare.compare(db, a, a_version, b, b_version)
```

- [ ] **Step 5: Oracle list and run**

Add `tests/test_project_compare.py` to `test-oracle`. Run TEST and the Oracle command for it. Expected: PASS on both. The EXISTS correlation and the joins must run on Oracle.

- [ ] **Step 6: Commit**

```bash
git add curation/project_compare.py routers/releases.py main.py tests/test_project_compare.py docker-compose.stack.yml
git commit -m "Compare two projects, live or published, matched by crop: counts, classes, disagreements"
```

---

### Task 7: Combined releases

**Files:**
- Create: `curation/releases.py`
- Modify: `routers/releases.py` (release routes)
- Modify: `curation/importer.py` (startup recovery: `releases.mark_interrupted`)
- Test: `tests/test_releases.py` (new; add it to `test-oracle`)

**Interfaces:**
- Consumes:
  - `members.project_role` (Task 2);
  - `versions._slug`, `settings.versions_dir()`, `settings.publish_uri()`;
  - the GCS upload pattern in `versions._upload`. Read it, then write a small `_upload_dir(folder, files, prefix)` in `releases.py` following the same pattern.
- Produces:
  - `releases.start(db, user, name, dataset_mode, sources: list[dict], notes) -> CurationRelease`
  - `releases.as_dict(r) -> dict`
  - `releases.release_dir(r) -> Path`
  - `releases.mark_interrupted(db) -> int`
  - `FILES = ("manifest.csv", "manifest.json", "CHANGELOG.md", "conflicts.csv", "leakage.csv")`
- Produces routes:
  - `GET /api/curation/releases`
  - `GET /api/curation/releases/{id}`
  - `GET /api/curation/releases/{id}/files/{name}`
  - `POST /api/curation/releases {name, dataset_mode, sources: [{project_id, version}], notes}` → 202

- [ ] **Step 1: Write the failing tests** in `tests/test_releases.py`

```python
"""Combined releases from published versions: duplicates kept once, conflicts dropped, leakage listed."""
from __future__ import annotations

import csv
import io
import time

from test_curation import P, _add, _item_payload, _join, _test_buckets, team  # noqa: F401

R = f"{P}/releases"


def _crop(key, label, split, file_name, image=None):
    payload = {**_item_payload(key, label), "split": split, "file_name": file_name}
    if image:
        payload["source_image_uri"] = f"gs://nmfs/images/{image}.jpg"
    return payload


def _publish(client, pid):
    assert client.post(f"{P}/projects/{pid}/versions", json={}).status_code == 202
    deadline = time.time() + 30
    while time.time() < deadline:
        versions = client.get(f"{P}/projects/{pid}/versions").json()["versions"]
        if versions and versions[0]["status"] != "building":
            assert versions[0]["status"] == "published", versions[0]
            return versions[0]["version"]
        time.sleep(0.1)
    raise AssertionError("version did not finish")


def _wait(client, release_id):
    deadline = time.time() + 30
    while time.time() < deadline:
        body = client.get(f"{R}/{release_id}").json()
        if body["status"] != "building":
            return body
        time.sleep(0.1)
    raise AssertionError("release did not finish")


def _three_projects(team):
    admin = team["admin"]
    pids = [admin.post(f"{P}/projects", json={"name": n}).json()["id"] for n in ("Hawaii", "Mariana", "Rare")]
    hawaii, mariana, rare = pids
    _add(admin, hawaii, _crop("h1", "ACBR", "train", "a.png", "img-a"))
    _add(admin, hawaii, _crop("h2", "POCS", "train", "b.png", "img-b"))
    _add(admin, hawaii, _crop("h3", "SAND", "val", "c.png", "img-c"))
    _add(admin, mariana, _crop("m1", "ACBR", "train", "a.png", "img-a"))   # same crop, same label → duplicate
    _add(admin, mariana, _crop("m2", "TURFH", "test", "d.png", "img-b"))   # img-b now in train and test → leakage
    m3 = _add(admin, mariana, _crop("m3", "POCS", "train", "b.png", "img-b"))
    _add(admin, rare, _crop("r1", "POCS", "train", "b.png", "img-b"))       # b.png: two agree…
    r2 = _add(admin, rare, _crop("r2", "SAND", "val", "c.png", "img-c"))
    admin.patch(f"{P}/projects/{rare}/items/{r2['id']}", json={"expected_revision": r2["revision"], "current_label": "CCAH"})
    admin.patch(f"{P}/projects/{mariana}/items/{m3['id']}", json={"expected_revision": m3["revision"], "current_label": "ACBR"})
    return pids, [_publish(admin, pid) for pid in pids]


def test_a_combined_release_keeps_duplicates_once_and_drops_conflicts(team):
    (hawaii, mariana, rare), versions = _three_projects(team)
    response = team["admin"].post(R, json={"name": "Pacific T3", "dataset_mode": "T3", "notes": "first", "sources": [
        {"project_id": hawaii, "version": versions[0]}, {"project_id": mariana, "version": versions[1]},
        {"project_id": rare, "version": versions[2]}]})
    assert response.status_code == 202, response.text
    body = _wait(team["admin"], response.json()["id"])
    assert body["status"] == "published", body
    stats = body["statistics"]
    # a.png: duplicate (kept once). b.png: POCS/ACBR/POCS → conflict, all three dropped.
    # c.png: SAND vs CCAH → conflict, both dropped. d.png kept.
    assert (stats["duplicates"], stats["conflicts"]) == (1, 2)
    assert body["item_count"] == 2
    manifest = list(csv.DictReader(io.StringIO(team["admin"].get(f"{R}/{body['id']}/files/manifest.csv").text)))
    assert sorted(row["file_name"] for row in manifest) == ["a.png", "d.png"]
    assert {row["project"] for row in manifest} == {"Hawaii", "Mariana"}
    conflicts = list(csv.DictReader(io.StringIO(team["admin"].get(f"{R}/{body['id']}/files/conflicts.csv").text)))
    assert sorted((row["file_name"], row["label"]) for row in conflicts) == [
        ("b.png", "ACBR"), ("b.png", "POCS"), ("b.png", "POCS"), ("c.png", "CCAH"), ("c.png", "SAND")]
    changelog = team["admin"].get(f"{R}/{body['id']}/files/CHANGELOG.md").text
    assert "Pacific T3 — r1" in changelog and "Hawaii v1" in changelog


def test_leakage_is_listed_not_fixed(team):
    admin = team["admin"]
    a = admin.post(f"{P}/projects", json={"name": "Leak A"}).json()["id"]
    b = admin.post(f"{P}/projects", json={"name": "Leak B"}).json()["id"]
    _add(admin, a, _crop("l1", "ACBR", "train", "l1.png", "shared-image"))
    _add(admin, b, _crop("l2", "POCS", "test", "l2.png", "shared-image"))
    va, vb = _publish(admin, a), _publish(admin, b)
    rid = admin.post(R, json={"name": "Leaky", "dataset_mode": "T3", "sources": [
        {"project_id": a, "version": va}, {"project_id": b, "version": vb}]}).json()["id"]
    body = _wait(admin, rid)
    assert body["statistics"]["leakage"] == 1 and body["item_count"] == 2
    leakage = admin.get(f"{R}/{rid}/files/leakage.csv").text
    assert "shared-image" in leakage


def test_numbering_permissions_and_validation(team):
    admin = team["admin"]
    pid = admin.post(f"{P}/projects", json={"name": "Solo"}).json()["id"]
    _add(admin, pid, _crop("s1", "ACBR", "train", "s1.png"))
    version = _publish(admin, pid)
    source = [{"project_id": pid, "version": version}]
    first = _wait(admin, admin.post(R, json={"name": "Solo set", "dataset_mode": "T3", "sources": source}).json()["id"])
    second = _wait(admin, admin.post(R, json={"name": "Solo set", "dataset_mode": "T3", "sources": source}).json()["id"])
    assert (first["number"], second["number"]) == (1, 2)
    assert "unchanged" in admin.get(f"{R}/{second['id']}/files/CHANGELOG.md").text.lower()
    # ana: app-wide curator → 403; a lead who isn't a project lead → 403
    assert team["ana"].post(R, json={"name": "x", "dataset_mode": "T3", "sources": source}).status_code == 403
    assert admin.post(R, json={"name": "x", "dataset_mode": "T1", "sources": source}).status_code == 422  # mode
    assert admin.post(R, json={"name": "x", "dataset_mode": "T3", "sources": []}).status_code == 422
    assert admin.post(R, json={"name": "x", "dataset_mode": "T3", "sources": source + source}).status_code == 422
    assert admin.post(R, json={"name": "x", "dataset_mode": "T3",
                               "sources": [{"project_id": pid, "version": 9}]}).status_code == 422
    assert team["vic"].get(R).status_code == 200


def test_a_build_interrupted_by_a_restart_is_marked_failed(team):
    from appdb import session_scope
    from curation import releases
    from curation.models import CurationRelease

    with session_scope() as db:
        db.add(CurationRelease(name="Stuck", number=1, dataset_mode="T3", status="building", sources=[]))
    with session_scope() as db:
        assert releases.mark_interrupted(db) == 1
    with session_scope() as db:
        stuck = db.query(CurationRelease).filter_by(name="Stuck").one()
        assert stuck.status == "failed" and "build it again" in stuck.error_message
```

**Note:** in `_three_projects`, crops in one project share `source_image_uri` per image while carrying different splits. If item creation rejects that (splits must agree per image), give each project's items different image names, and get leakage only across projects as `test_leakage_is_listed_not_fixed` does. Then adjust the leakage expectation in the first test. Check `CurationItemCreate`/`create_item` before running.

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_releases.py`
Expected: FAIL with 404/405.

- [ ] **Step 3: Create `curation/releases.py`**

```python
"""
Combined releases: one dataset built from a published version of each of
several projects. Reads only frozen version rows, so a release is reproducible.

The same crop (file_name, source_label) twice with the same label is kept once
and counted as a duplicate; with different labels it is a conflict — every row
of that crop is left out and listed in conflicts.csv. A source image that ends
up in more than one split across projects is listed in leakage.csv; splits are
not rewritten, because each project's lead owns them.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import shutil
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from appdb.base import utcnow
from appdb.engine import session_scope
from auth.service import CurrentUser
from curation import members, settings
from curation.models import CurationDatasetVersion, CurationProject, CurationRelease, CurationVersionInput
from curation.versions import SPLITS, _slug

logger = logging.getLogger(__name__)
FILES = ("manifest.csv", "manifest.json", "CHANGELOG.md", "conflicts.csv", "leakage.csv")
COLUMNS = ["project", "project_version", "item_id", "label", "split", "source_label", "crop_uri", "source_image_uri",
           "row", "column", "crop_size", "region", "island", "site", "year", "annotator", "source_key", "file_name"]


def release_dir(release: CurationRelease) -> Path:
    return settings.versions_dir() / "_releases" / _slug(release.name) / f"r{release.number}"


def as_dict(release: CurationRelease) -> dict:
    return {"id": release.id, "name": release.name, "number": release.number, "dataset_mode": release.dataset_mode,
            "status": release.status, "notes": release.notes, "sources": release.sources or [],
            "statistics": release.statistics or {}, "item_count": release.item_count,
            "output_uri": release.output_uri, "error_message": release.error_message,
            "created_by_name": release.created_by_name, "created_at": release.created_at,
            "published_at": release.published_at, "files": list(FILES) if release.status == "published" else []}


def start(db: Session, user: CurrentUser, name: str, dataset_mode: str, sources: list[dict],
          notes: Optional[str]) -> CurationRelease:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Give the release a name")
    if not sources:
        raise HTTPException(status_code=422, detail="Choose at least one project version")
    if len({s["project_id"] for s in sources}) != len(sources):
        raise HTTPException(status_code=422, detail="Each project can appear only once")
    if not user.has_role("lead"):
        raise HTTPException(status_code=403, detail="Building a release needs the app-wide lead role")
    resolved = []
    for source in sources:
        project = db.get(CurationProject, source["project_id"])
        if project is None:
            raise HTTPException(status_code=422, detail="Unknown project")
        version = db.scalar(select(CurationDatasetVersion).where(
            CurationDatasetVersion.project_id == project.id, CurationDatasetVersion.version == source["version"]))
        if version is None or version.status != "published":
            raise HTTPException(status_code=422, detail=f"{project.name} has no published v{source['version']}")
        if project.dataset_mode != dataset_mode:
            raise HTTPException(status_code=422, detail=f"{project.name} is {project.dataset_mode}, not {dataset_mode}")
        if members.project_role(db, project.id, user) != "lead":
            raise HTTPException(status_code=403, detail=f"You need the project lead role on {project.name}")
        resolved.append({"project_id": project.id, "project_name": project.name, "version": version.version,
                         "version_id": version.id, "notes": version.notes, "item_count": version.item_count})
    for _ in range(3):
        number = (db.scalar(select(func.max(CurationRelease.number)).where(CurationRelease.name == name)) or 0) + 1
        release = CurationRelease(name=name, number=number, dataset_mode=dataset_mode, status="building",
                                  notes=(notes or "").strip() or None, sources=resolved,
                                  created_by_user_id=user.id, created_by_name=user.label)
        db.add(release)
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
    else:
        raise HTTPException(status_code=409, detail="Another release with this name is being numbered; try again")
    db.refresh(release)
    threading.Thread(target=_build, args=(release.id,), name=f"release-{release.id}", daemon=True).start()
    return release


def _build(release_id: int) -> None:
    folder: Optional[Path] = None
    try:
        with session_scope() as db:
            release = db.get(CurationRelease, release_id)
            folder = release_dir(release)
            if folder.exists():
                shutil.rmtree(folder)
            folder.mkdir(parents=True)
            kept: dict[tuple, tuple] = {}
            groups: dict[tuple, list] = defaultdict(list)   # every row per crop key, for conflicts.csv
            conflicted: set[tuple] = set()
            duplicates = 0
            for source in release.sources:
                rows = db.execute(select(
                    CurationVersionInput.item_id, CurationVersionInput.label, CurationVersionInput.split,
                    CurationVersionInput.source_label, CurationVersionInput.crop_uri,
                    CurationVersionInput.source_image_uri, CurationVersionInput.row_px, CurationVersionInput.column_px,
                    CurationVersionInput.crop_size, CurationVersionInput.region, CurationVersionInput.island,
                    CurationVersionInput.site, CurationVersionInput.survey_year, CurationVersionInput.annotator,
                    CurationVersionInput.source_key, CurationVersionInput.file_name)
                    .where(CurationVersionInput.version_id == source["version_id"])
                    .order_by(CurationVersionInput.item_id).execution_options(yield_per=5000))
                for row in rows:
                    record = (source["project_name"], source["version"], *row)
                    key = (row.file_name, row.source_label)
                    groups[key].append(record)
                    if key in conflicted:
                        continue
                    if key in kept:
                        if kept[key][3] == row.label:
                            duplicates += 1
                        else:
                            conflicted.add(key)
                            del kept[key]
                        continue
                    kept[key] = record
            _write(folder, release, kept, groups, conflicted, duplicates)
            release.status = "published"
            release.published_at = utcnow()
            release.output_uri = _upload_dir(folder, release) or str(folder)
    except Exception as exc:  # noqa: BLE001 — the release carries the reason
        logger.exception("Release %s failed", release_id)
        if folder is not None and folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        with session_scope() as db:
            failed = db.get(CurationRelease, release_id)
            failed.status = "failed"
            failed.error_message = str(exc)[:2000] or type(exc).__name__


def _write(folder: Path, release: CurationRelease, kept: dict, groups: dict, conflicted: set, duplicates: int) -> None:
    classes: dict[str, dict[str, int]] = defaultdict(lambda: {s: 0 for s in SPLITS})
    per_source: dict[str, int] = defaultdict(int)
    image_splits: dict[str, set] = defaultdict(set)
    image_owners: dict[str, set] = defaultdict(set)
    with (folder / "manifest.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        for record in sorted(kept.values(), key=lambda r: (r[4], r[3], r[0], r[2])):
            writer.writerow(record)
            project, version, _, label, split = record[:5]
            classes[label][split if split in SPLITS else "train"] += 1
            per_source[f"{project} v{version}"] += 1
            image_splits[record[7]].add(split)
            image_owners[record[7]].add(f"{project}:{split}")
    with (folder / "conflicts.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["file_name", "source_label", "project", "project_version", "item_id", "label", "split"])
        for key in sorted(conflicted):
            for record in groups[key]:
                writer.writerow([key[0], key[1], record[0], record[1], record[2], record[3], record[4]])
    leaks = sorted(image for image, splits in image_splits.items() if len(splits) > 1)
    with (folder / "leakage.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["source_image_uri", "project_splits"])
        for image in leaks:
            writer.writerow([image, " ".join(sorted(image_owners[image]))])
    release.statistics = {
        "classes": {label: dict(counts) for label, counts in sorted(classes.items())},
        "splits": {s: sum(c[s] for c in classes.values()) for s in SPLITS},
        "per_source": dict(per_source), "duplicates": duplicates, "conflicts": len(conflicted),
        "leakage": len(leaks), "class_count": len(classes),
    }
    release.item_count = len(kept)
    summary = {"release": release.name, "number": release.number, "dataset_mode": release.dataset_mode,
               "notes": release.notes, "sources": release.sources, "statistics": release.statistics,
               "manifest_sha256": hashlib.sha256((folder / "manifest.csv").read_bytes()).hexdigest(),
               "built_by": release.created_by_name, "built_at": utcnow().isoformat() + "Z"}
    (folder / "manifest.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (folder / "CHANGELOG.md").write_text(_changelog(release), encoding="utf-8")


def _changelog(release: CurationRelease) -> str:
    stats = release.statistics
    lines = [f"# {release.name} — r{release.number}", "",
             f"Built by {release.created_by_name or 'unknown'} on {utcnow():%Y-%m-%d %H:%M} UTC "
             f"({release.dataset_mode})."]
    if release.notes:
        lines += ["", f"> {release.notes}"]
    lines += ["", "## Sources", ""] + [
        f"- {s['project_name']} v{s['version']} — {s['item_count']:,} items" + (f": {s['notes']}" if s.get("notes") else "")
        for s in release.sources]
    with session_scope() as db:
        previous = db.scalar(select(CurationRelease).where(
            CurationRelease.name == release.name, CurationRelease.number < release.number,
            CurationRelease.status == "published").order_by(CurationRelease.number.desc()).limit(1))
        before = {s["project_name"]: s["version"] for s in (previous.sources if previous else [])}
    now = {s["project_name"]: s["version"] for s in release.sources}
    if previous is not None:
        moves = [f"- {p}: v{before[p]} → v{now[p]}" for p in sorted(now) if p in before and before[p] != now[p]]
        moves += [f"- {p}: added (v{now[p]})" for p in sorted(now) if p not in before]
        moves += [f"- {p}: removed" for p in sorted(before) if p not in now]
        lines += ["", f"## Since r{previous.number}", ""] + (moves or ["- Sources unchanged."])
    splits = stats["splits"]
    lines += ["", "## Totals", "",
              f"- {release.item_count:,} items (train {splits['train']:,} · val {splits['val']:,} · test {splits['test']:,})",
              f"- {stats['duplicates']:,} duplicate crops kept once",
              f"- {stats['conflicts']:,} crops left out for conflicting labels (conflicts.csv)",
              f"- {stats['leakage']:,} source images in more than one split (leakage.csv)"]
    return "\n".join(lines) + "\n"


def _upload_dir(folder: Path, release: CurationRelease) -> Optional[str]:
    """Copy the release's files to CURATION_PUBLISH_URI (gs://…) if set, as versions._upload does."""
    target = settings.publish_uri()
    if not target:
        return None
    from library.storage.gcs import GCSBackend, parse_gs_uri  # noqa: PLC0415

    bucket, prefix = parse_gs_uri(target.rstrip("/") + f"/_releases/{_slug(release.name)}/r{release.number}/")
    backend = GCSBackend(bucket)
    for name in FILES:
        backend.write_bytes(f"{prefix}{name}", (folder / name).read_bytes())
    return f"gs://{bucket}/{prefix}"


def mark_interrupted(db: Session) -> int:
    count = 0
    for release in db.scalars(select(CurationRelease).where(CurationRelease.status == "building")):
        release.status = "failed"
        release.error_message = "The server restarted while this release was being built — build it again."
        count += 1
    return count
```

**Before writing `_upload_dir`**, read `versions._upload`. Use exactly the GCS calls it uses (backend construction, write method, prefix handling); the names above are a sketch of that pattern.

- [ ] **Step 4: Routes.** Append to `routers/releases.py`, adding imports for `HTTPException`, `FileResponse`, `BaseModel`, `Field`, `Optional`, `select`, `releases` and `CurationRelease`:

```python
class SourceIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=1)


class ReleaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dataset_mode: str = Field(pattern="^(T1|T3)$")
    sources: list[SourceIn] = Field(default_factory=list, max_length=50)
    notes: Optional[str] = Field(default=None, max_length=4000)


@router.get("/releases")
def list_releases(db: Session = Depends(get_db), _me: CurrentUser = Depends(current_user)):
    rows = db.scalars(select(CurationRelease).order_by(CurationRelease.id.desc())).all()
    return [releases.as_dict(r) for r in rows]


@router.post("/releases", status_code=202)
def build_release(body: ReleaseIn, me: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    release = releases.start(db, me, body.name, body.dataset_mode, [s.model_dump() for s in body.sources], body.notes)
    return releases.as_dict(release)


@router.get("/releases/{release_id}")
def get_release(release_id: int, db: Session = Depends(get_db), _me: CurrentUser = Depends(current_user)):
    release = db.get(CurationRelease, release_id)
    if release is None:
        raise HTTPException(status_code=404, detail="Release not found")
    return releases.as_dict(release)


@router.get("/releases/{release_id}/files/{name}")
def release_file(release_id: int, name: str, db: Session = Depends(get_db), _me: CurrentUser = Depends(current_user)):
    release = db.get(CurationRelease, release_id)
    if release is None or name not in releases.FILES:
        raise HTTPException(status_code=404, detail="No such file")
    path = releases.release_dir(release) / name
    if release.status != "published" or not path.is_file():
        raise HTTPException(status_code=404, detail="This release's files are not available")
    media = "text/csv" if name.endswith(".csv") else "application/json" if name.endswith(".json") else "text/markdown"
    return FileResponse(path, media_type=media, filename=f"{releases._slug(release.name)}_r{release.number}_{name}")  # noqa: SLF001
```

An empty `sources` must give 422 from `releases.start`, not from pydantic, so the message stays clear.

- [ ] **Step 5: Recovery.** In `curation/importer.py`, at the recovery hook (~line 418), add `from curation import releases  # noqa: PLC0415` next to the `versions` import, and change the count line to:

```python
        count = mark_interrupted_imports(db) + versions.mark_interrupted(db) + releases.mark_interrupted(db)
```

- [ ] **Step 6: Oracle list and run**

Add `tests/test_releases.py` to `test-oracle`. Run TEST `tests/test_releases.py` and the Oracle command for it. Expected: PASS on both.

- [ ] **Step 7: Commit**

```bash
git add curation/releases.py routers/releases.py curation/importer.py tests/test_releases.py docker-compose.stack.yml
git commit -m "Combined releases from published versions: duplicates once, conflicts and leakage listed, reproducible"
```

---

### Task 8: UI — project roles, the members dialog, and the read-only notice

**Files:**
- Create: `static/curation/collab.js`
- Modify: `templates/curation.html`:
  - the script tag after `hub.js`;
  - header buttons `members-button`, `releases-button` (Task 10 wires Releases);
  - `#membership-notice`;
  - `#members-dialog`;
  - `data-min-role` changes.
- Modify: `static/curation/curation.js`:
  - `canDo` and `roleFor` for project roles;
  - re-apply gates on project change;
  - the project-scoped `canDo(…)` calls.
- Modify: `tests/test_curation.py` (the integrity test also scans `collab.js`; a string check)

**Interfaces:**
- Consumes:
  - `my_role` on projects;
  - the members routes and `GET /api/curation/users` (Task 2).
- Produces:
  - `canDo('project-curator')` and `canDo('project-lead')`;
  - the global functions in `collab.js`: `renderMembership()`, `openMembers()`.
  - `collab.js` uses these globals from `curation.js`: `byId`, `request`, `state`, `session`, `canDo`, `escapeHtml`, `showNotice`, `currentProject`, `timeAgo`, `fmt`.

- [ ] **Step 1: Write the failing checks.** In `test_curation_page_renders_and_every_scripted_element_exists`, change the script-ids line to scan both files:

```python
    script_ids = set()
    for name in ("static/curation/curation.js", "static/curation/collab.js"):
        script_ids |= set(re.findall(r"byId\('([a-z0-9-]+)'\)", Path(name).read_text(encoding="utf-8")))
```

Then append this test:

```python
def test_curation_page_gates_project_work_by_project_role(client):
    page = client.get("/curation").text
    assert "/static/curation/collab.js" in page
    for element in ('id="members-button"', 'id="releases-button"', 'id="membership-notice"', 'id="members-dialog"'):
        assert element in page
    assert 'data-min-role="project-curator"' in page and 'data-min-role="project-lead"' in page
    assert 'id="publish-button" class="btn btn-primary" type="submit" data-min-role="project-lead"' in page
    script = client.get("/static/curation/curation.js").text
    assert "'project-curator'" in script and "'project-lead'" in script
```

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_curation.py -k "page_renders or project_role"`
Expected: FAIL; `collab.js` is missing.

- [ ] **Step 3: `canDo` in `curation.js`.** Replace the `roleFor`/`canDo` lines (~15-18) with:

```js
const roleFor = role => role === 'create' ? CREATE_ROLE : role
// Project work follows the project role (members only); app-wide admins lead everywhere.
const projectRole = () => currentProject()?.my_role || null
const canDo = role => {
  if (!session.user) return false
  if (role === 'project-curator') return session.user.role === 'admin' || ['curator', 'lead'].includes(projectRole())
  if (role === 'project-lead') return session.user.role === 'admin' || projectRole() === 'lead'
  return ROLE_ORDER.indexOf(session.user.role) >= ROLE_ORDER.indexOf(roleFor(role))
}
const ROLE_NAMES = { 'project-curator': 'project curator', 'project-lead': 'project lead' }
```

Then:
- In `applyRoleGates`, change the title line to `if (!allowed) control.title = \`Needs the ${ROLE_NAMES[control.dataset.minRole] || roleFor(control.dataset.minRole)} role\``.
- At the end of `renderProjectMode()`, add:

```js
  applyRoleGates()
  if (typeof renderMembership === 'function') renderMembership()
```

- [ ] **Step 4: Project-scoped gates.**
- **Template:** change `data-min-role` on these controls:
  - `quality-accept-all`, `quality-reject-all`, `add-point-mode`, the item editor submit (~line 294), `bulk-action`, `apply-bulk`, `matrix-bulk-action`, `apply-matrix-bulk` → `project-curator`;
  - `cancel-import`, `resume-import`, `start-evaluation` → `project-curator`;
  - `publish-button` → `project-lead`.
  - Leave `import-noaa-button`, `new-project-button`, `data-create-project`, `model-from-hub`, `register-evaluation-model`, `project-from-hub` (`create`) and `delete-project-button` (`lead`) as they are.
- **`curation.js`:** go through every `canDo('curator')`, `canDo('lead')` and `canDo('create')` (`grep -n "canDo('" static/curation/curation.js`). Use `'project-curator'` wherever the control acts on the open project:
  - quality cards;
  - crop and matrix bulk;
  - matrix relabel;
  - points;
  - imports;
  - resume;
  - start and cancel evaluation.

  Use `'project-lead'` for project-lead actions:
  - write image folders;
  - publish.

  Keep the app-wide ones:
  - `empty-projects-role` (`create`);
  - validating evaluation models (`create`);
  - anything about creating projects or registering.

  Do the same in `hub.js`: resume import → `'project-curator'`; Apply link → `'project-lead'`.

- [ ] **Step 5: Template additions.**
- After the Registry button in the curation header:

```html
      <button id="members-button" class="btn btn-outline-light" type="button" title="Who works on this project"><i class="bi bi-people"></i> Members</button>
      <button id="releases-button" class="btn btn-outline-light" type="button" title="Combined releases and project comparison"><i class="bi bi-collection"></i> Releases</button>
```

- Directly after the curation header, the notice:

```html
  <div id="membership-notice" class="alert alert-info py-2 small d-none" role="status"></div>
```

- With the other dialogs:

```html
<dialog id="members-dialog" class="hub-dialog">
  <div class="curation-panel-title"><div><strong>Members</strong><small id="members-subtitle"></small></div>
    <button class="btn btn-sm btn-outline-secondary icon-button" type="button" data-close-dialog="members-dialog" title="Close"><i class="bi bi-x-lg"></i></button></div>
  <div id="members-list"></div>
  <form id="members-add" class="d-flex gap-2 mt-3">
    <input id="members-username" class="form-control form-control-sm" list="members-user-options" placeholder="Username" aria-label="Username">
    <datalist id="members-user-options"></datalist>
    <select id="members-role" class="form-select form-select-sm" aria-label="Role"><option value="curator">Curator</option><option value="lead">Lead</option></select>
    <button id="members-add-button" class="btn btn-sm btn-primary" type="submit" data-min-role="project-lead">Add</button>
  </form>
  <p id="members-message" class="small mt-2 mb-0" role="status"></p>
</dialog>
```

- After `<script src="/static/curation/hub.js"></script>`, add `<script src="/static/curation/collab.js"></script>`.
- Check how `data-close-dialog` buttons are wired in `hub.js` (`grep -n "data-close-dialog" static/curation/*.js`). If the wiring is per dialog, wire this one the same way in `collab.js`.

- [ ] **Step 6: Create `static/curation/collab.js`**

```js
// Collaboration: project members, change sets and history, comparing people,
// combined releases and project comparison. Classic script loaded after
// curation.js and hub.js; uses their globals (byId, request, state, session,
// canDo, escapeHtml, showNotice, currentProject, timeAgo, fmt).

// ── Members ──────────────────────────────────────────────────────────────────
let membersCache = { projectId: null, list: [] }

async function projectMembers(force = false) {
  if (!state.projectId) return []
  if (force || membersCache.projectId !== state.projectId) {
    membersCache = { projectId: state.projectId, list: await request(`/api/curation/projects/${state.projectId}/members`) }
  }
  return membersCache.list
}

async function renderMembership() {
  const notice = byId('membership-notice')
  const project = currentProject()
  if (!project || !session.user || canDo('project-curator')) { notice.classList.add('d-none'); return }
  let leads = []
  try { leads = (await projectMembers()).filter(m => m.role === 'lead').map(m => m.display_name || m.username) } catch (_) {}
  notice.textContent = `You can view this project. To curate it, ask a project lead${leads.length ? ` (${leads.join(', ')})` : ''} to add you.`
  notice.classList.remove('d-none')
}

async function openMembers() {
  if (!state.projectId) return
  byId('members-dialog').showModal()
  byId('members-message').textContent = ''
  byId('members-subtitle').textContent = currentProject()?.name || ''
  await renderMembersList(true)
}

async function renderMembersList(force) {
  const list = byId('members-list')
  try {
    const members = await projectMembers(force)
    const lead = canDo('project-lead')
    list.innerHTML = `<table class="table table-sm mb-0"><thead><tr><th>Name</th><th>Role</th><th>Added</th><th></th></tr></thead><tbody>${members.map(m => `
      <tr><td>${escapeHtml(m.display_name || m.username)} <small class="text-muted">${escapeHtml(m.username)}</small></td>
      <td>${lead ? `<select class="form-select form-select-sm" data-member-role="${m.user_id}"><option value="curator" ${m.role === 'curator' ? 'selected' : ''}>Curator</option><option value="lead" ${m.role === 'lead' ? 'selected' : ''}>Lead</option></select>` : escapeHtml(m.role)}</td>
      <td><small>${escapeHtml(m.added_by_name || '')} ${escapeHtml(timeAgo(m.added_at))}</small></td>
      <td>${lead ? `<button type="button" class="btn btn-sm btn-outline-danger" data-member-remove="${m.user_id}">Remove</button>` : ''}</td></tr>`).join('')}</tbody></table>`
    list.querySelectorAll('[data-member-role]').forEach(select => select.addEventListener('change', () =>
      memberAction(`/members/${select.dataset.memberRole}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ role: select.value }) })))
    list.querySelectorAll('[data-member-remove]').forEach(button => button.addEventListener('click', () => {
      if (window.confirm('Remove this member from the project? Their past changes stay in the history.')) memberAction(`/members/${button.dataset.memberRemove}`, { method: 'DELETE' })
    }))
  } catch (error) { list.textContent = error.message }
}

async function memberAction(path, options) {
  const message = byId('members-message')
  try {
    await request(`/api/curation/projects/${state.projectId}${path}`, options)
    message.textContent = 'Saved.'
    message.className = 'small mt-2 mb-0 text-success'
    await refreshMyRole()
    await renderMembersList(true)
  } catch (error) {
    message.textContent = error.message
    message.className = 'small mt-2 mb-0 text-danger'
    await renderMembersList(true)
  }
}

async function refreshMyRole() {
  const fresh = await request(`/api/curation/projects/${state.projectId}`)
  const project = currentProject()
  if (project) project.my_role = fresh.my_role
  applyRoleGates()
  renderMembership()
}

byId('members-button').addEventListener('click', openMembers)
byId('members-add').addEventListener('submit', event => {
  event.preventDefault()
  const username = byId('members-username').value.trim()
  if (!username) return
  memberAction('/members', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, role: byId('members-role').value }) })
    .then(() => { byId('members-username').value = '' })
})
byId('members-username').addEventListener('input', async event => {
  const query = event.target.value.trim()
  if (query.length < 2) return
  try {
    const users = await request(`/api/curation/users?query=${encodeURIComponent(query)}`)
    byId('members-user-options').innerHTML = users.map(u => `<option value="${escapeHtml(u.username)}">${escapeHtml(u.display_name || '')}</option>`).join('')
  } catch (_) { /* the form still accepts a typed username */ }
})
```

`membersCache` must be reset when the project changes. The `projectId` check in `projectMembers` covers this.

- [ ] **Step 7: Run**

Run: TEST `tests/test_curation.py` and the JS syntax check, now including `collab.js`.
Expected: PASS, SYNTAX-OK.

- [ ] **Step 8: Commit**

```bash
git add static/curation/collab.js static/curation/curation.js static/curation/hub.js templates/curation.html tests/test_curation.py
git commit -m "Curation page follows the project role; members dialog; read-only notice for non-members"
```

---

### Task 9: UI — commit pill, commit dialog, History

**Files:**
- Modify: `templates/curation.html` (`#commit-pill`, `#commit-dialog`, the History section at the top of `#versions-panel`)
- Modify: `static/curation/collab.js` (commit and history)
- Modify: `static/curation/curation.js` (hooks: after `loadStats`, after `loadVersions`, and preview rendering of `change_sets`/`uncommitted`)
- Test: `tests/test_curation.py` (string and element check)

**Interfaces:**
- Consumes: the change-set routes (Task 3); `preview.change_sets` and `preview.uncommitted` (Task 4).
- Produces: `refreshCommitPill()` and `loadHistory()` in `collab.js`. The crop-grid item filter is `state.matrixLinkedItemIds`, or whatever `loadCrops` uses for `item_ids`; check with `grep -n "item_ids" static/curation/curation.js` and reuse the mechanism the matrix uses to show linked items.

- [ ] **Step 1: Write the failing check**

```python
def test_curation_page_has_commit_and_history(client):
    page = client.get("/curation").text
    for element in ('id="commit-pill"', 'id="commit-dialog"', 'id="commit-message"', 'id="commit-summary"',
                    'id="commit-confirm"', 'id="history-list"', 'id="history-person"', 'id="history-since"',
                    'id="history-uncommitted"'):
        assert element in page
    script = client.get("/static/curation/collab.js").text
    assert "function refreshCommitPill(" in script and "function loadHistory(" in script and "/change-sets" in script
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_curation.py::test_curation_page_has_commit_and_history`
Expected: FAIL.

- [ ] **Step 3: Template.**
- In the curation header, before the Members button:

```html
      <button id="commit-pill" class="btn btn-warning d-none" type="button" title="Group your changes under a message"></button>
```

- The dialog:

```html
<dialog id="commit-dialog" class="hub-dialog">
  <div class="curation-panel-title"><div><strong>Commit your changes</strong><small>Your edits are already live; this groups them under a message others can read.</small></div>
    <button class="btn btn-sm btn-outline-secondary icon-button" type="button" data-close-dialog="commit-dialog" title="Close"><i class="bi bi-x-lg"></i></button></div>
  <label class="form-label" for="commit-message">Message <span class="text-muted" id="commit-counter">0/500</span></label>
  <textarea id="commit-message" class="form-control" rows="2" maxlength="500" placeholder="e.g. Added crops for rare species ACAS"></textarea>
  <div id="commit-summary" class="small mt-2"></div>
  <div id="commit-recent" class="small mt-2 commit-recent"></div>
  <div class="d-flex gap-2 mt-3"><button id="commit-confirm" class="btn btn-primary" type="button" disabled>Commit</button><span id="commit-error" class="small text-danger"></span></div>
</dialog>
```

- First child of `#versions-panel`, before the compare bar:

```html
    <section class="history-section">
      <div class="curation-panel-title"><div><strong>History</strong><small>Change sets, newest first</small></div>
        <div class="d-flex gap-2">
          <select id="history-person" class="form-select form-select-sm" aria-label="Person"><option value="">Everyone</option></select>
          <select id="history-since" class="form-select form-select-sm" aria-label="Since"><option value="">All time</option></select>
        </div></div>
      <div id="history-uncommitted" class="small text-muted mb-2"></div>
      <div id="history-list" class="history-list"></div>
    </section>
```

- [ ] **Step 4: `collab.js`, change sets.** Append:

```js
// ── Change sets ──────────────────────────────────────────────────────────────
const KIND_NAMES = { relabel: 'relabels', inclusion: 'inclusion changes', quality: 'quality decisions', point: 'points added', review: 'review changes', added: 'items added', other: 'other' }
const describeKinds = kinds => Object.entries(kinds || {}).map(([kind, n]) => `${fmt(n)} ${KIND_NAMES[kind] || kind}`).join(' · ')
const describeClasses = classes => (classes || []).map(c => `${escapeHtml(c.label)} ${fmt(c.items)}`).join(' · ')
let commitTarget = null   // null = me; a user id when a lead commits for someone

async function refreshCommitPill() {
  const pill = byId('commit-pill')
  if (!state.projectId || !canDo('project-curator')) { pill.classList.add('d-none'); return }
  try {
    const mine = await request(`/api/curation/projects/${state.projectId}/change-sets/pending?user=me`)
    pill.textContent = `${fmt(mine.count)} uncommitted change${mine.count === 1 ? '' : 's'} · Commit…`
    pill.classList.toggle('d-none', !mine.count)
  } catch (_) { pill.classList.add('d-none') }
}

async function openCommit(userId = null, name = null) {
  commitTarget = userId
  const pending = await request(`/api/curation/projects/${state.projectId}/change-sets/pending?user=${userId ?? 'me'}`)
  byId('commit-dialog').querySelector('strong').textContent = name ? `Commit ${name}'s changes` : 'Commit your changes'
  byId('commit-summary').innerHTML = `<b>${fmt(pending.count)} changes</b>: ${escapeHtml(describeKinds(pending.summary.kinds))}<br>Classes: ${describeClasses(pending.summary.classes) || '—'}`
  byId('commit-recent').innerHTML = pending.recent.map(e => `<div>${escapeHtml(e.event_type.replaceAll('_', ' '))} · item ${e.item_id ?? '—'}${e.previous_value?.current_label && e.new_value?.current_label && e.previous_value.current_label !== e.new_value.current_label ? ` · ${escapeHtml(e.previous_value.current_label)} → ${escapeHtml(e.new_value.current_label)}` : ''} · ${escapeHtml(timeAgo(e.created_at))}</div>`).join('')
  byId('commit-message').value = ''
  byId('commit-error').textContent = ''
  updateCommitButton()
  byId('commit-dialog').showModal()
}

function updateCommitButton() {
  const length = byId('commit-message').value.trim().length
  byId('commit-counter').textContent = `${length}/500`
  byId('commit-confirm').disabled = length < 1 || length > 500
}

async function confirmCommit() {
  byId('commit-confirm').disabled = true
  try {
    const body = { message: byId('commit-message').value.trim() }
    if (commitTarget !== null) body.user_id = commitTarget
    const done = await request(`/api/curation/projects/${state.projectId}/change-sets`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    byId('commit-dialog').close()
    showNotice(`Committed ${fmt(done.change_count)} changes: “${done.message}”`)
    refreshCommitPill()
    if (state.view === 'versions') loadHistory()
  } catch (error) {
    byId('commit-error').textContent = error.message
    updateCommitButton()
  }
}

byId('commit-pill').addEventListener('click', () => openCommit())
byId('commit-message').addEventListener('input', updateCommitButton)
byId('commit-confirm').addEventListener('click', confirmCommit)

// ── History ──────────────────────────────────────────────────────────────────
async function loadHistory() {
  if (!state.projectId) return
  const projectId = state.projectId
  const params = new URLSearchParams()
  if (byId('history-person').value) params.set('user_id', byId('history-person').value)
  if (byId('history-since').value) params.set('since_version', byId('history-since').value)
  const base = `/api/curation/projects/${projectId}/change-sets`
  const [sets, open, versionList] = await Promise.all([request(`${base}?${params}`), request(`${base}/uncommitted`), request(`/api/curation/projects/${projectId}/versions`)])
  if (projectId !== state.projectId) return
  fillHistoryFilters(sets, open, versionList.versions)
  const lead = canDo('project-lead')
  byId('history-uncommitted').innerHTML = open.length
    ? `Uncommitted: ${open.map(u => `${escapeHtml(u.name)} ${fmt(u.count)}${lead ? ` <button type="button" class="btn btn-link btn-sm p-0" data-commit-for="${u.user_id}" data-commit-name="${escapeHtml(u.name)}">Commit for ${escapeHtml(u.name)}</button>` : ''}`).join(' · ')}`
    : 'Nothing uncommitted.'
  byId('history-list').innerHTML = sets.length ? sets.map(cs => `<article class="history-item">
      <header><b>${escapeHtml(cs.author_name)}</b> · ${escapeHtml(cs.message)} <span class="version-meta">${fmt(cs.change_count)} changes · ${escapeHtml(timeAgo(cs.created_at))}${cs.committed_by_name ? ` · committed by ${escapeHtml(cs.committed_by_name)}` : ''}</span></header>
      <div class="small">${escapeHtml(describeKinds(cs.summary.kinds))}${cs.summary.classes?.length ? ` · ${describeClasses(cs.summary.classes)}` : ''}</div>
      <button type="button" class="btn btn-sm btn-outline-secondary mt-1" data-history-open="${cs.id}">Changes</button>
      <div class="history-detail d-none" data-history-detail="${cs.id}"></div></article>`).join('')
    : '<p class="text-muted small">No change sets yet. Edit, then commit with a message.</p>'
  document.querySelectorAll('[data-commit-for]').forEach(b => b.addEventListener('click', () => openCommit(Number(b.dataset.commitFor), b.dataset.commitName)))
  document.querySelectorAll('[data-history-open]').forEach(b => b.addEventListener('click', () => toggleHistoryDetail(b.dataset.historyOpen)))
}

function fillHistoryFilters(sets, open, versions) {
  const person = byId('history-person')
  const keepPerson = person.value
  const people = new Map()
  sets.forEach(cs => cs.author_user_id && people.set(String(cs.author_user_id), cs.author_name))
  open.forEach(u => people.set(String(u.user_id), u.name))
  person.innerHTML = '<option value="">Everyone</option>' + [...people].map(([id, name]) => `<option value="${id}">${escapeHtml(name)}</option>`).join('')
  person.value = people.has(keepPerson) ? keepPerson : ''
  const since = byId('history-since')
  const keepSince = since.value
  const published = versions.filter(v => v.status === 'published').map(v => String(v.version))
  since.innerHTML = '<option value="">All time</option>' + published.map(n => `<option value="${n}">Since v${n}</option>`).join('')
  since.value = published.includes(keepSince) ? keepSince : ''
}

async function toggleHistoryDetail(id) {
  const box = document.querySelector(`[data-history-detail="${id}"]`)
  const opening = box.classList.contains('d-none')
  box.classList.toggle('d-none', !opening)
  if (!opening || box.dataset.loaded) return
  try {
    const detail = await request(`/api/curation/projects/${state.projectId}/change-sets/${id}`)
    box.innerHTML = `<button type="button" class="btn btn-sm btn-primary my-1" data-history-show="${id}">Show these ${fmt(detail.item_ids.length)} items</button>` +
      detail.events.map(e => `<div class="small">${escapeHtml(e.event_type.replaceAll('_', ' '))} · item ${e.item_id ?? '—'}${['current_label', 'included', 'review_state'].map(field => e.previous_value && field in e.previous_value && e.previous_value[field] !== e.new_value?.[field] ? ` · ${field.replace('_', ' ')} ${escapeHtml(String(e.previous_value[field]))} → ${escapeHtml(String(e.new_value?.[field]))}` : '').join('')}${e.reason ? ` · “${escapeHtml(e.reason)}”` : ''}</div>`).join('')
    box.dataset.loaded = '1'
    box.querySelector('[data-history-show]').addEventListener('click', () => showItems(detail.item_ids))
  } catch (error) { box.textContent = error.message }
}

// Open the crop grid on exactly these items (the same mechanism the matrix uses for linked items).
function showItems(itemIds) {
  if (!itemIds.length) return
  state.matrixLinkedItemIds = itemIds.slice(0, 5000)
  setView('crop')
}

byId('history-person').addEventListener('change', loadHistory)
byId('history-since').addEventListener('change', loadHistory)
```

**Before relying on `showItems`:** read how the matrix sends linked item ids into the crop grid (`grep -n "matrixLinkedItemIds\|item_ids" static/curation/curation.js`). Make `showItems` use that exact mechanism, including clearing any filter that would hide the items. The test for P1's matrix drill-down shows the crop grid honours `item_ids`.

- [ ] **Step 5: `curation.js` hooks.**
- At the end of `loadStats()`: `if (typeof refreshCommitPill === 'function') refreshCommitPill()`.
- At the end of `loadVersions()`, after rendering: `if (typeof loadHistory === 'function') loadHistory().catch(error => showNotice(error.message))`.
- In `renderPreview(preview)`, append to `#preview-contributors` a line listing `preview.change_sets` (author: message, N changes) and `preview.uncommitted` ("Uncommitted: Ana 12"). Escape everything, and guard for missing keys with `|| []`.

- [ ] **Step 6: Styles.** Append to `static/curation.css`:

```css
.history-section { margin-bottom: 16px; }
.history-item { border-bottom: 1px solid #e6e9ec; padding: 8px 0; }
.history-detail { margin-top: 6px; padding-left: 8px; border-left: 3px solid #e6e9ec; max-height: 320px; overflow: auto; }
.commit-recent { max-height: 200px; overflow: auto; }
```

- [ ] **Step 7: Run**

Run: TEST `tests/test_curation.py` and the JS syntax check.
Expected: PASS, SYNTAX-OK.

- [ ] **Step 8: Commit**

```bash
git add templates/curation.html static/curation/collab.js static/curation/curation.js static/curation.css tests/test_curation.py
git commit -m "Commit pill and dialog; History of change sets with Show these items; preview lists change sets"
```

---

### Task 10: UI — compare people, releases panel, compare projects

**Files:**
- Modify: `templates/curation.html`:
  - the Compare people form in the dataset status panel, after `#status-curators`;
  - `#releases-dialog` (list, detail, New release form, Compare projects form).
- Modify: `static/curation/collab.js`
- Test: `tests/test_curation.py`

**Interfaces:**
- Consumes:
  - `/people/compare` (Task 5);
  - `/compare-projects` (Task 6);
  - `/releases` (Task 7);
  - `renderBreakdownTable(container, data)` (P1, in `curation.js`). It takes `{by, splits, rows: [{key, counts, total, rare}], totals, total, rare_min}`. Build that shape from release statistics as below.

- [ ] **Step 1: Write the failing check**

```python
def test_curation_page_has_people_compare_and_releases(client):
    page = client.get("/curation").text
    for element in ('id="people-a"', 'id="people-b"', 'id="people-since"', 'id="people-run"', 'id="people-result"',
                    'id="releases-dialog"', 'id="releases-list"', 'id="release-detail"', 'id="release-form"',
                    'id="release-name"', 'id="release-mode"', 'id="release-sources"', 'id="release-notes"',
                    'id="release-build"', 'id="pc-a"', 'id="pc-a-version"', 'id="pc-b"', 'id="pc-b-version"',
                    'id="pc-run"', 'id="pc-result"'):
        assert element in page
    script = client.get("/static/curation/collab.js").text
    for needle in ("/people/compare", "/compare-projects", "/api/curation/releases", "renderBreakdownTable("):
        assert needle in script
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_curation.py::test_curation_page_has_people_compare_and_releases`
Expected: FAIL.

- [ ] **Step 3: Template.**
- After `<div id="status-curators" …></div>` and its heading:

```html
        <h2 class="mt-3">Compare people</h2>
        <div class="d-flex flex-wrap gap-2 align-items-center">
          <select id="people-a" class="form-select form-select-sm w-auto" aria-label="Person A"></select>
          <select id="people-b" class="form-select form-select-sm w-auto" aria-label="Person B"></select>
          <select id="people-since" class="form-select form-select-sm w-auto" aria-label="Since"><option value="">All time</option></select>
          <button id="people-run" class="btn btn-sm btn-outline-primary" type="button">Compare</button>
        </div>
        <div id="people-result" class="mt-2"></div>
```

- The releases dialog:

```html
<dialog id="releases-dialog" class="hub-dialog releases-dialog">
  <div class="curation-panel-title"><div><strong>Releases</strong><small>Datasets built from published versions of several projects</small></div>
    <button class="btn btn-sm btn-outline-secondary icon-button" type="button" data-close-dialog="releases-dialog" title="Close"><i class="bi bi-x-lg"></i></button></div>
  <div id="releases-list"></div>
  <div id="release-detail" class="mt-2"></div>
  <details class="mt-3"><summary><b>New release</b></summary>
    <form id="release-form" class="mt-2">
      <div class="d-flex gap-2"><input id="release-name" class="form-control form-control-sm" maxlength="200" placeholder="Name, e.g. Pacific T3 2026-Q4" aria-label="Release name">
        <select id="release-mode" class="form-select form-select-sm w-auto" aria-label="Dataset mode"><option value="T3">T3</option><option value="T1">T1</option></select></div>
      <div id="release-sources" class="mt-2"></div>
      <textarea id="release-notes" class="form-control form-control-sm mt-2" rows="2" maxlength="4000" placeholder="Release notes"></textarea>
      <button id="release-build" class="btn btn-sm btn-primary mt-2" type="submit" data-min-role="lead">Build release</button>
      <span id="release-message" class="small ms-2" role="status"></span>
    </form></details>
  <details class="mt-3"><summary><b>Compare projects</b></summary>
    <div class="d-flex flex-wrap gap-2 mt-2 align-items-center">
      <select id="pc-a" class="form-select form-select-sm w-auto" aria-label="Project A"></select><select id="pc-a-version" class="form-select form-select-sm w-auto" aria-label="A version"></select>
      <span>vs</span>
      <select id="pc-b" class="form-select form-select-sm w-auto" aria-label="Project B"></select><select id="pc-b-version" class="form-select form-select-sm w-auto" aria-label="B version"></select>
      <button id="pc-run" class="btn btn-sm btn-outline-primary" type="button">Compare</button>
    </div>
    <div id="pc-result" class="mt-2"></div></details>
</dialog>
```

- [ ] **Step 4: `collab.js`, compare people.** Append:

```js
// ── Compare people ───────────────────────────────────────────────────────────
async function fillPeople() {
  if (!state.projectId) return
  const [open, sets, versionList, members] = await Promise.all([
    request(`/api/curation/projects/${state.projectId}/change-sets/uncommitted`),
    request(`/api/curation/projects/${state.projectId}/change-sets?limit=200`),
    request(`/api/curation/projects/${state.projectId}/versions`), projectMembers(true)])
  const people = new Map(members.map(m => [String(m.user_id), m.display_name || m.username]))
  open.forEach(u => people.set(String(u.user_id), u.name))
  sets.forEach(cs => cs.author_user_id && people.set(String(cs.author_user_id), cs.author_name))
  const options = [...people].map(([id, name]) => `<option value="${id}">${escapeHtml(name)}</option>`).join('')
  for (const [id, index] of [['people-a', 0], ['people-b', 1]]) {
    const select = byId(id)
    const keep = select.value
    select.innerHTML = options
    select.value = people.has(keep) ? keep : ([...people.keys()][index] || '')
  }
  byId('people-since').innerHTML = '<option value="">All time</option>' + versionList.versions.filter(v => v.status === 'published').map(v => `<option value="${v.version}">Since v${v.version}</option>`).join('')
}

async function runPeopleCompare() {
  const a = byId('people-a').value
  const b = byId('people-b').value
  const box = byId('people-result')
  if (!a || !b || a === b) { box.innerHTML = '<span class="small text-muted">Choose two different people.</span>'; return }
  const since = byId('people-since').value
  try {
    const result = await request(`/api/curation/projects/${state.projectId}/people/compare?a=${a}&b=${b}${since ? `&since=${since}` : ''}`)
    const column = p => `<div class="col"><b>${escapeHtml(p.name)}</b><div class="small">${fmt(p.changes)} changes · ${escapeHtml(describeKinds(p.kinds)) || '—'}</div>
      <div class="small">Classes: ${describeClasses(p.classes) || '—'}</div>
      <div class="small">${p.change_sets.map(cs => `• ${escapeHtml(cs.message)} (${fmt(cs.change_count)})`).join('<br>') || 'No change sets'}</div></div>`
    const d = result.disagreements
    box.innerHTML = `<div class="row g-3">${column(result.people[0])}${column(result.people[1])}</div>
      <p class="small mt-2 mb-1">Both changed <b>${fmt(result.overlap)}</b> items · <b>${fmt(d.count)}</b> disagreements${d.count ? ` <button type="button" class="btn btn-sm btn-outline-primary ms-2" id="people-show">Show these items</button>` : ''}</p>
      ${d.items.length ? `<div class="table-responsive"><table class="table table-sm"><thead><tr><th>Item</th><th>Field</th><th>${escapeHtml(result.people[0].name)}</th><th>${escapeHtml(result.people[1].name)}</th><th>Now</th></tr></thead><tbody>${d.items.slice(0, 100).map(r => `<tr><td>${escapeHtml(r.file_name || String(r.item_id))}</td><td>${escapeHtml(r.field.replace('_', ' '))}</td><td>${escapeHtml(String(r.a_value))}</td><td>${escapeHtml(String(r.b_value))}</td><td>${escapeHtml(String(r.current))}</td></tr>`).join('')}</tbody></table></div>` : ''}`
    byId('people-show')?.addEventListener('click', () => showItems(d.item_ids))
  } catch (error) { box.innerHTML = `<span class="small text-danger">${escapeHtml(error.message)}</span>` }
}

byId('people-run').addEventListener('click', runPeopleCompare)
```

In `curation.js` `loadDatasetStatus`, add `if (typeof fillPeople === 'function') fillPeople().catch(() => {})`. The `people-result` byId lookups live in `collab.js`; the integrity test covers both files.

- [ ] **Step 5: `collab.js`, releases and compare projects.** Append:

```js
// ── Releases ─────────────────────────────────────────────────────────────────
let releaseTimer = null
const releaseBreakdown = stats => ({
  by: 'label', splits: ['train', 'val', 'test', 'unassigned'], rare_min: 0,
  rows: Object.entries(stats.classes || {}).map(([key, counts]) => ({ key, counts: { unassigned: 0, ...counts }, total: Object.values(counts).reduce((a, b) => a + b, 0), rare: false })),
  totals: { unassigned: 0, ...(stats.splits || {}) }, total: Object.values(stats.splits || {}).reduce((a, b) => a + b, 0),
})

async function openReleases() {
  byId('releases-dialog').showModal()
  byId('release-detail').innerHTML = ''
  await Promise.all([loadReleases(), fillReleaseSources(), fillProjectCompare()])
}

async function loadReleases() {
  clearTimeout(releaseTimer)
  const list = await request('/api/curation/releases')
  byId('releases-list').innerHTML = list.length ? `<table class="table table-sm"><thead><tr><th>Release</th><th>Mode</th><th>Sources</th><th class="num">Items</th><th>Status</th><th></th></tr></thead><tbody>${list.map(r => `
    <tr><td><b>${escapeHtml(r.name)}</b> r${r.number}</td><td>${escapeHtml(r.dataset_mode)}</td>
    <td class="small">${r.sources.map(s => `${escapeHtml(s.project_name)} v${s.version}`).join(' + ')}</td><td class="num">${fmt(r.item_count)}</td>
    <td>${escapeHtml(r.status)}${r.error_message ? `<br><small class="text-danger">${escapeHtml(r.error_message)}</small>` : ''}</td>
    <td>${r.status === 'published' ? `<button type="button" class="btn btn-sm btn-outline-secondary" data-release-open="${r.id}">Details</button>` : ''}</td></tr>`).join('')}</tbody></table>`
    : '<p class="text-muted small">No releases yet.</p>'
  document.querySelectorAll('[data-release-open]').forEach(b => b.addEventListener('click', () => showRelease(list.find(r => String(r.id) === b.dataset.releaseOpen))))
  if (list.some(r => r.status === 'building') && byId('releases-dialog').open) releaseTimer = setTimeout(loadReleases, 3000)
}

function showRelease(release) {
  const s = release.statistics
  const base = `/api/curation/releases/${release.id}/files`
  byId('release-detail').innerHTML = `<h3 class="h6">${escapeHtml(release.name)} r${release.number}</h3>
    <p class="small mb-1">${fmt(release.item_count)} items · ${fmt(s.duplicates)} duplicates kept once · ${fmt(s.conflicts)} conflicts left out · ${fmt(s.leakage)} source images in more than one split</p>
    <p class="small">${release.files.map(name => `<a href="${base}/${name}">${escapeHtml(name)}</a>`).join(' · ')}</p><div id="release-breakdown"></div>`
  renderBreakdownTable(byId('release-breakdown'), releaseBreakdown(s))
}

async function fillReleaseSources() {
  const mode = byId('release-mode').value
  const projects = state.projects.filter(p => p.dataset_mode === mode)
  const rows = await Promise.all(projects.map(async p => {
    const versions = (await request(`/api/curation/projects/${p.id}/versions`)).versions.filter(v => v.status === 'published')
    return `<div class="d-flex gap-2 align-items-center mb-1"><span class="small" style="min-width:16rem">${escapeHtml(p.name)}${p.my_role === 'lead' ? '' : ' <small class="text-muted">(not your project)</small>'}</span>
      <select class="form-select form-select-sm w-auto" data-release-source="${p.id}"><option value="">not included</option>${versions.map(v => `<option value="${v.version}">v${v.version} · ${fmt(v.item_count)} items</option>`).join('')}</select></div>`
  }))
  byId('release-sources').innerHTML = rows.join('') || '<p class="small text-muted">No projects in this mode.</p>'
}

byId('releases-button').addEventListener('click', openReleases)
byId('release-mode').addEventListener('change', fillReleaseSources)
byId('release-form').addEventListener('submit', async event => {
  event.preventDefault()
  const sources = [...document.querySelectorAll('[data-release-source]')].filter(s => s.value).map(s => ({ project_id: s.dataset.releaseSource, version: Number(s.value) }))
  const message = byId('release-message')
  try {
    const release = await request('/api/curation/releases', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: byId('release-name').value.trim(), dataset_mode: byId('release-mode').value, sources, notes: byId('release-notes').value.trim() || null }) })
    message.textContent = `Building ${release.name} r${release.number}…`
    message.className = 'small ms-2 text-success'
    loadReleases()
  } catch (error) {
    message.textContent = error.message
    message.className = 'small ms-2 text-danger'
  }
})

// ── Compare projects ─────────────────────────────────────────────────────────
async function fillProjectCompare() {
  const options = state.projects.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('')
  byId('pc-a').innerHTML = options
  byId('pc-b').innerHTML = options
  if (state.projects[1]) byId('pc-b').value = state.projects[1].id
  await Promise.all([fillCompareVersions('pc-a'), fillCompareVersions('pc-b')])
}

async function fillCompareVersions(id) {
  const versions = (await request(`/api/curation/projects/${byId(id).value}/versions`)).versions.filter(v => v.status === 'published')
  byId(`${id}-version`).innerHTML = '<option value="live">Live</option>' + versions.map(v => `<option value="${v.version}">v${v.version}</option>`).join('')
}

async function runProjectCompare() {
  const box = byId('pc-result')
  const query = `a=${byId('pc-a').value}&a_version=${byId('pc-a-version').value}&b=${byId('pc-b').value}&b_version=${byId('pc-b-version').value}`
  box.innerHTML = '<span class="small text-muted">Comparing…</span>'
  try {
    const r = await request(`/api/curation/compare-projects?${query}`)
    const ld = r.label_disagreements
    box.innerHTML = `<p class="small mb-1"><b>${escapeHtml(r.a.project)}</b> ${escapeHtml(r.a.ref)} ${fmt(r.a.count)} · <b>${escapeHtml(r.b.project)}</b> ${escapeHtml(r.b.ref)} ${fmt(r.b.count)} · matched ${fmt(r.matched)} · only A ${fmt(r.only_a)} · only B ${fmt(r.only_b)}</p>
      <p class="small mb-1">${fmt(ld.count)} label disagreements${ld.a_item_ids.length && r.a.ref === 'live' ? ` <button type="button" class="btn btn-sm btn-outline-primary ms-1" id="pc-show">Show in ${escapeHtml(r.a.project)}</button>` : ''}: ${ld.pairs.slice(0, 15).map(p => `${escapeHtml(p.a_label)} → ${escapeHtml(p.b_label)} ${fmt(p.count)}`).join(' · ') || 'none'}</p>
      <p class="small mb-1">${fmt(r.split_disagreements.count)} split disagreements${r.split_disagreements.pairs.length ? ': ' + r.split_disagreements.pairs.map(p => `${escapeHtml(p.a_split)} → ${escapeHtml(p.b_split)} ${fmt(p.count)}`).join(' · ') : ''}</p>
      <div class="table-responsive"><table class="table table-sm"><thead><tr><th>Class</th><th class="num">A</th><th class="num">B</th><th class="num">B − A</th></tr></thead><tbody>${r.classes.slice(0, 200).map(c => `<tr><td>${escapeHtml(c.label)}</td><td class="num">${fmt(c.a)}</td><td class="num">${fmt(c.b)}</td><td class="num">${c.diff > 0 ? '+' : ''}${fmt(c.diff)}</td></tr>`).join('')}</tbody></table></div>`
    byId('pc-show')?.addEventListener('click', async () => {
      byId('releases-dialog').close()
      if (state.projectId !== r.a.project_id) {
        byId('project-select').value = r.a.project_id
        byId('project-select').dispatchEvent(new Event('change'))
        await new Promise(resolve => setTimeout(resolve, 1500))
      }
      showItems(ld.a_item_ids)
    })
  } catch (error) { box.innerHTML = `<span class="small text-danger">${escapeHtml(error.message)}</span>` }
}

byId('pc-a').addEventListener('change', () => fillCompareVersions('pc-a'))
byId('pc-b').addEventListener('change', () => fillCompareVersions('pc-b'))
byId('pc-run').addEventListener('click', runProjectCompare)
```

`renderBreakdownTable`'s default sort expects every row's `counts` to hold every split. `releaseBreakdown` fills `unassigned: 0` for that reason. The "Show in A" project switch waits a fixed 1.5 s; if the project-change handler is async and awaitable, await it instead.

- [ ] **Step 6: Styles.** Append `.releases-dialog { width: min(1100px, 95vw); }` to `static/curation.css`.

- [ ] **Step 7: Run**

Run: TEST `tests/test_curation.py` and the JS syntax check.
Expected: PASS, SYNTAX-OK.

- [ ] **Step 8: Commit**

```bash
git add templates/curation.html static/curation/collab.js static/curation/curation.js static/curation.css tests/test_curation.py
git commit -m "Compare people in Dataset status; Releases panel with build, details and project comparison"
```

---

### Task 11: Docs, full verification, deployment

**Files:** `docs/CURATION.md`.

- [ ] **Step 1: Docs.** In `docs/CURATION.md`, add a "Working together" section covering:
  - project members and roles: who curates, who leads, admins; that non-members are read-only;
  - committing: the pill, the dialog, that edits are live before committing, and that a lead can commit for someone;
  - History and "Show these items";
  - comparing people and comparing projects;
  - combined releases: built from published versions; duplicates kept once; conflicts left out (`conflicts.csv`); leakage listed (`leakage.csv`); the files and where they're written.

  Update the **Roles** bullet to describe project roles. Commit with "CURATION.md: working together — members, change sets, comparisons, releases".
- [ ] **Step 2: Offline suite:** TEST `tests`. Expected: 0 failures.
- [ ] **Step 3: Oracle suite:** `docker compose -f docker-compose.stack.yml --profile test run --build --rm test-oracle`. Expected: 0 failures.
- [ ] **Step 4: Deploy the app only:** `docker compose -f docker-compose.stack.yml -f docker-compose.evaluation.yml --profile evaluation up -d --build --no-deps oceanframe-app`. Then `curl -s localhost:8080/healthz` gives `{"status":"ok","app_db":"oracle"}`. The startup log shows migrations `20261011_01`/`02` applied.
- [ ] **Step 5: Check by hand,** signed in as `tester`:
  1. `tester` is lead on the projects they own (Members dialog).
  2. Create a second test user through `/users` (admin) and add them as curator on the HF test project. Edit as them: the pill counts, Commit with a message, and History shows it. "Show these items" filters the crop grid.
  3. Compare people `tester` and the new user.
  4. Publish NOAA v1 if it has none. Build a release from HF v1 + NOAA v1. Duplicates and conflicts appear; the files download.
  5. Compare the HF project live with NOAA live: matched about 56,864.
  6. As a user who isn't a member, the workspace is read-only with the notice.
