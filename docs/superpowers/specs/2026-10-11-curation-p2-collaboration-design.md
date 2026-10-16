# Curation overhaul, part 3 (P2): collaboration

## Context

P1 is merged into local `main`. It covered include/hold/exclude, bulk relabel, split breakdowns, compare any two versions, one header, view links, background model registration and the model cache.

You asked next for:
- several people curating **the same dataset at the same time**, with **commit-style messages** that say what each person did ("added crops for rare species");
- a **high-level compare or join of datasets**, so people can collaborate on one dataset.

Order agreed earlier:

| # | Sub-project | Status |
|---|---|---|
| P1 | polish, splits, compare, header, background registration | done |
| **P2** | **collaboration: change sets, project members, comparing people and projects, combined releases** | **this spec** |
| P3 | easier curation: fix/add boxes and patches from cloud images, confusion and embedding mining, FiftyOne | next |
| P4 | library ↔ curation | later |
| P5 | detection and segmentation | last |

### Decisions made with you
- **Join** means three things:
  - a **combined release** built from several projects;
  - **comparing two projects**;
  - **comparing two people's work**.

  It does **not** mean moving items between projects.
- **Change sets: commit afterwards.** Edits apply to the shared draft immediately, as today. Each person's edits collect as "your uncommitted changes"; Commit groups them under a message.
- **Membership: per-project roles.** Members are `curator` or `lead`. Only members edit a project; everyone signed in can view it; app-wide admins can do anything.
- **Combined releases are built from published versions**, one per project, in one dataset mode.

### What exists today
- Every edit writes a `curation_events` row (`curation/models.py:135-160`): user, event type, optional `reason`, previous/new value. Bulk actions share an `operation_id`.
- Per-item `revision` makes simultaneous edits safe: single-item edits get 409, bulk edits report conflicts (`service._check_revision`, `bulk_update_items`).
- Roles are app-wide: `viewer < curator < lead < admin` (`auth/deps.py`). Creation follows `CURATION_CREATE_ROLE`. `CurationProject.owner_user_id` is used only for delete.
- Versions freeze `curation_version_inputs` rows and record contributors; `versions.compare` diffs any two `(item_id, label, split)` sets.
- The live database has 3 events, 3 projects, 4 users and about 1M items, so the migrations are cheap.
- Matching crops across sources by (crop `file_name`, `source_label`) is proven by `service.match_items`: 378,680 of 378,680 HF rows matched.

---

## Design

### 1. Data model

New tables, created by `create_all`. Use `Str(n)`/`JSONText`, and keep identifiers at 30 characters or fewer.

```
curation_change_sets  (CurationChangeSet)
  id                    Integer Identity PK
  project_id            FK curation_projects.id ON DELETE CASCADE, indexed
  author_user_id        FK of_users.id ON DELETE SET NULL
  author_name           Str(NAME_LEN)
  committed_by_user_id  FK of_users.id ON DELETE SET NULL   -- differs from author when a lead commits for someone
  committed_by_name     Str(NAME_LEN)
  message               Str(500) NOT NULL
  change_count          Integer NOT NULL
  summary               JSONText   -- {kinds: {relabel, inclusion, quality, point, review}, classes: [{label, items}] top 10, items: n}
  created_at            DateTime

curation_project_members  (CurationProjectMember)
  project_id     FK curation_projects.id ON DELETE CASCADE   } PK (project_id, user_id)
  user_id        FK of_users.id ON DELETE CASCADE            }
  role           Str(10)  'curator' | 'lead'
  added_by_name  Str(NAME_LEN)
  added_at       DateTime

curation_releases  (CurationRelease)
  id              Integer Identity PK
  name            Str(200) NOT NULL
  number          Integer NOT NULL      -- r1, r2 … per name
  dataset_mode    Str(10)               -- T1 | T3
  status          Str(20)  building | published | failed
  notes           Text
  sources         JSONText  -- [{project_id, project_name, version, version_id}]
  statistics      JSONText  -- totals, classes × splits, per_source, duplicates, conflicts, leakage
  item_count      Integer
  output_uri      Str(URI_LEN)
  error_message   Text
  created_by_user_id / created_by_name, created_at, published_at
  UNIQUE (name, number)
```

**A new column through the `MIGRATIONS` ledger** (`appdb/bootstrap.py`): `curation_events.change_set_id`. It is an Integer, NULL, with an FK to `curation_change_sets.id` ON DELETE SET NULL, plus an index on (`project_id`, `user_id`, `change_set_id`). On SQLite, a fresh `create_all` already includes it.

**A second migration seeds memberships:**
- each project's owner becomes `lead`;
- every distinct user with item events in the project becomes `curator`, unless already a member;
- a project with no owner gets no lead (admins can add one).

It is idempotent: an existing row is never changed.

**Committable events.** These event types belong to a change set:
- `item_created`
- `item_updated`
- `bulk_item_updated`
- `manual_point_created`
- `quality_reviewed`
- the evaluation-correction events written by `/evaluations/{run}/…/item`

Imports (`source_import_completed`, hub import and link events) and system events are never committable.

Nothing else changes: items, versions and existing event columns stay as they are.

### 2. Permissions

**New module `curation/members.py`:**
- `project_role(db, project_id, user) -> 'lead' | 'curator' | None`
  - An app-wide admin counts as `lead` on every project.
- `require_project_role(minimum)` — a FastAPI dependency factory reading `project_id` from the path.
  - It raises **403** with "You are not a member of this project — ask a project lead (…names…) to add you", or with "This needs the project lead role".
- `ensure_creator_is_lead(db, project, user)` — called when a project is created:
  - in `service.create_project`;
  - in the HF `create_project_from_dataset`.

| Action (routes) | Who |
|---|---|
| View anything: GETs, compare, history, releases list | any signed-in user (unchanged) |
| Item edits:<br>`POST /projects/{id}/items`<br>`PATCH …/items/{item_id}`<br>`…/items/bulk`<br>`…/items/{id}/quality`<br>`…/quality/bulk`<br>`…/source-images/{a}/quality`<br>`…/points`<br>`…/classify`<br>the two evaluation `…/item` PATCHes | project **curator or lead** (replaces `require_curator`) |
| Commit own changes | project curator or lead |
| Commit another member's changes | project lead |
| `POST /projects/{id}/imports` and its cancel<br>`POST /projects/{id}/evaluations` and its cancel<br>HF resume import | project curator or lead (replaces `require_creator`) |
| `POST /projects/{id}/versions`<br>`…/versions/{n}/images`<br>HF link apply (`dry_run=false`) | project **lead** (replaces `require_lead`) |
| Members: list (any signed-in user); add, change role, remove (project lead) | — |
| Delete project | owner or admin (unchanged) |
| Create project, the registry, evaluation models | app-wide `CURATION_CREATE_ROLE` (unchanged); the creator becomes project lead |
| Build a combined release | app-wide lead **and** project lead on every source project |

**Guardrails:**
- A project must keep at least one lead. Removing or demoting the last lead is refused with 409, unless an admin does it.
- A lead may change their own role only if another lead remains.

**The page:**
- `GET /api/curation/projects` and the project response gain `my_role` (`lead` | `curator` | `null`).
- `canDo` gains the project-scoped checks `canDo('project-curator')` and `canDo('project-lead')`. Template `data-min-role` attributes on project-scoped controls switch to these.
- A non-member sees a read-only workspace with a notice naming the project's leads.

**Members panel.** A "Members" button in the curation header opens a dialog:
- list: name, role, added by and when;
- leads get add (search existing users by username or display name), change role, remove.
- Endpoints:
  - `GET /projects/{id}/members`
  - `POST /projects/{id}/members {username, role}`
  - `PATCH /projects/{id}/members/{user_id} {role}`
  - `DELETE /projects/{id}/members/{user_id}`
- Adding a user who doesn't exist is a 404. Adding an existing member is a 409.

### 3. Change sets

**Uncommitted changes:** the committable events in this project with this author's `user_id` and `change_set_id IS NULL`.

- **`GET /projects/{id}/change-sets/pending?user=<id|me>`** — `{count, summary, recent: [20 latest events]}`. `user` other than `me` is project-lead only.
- **`POST /projects/{id}/change-sets {message, user?}`**
  1. Computes the summary over the author's uncommitted events.
  2. Inserts the change set.
  3. Runs `UPDATE curation_events SET change_set_id=:new WHERE project_id=:p AND user_id=:author AND change_set_id IS NULL AND event_type IN (…) AND id <= :max_seen`. `max_seen` is the highest event id read for the summary, so an edit landing mid-commit stays uncommitted for next time.
  4. Sets `change_count` to the UPDATE rowcount. If it is 0, deletes the row and returns 409 "Nothing left to commit". This handles the double-commit race.
  - `user` (committing for someone else) requires project lead. It records `committed_by`.
  - The message is required: 1-500 characters after trimming.
- **`GET /projects/{id}/change-sets?user=&since_version=&limit=&before=`** — the history, newest first, paged.
- **`GET /projects/{id}/change-sets/{cs_id}`** — the change set plus its events (before → after per item, paged) and `item_ids` for "Show these items".
- **`GET /projects/{id}/change-sets/uncommitted`** — `[{user_id, name, count}]`, for "in progress".

**Summary kinds,** computed from each event's previous/new values:
- `relabel`: `current_label` changed;
- `inclusion`: `included` changed;
- `quality`: `quality_reviewed`;
- `point`: `manual_point_created`;
- `review`: `review_state` changed;
- `added`: `item_created`.

`classes` are the top 10 current labels of the touched items, with distinct item counts.

**UI:**
- **Header pill:** "37 uncommitted changes · Commit…". It is hidden at 0, refreshed with `loadStats`, and shown only to members.
- **Commit dialog:** a message (required, counter up to 500), the summary, the 20 most recent changes, and Commit.
- **Versions view,** a new **History** section at the top:
  - change sets, newest first: author, message, counts by kind, top classes, time, and "committed by X for Y";
  - an in-progress line: "Uncommitted: Ana 37 · Ben 4";
  - filters for person and "since vN".
  - Clicking a change set expands its changes. **Show these items** opens the crop grid with the existing `item_ids` filter.
  - Leads see "Commit for …" beside each person with uncommitted work.

**In version change logs:**
- `versions._build` collects the change sets whose events fall in the version's event window (previous watermark up to this watermark), plus the uncommitted counts per person in that window.
- `manifest.json` gains `change_sets: [{id, author, message, change_count}]` and `uncommitted: [{name, count}]`.
- `CHANGELOG.md` gains a "Change sets" section and an "Uncommitted" line.
- The publish preview (`versions.preview`) gains the same two lists, so the lead sees them before publishing.

### 4. Comparing two people

**`GET /projects/{id}/people/compare?a=<user_id>&b=<user_id>&since=<version number>`** — `since` is optional; without it the comparison covers all time.
- `people: [{user_id, name, change_sets: [...], changes, kinds, classes}]` for A and B.
- `overlap`: the number of distinct items both changed.
- `disagreements`: items both changed where their **latest** values differ, on label or on inclusion. Each item has `{item_id, file_name, field, a_value, a_at, b_value, b_at, current}`, up to 500 items, plus a total `count` and `item_ids`.
- Viewers can read it. 422 when a == b; 404 for an unknown user.

**UI.** A "Compare people" form in Dataset status, beside "Who is curating". It has two person selects (from the curators list) and "since", and shows:
- two summary columns;
- an overlap number;
- a disagreements table with "Show these items" for the crop grid.

### 5. Combined releases

**`curation/releases.py`**, with routes under `/api/curation/releases` in `routers/releases.py`:
- **`GET /releases`** — list.
- **`GET /releases/{id}`** — detail.
- **`GET /releases/{id}/files/{name}`** — downloads for `manifest.csv`, `manifest.json`, `CHANGELOG.md`, `conflicts.csv`, `leakage.csv`.
- **`POST /releases {name, dataset_mode, sources: [{project_id, version}], notes}`** → 202, status `building`.
  - **Validation** (422):
    - at least 1 source;
    - each project at most once;
    - every version is `published`;
    - every project matches `dataset_mode`.
  - **Permission** (403): app-wide lead **and** project lead on every source.
  - **Number:** `number = max(number for name) + 1`, retried on IntegrityError, as `start_publish` does.
- **Building** runs in a daemon thread in the process that received the request, exactly like `versions.start_publish`. It only reads frozen, published rows, so it doesn't need the coordinator.
  1. Stream each source's `CurationVersionInput` rows, ordered by item id.
  2. Key each row by (`file_name`, `source_label`).
  3. **First occurrence:** keep it.
  4. **Same key, same label:** count it under `duplicates`; keep the first by source order.
  5. **Same key, different label:** a conflict. Drop **all** rows of that key from the release, and write one `conflicts.csv` row per source (key, project, version, label, split).
  6. **Leakage:** the same `source_image_uri` across different splits in the final set. Count it and write `leakage.csv` (source image, project/split pairs). Splits are not changed.
  7. `manifest.csv` has the version manifest's columns plus `project` and `project_version`.
  8. `statistics`:
     - totals;
     - `classes` × splits;
     - `per_source` item counts;
     - duplicate, conflict and leakage counts.
  9. **CHANGELOG.md** covers:
     - name and number, notes, sources (project, version, item count, that version's notes);
     - compared with the previous release of the same name: which sources changed version, were added or were removed;
     - totals, duplicates, conflicts, leakage.
  10. Files go under `settings.versions_dir() / "_releases" / <slug name> / r<n>/`. They upload to `settings.publish_uri()` when it is set, the same way version files do.
  11. On failure, set status `failed` with `error_message`. Partial files are removed.
- **Startup recovery:** releases left `building` by a restart become `failed` ("interrupted — build it again"), the same handling versions already get.

**UI.** A **Releases** button in the curation header opens a panel:
- **List:** name, rN, mode, sources, item count, status, and download links.
- **Detail:** totals, duplicates, conflicts, leakage, and the class × split table, using P1's `renderBreakdownTable` from statistics shaped as a breakdown response.
- **New release** form: name, mode, a per-project "not included / vN" select (published versions of projects in that mode), notes, and Build. It polls while building.

### 6. Comparing two projects

**`GET /api/curation/compare-projects?a=<project_id>&a_version=<n|live>&b=<project_id>&b_version=<n|live>`**
- Each side is a set of rows `(item_id, file_name, source_label, label, split)`:
  - **live:** included items;
  - **version:** `CurationVersionInput`.
- Matched with one SQL join on (`file_name`, `source_label`).
- Returns:
  - `a_count`, `b_count`, `matched`, `only_a`, `only_b`;
  - `classes: [{label, a, b, diff}]`;
  - `label_disagreements: {count, pairs: [{a_label, b_label, count}] top 50, a_item_ids (≤ 5000)}`;
  - `split_disagreements: {count, pairs: [{a_split, b_split, count}]}`.
- Viewers can read it.
- 422 when it's the same project and version, when the dataset modes differ, or when a version is unpublished. 404 for a missing project or version.

**UI.** A **Compare projects** button in the Releases panel opens a form with A and B (project plus live or version). The result shows:
- the counts;
- a per-class table;
- the top disagreement pairs, with **Show in A**, which switches to project A and filters the crop grid by `item_ids`.

---

## Files

**New:**
- `curation/members.py`, `curation/change_sets.py`, `curation/people.py`, `curation/releases.py`, `curation/project_compare.py`
- `routers/releases.py` (releases and project compare)
- `static/curation/collab.js`, a classic script after `hub.js`: members, commit, history, people compare, releases, project compare
- `tests/test_members.py`, `tests/test_change_sets.py`, `tests/test_people_compare.py`, `tests/test_releases.py`, `tests/test_project_compare.py`

**Changed:**
- Schema and setup: `curation/models.py` (three tables, `change_set_id`), `appdb/bootstrap.py` (two migrations)
- Routers: `routers/curation.py` (guards to project roles; members, change-set and people routes), `routers/registry.py` (HF guards to project roles where project-scoped)
- Services: `curation/service.py` (`create_project` makes the creator lead), `curation/registry.py` (same for HF project creation), `curation/versions.py` (change sets in preview, manifest and changelog)
- Startup: `main.py` (router; release recovery at startup)
- Page: `templates/curation.html` (header buttons, dialogs, History section, compare-people form), `static/curation/curation.js` (`my_role`, `canDo`, the pill, the History hook)
- Test setup: `docker-compose.stack.yml` (the new test files in `test-oracle`)
- Docs: `docs/CURATION.md`

## Verification
1. **Offline suite** (`--build`). New tests cover:
   - **Membership:** seeding from history; the creator becomes lead; a non-member gets 403 on edits; a global viewer who is a project curator can edit; a global lead who is not a member cannot publish; last-lead protection; admin bypass.
   - **Commit:** groups only committable events; 409 on an empty or double commit; a mid-commit edit stays uncommitted; a lead commits for a member, and a curator cannot.
   - **History and change logs:** history paging and filters; change sets and uncommitted counts in the preview, manifest.json and CHANGELOG.md.
   - **People compare:** per-person counts, overlap, and label and inclusion disagreements using latest values.
   - **Releases:** duplicates counted; conflicts dropped and listed; leakage listed; permission and validation errors; numbering; recovery marks `building` as `failed`; the change log names the sources that changed since the previous release.
   - **Project compare:** live and version sides, the counts, disagreement pairs, and a mode mismatch giving 422.
2. **Oracle suite,** with the new files added to `test-oracle`. This includes the two migrations on a database that already has projects and events.
3. **Local deployment:** rebuild the app only.
   - The migration seeds members: `tester` is lead on the projects they own.
   - Add a second test user as curator and make edits as them; the pill shows; commit with a message; History shows it; "Show these items" works.
   - Compare people.
   - Publish a version: the change log lists the change set.
   - Build a combined release from the HF project's v1 and a published version of the NOAA project; publish NOAA v1 first if it has none. The 56,864 test-split crops exist in both, so expect them as duplicates, or as conflicts where the two projects' labels differ.
   - Compare the two projects.
   - A non-member gets a read-only workspace.
