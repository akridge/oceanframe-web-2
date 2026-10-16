# Curation overhaul, part 2 (P1): curation polish, split breakdowns, compare

## Context

Part 1 (Oracle by default + Hugging Face registry) is merged into local `main`. Two later commits on `fix/nonblocking-status` sit on top of it:
- the status freeze fix;
- `CURATION_CREATE_ROLE`.

You asked next for:
- clearer include/exclude in quality review;
- split breakdowns and "other nice stuff";
- comparing and joining datasets across users;
- several people curating at once, with commit-style messages;
- library + curation integration;
- the remaining phases.

That is several subsystems, so it is split again. Order agreed: **P1 → P2 → P3 → P4 → P5**.

| # | Sub-project | Status |
|---|---|---|
| **P1** | Curation polish: include/hold/exclude, bulk relabel in the crop grid, split breakdowns, compare any two versions, one header, view links, background model registration, model cache | **this plan** |
| P2 | Collaboration: change sets with messages ("added crops for rare species"), per-person activity, project members, combining datasets | next |
| P3 | Easier curation: fix/add boxes and patches from cloud images, confusion/embedding mining, FiftyOne bridge | later |
| P4 | Library ↔ curation: one navigation, library assets → curation, shared embeddings | later |
| P5 | Detection and segmentation | last |

### What exploration found

**Quality review hides what it does to the dataset.**
- Cards have only Approve/Reject (`static/curation/curation.js:867-919`).
- `service.review_quality` (`curation/service.py:325-364`) maps:
  - accept → `included=True`;
  - **anything else, including `manual_review`, → `included=False`**.
- "Approve recommended" (`curation.js:945-963`) sends one POST per item, one after another.
- Recommendations come only from the browser's canvas score (`scoreImageQuality`, `curation.js:671-770`).

**No screen shows a live split breakdown.**
- `/stats`, `/status` and `/facets` have no split dimension (`service.py:369-532`).
- Published versions store `statistics.classes` (class × split) and split origins (`curation/versions.py:286-314`), but the UI only prints "train X · val Y · test Z" (`curation.js:619,628`).

**Compare is fixed.**
- `versions.compare(base, target)` (`versions.py:106-152`) diffs `(item_id, label, split)` subqueries but only reports in / out / relabelled, never split moves.
- It is used only for previous-vs-new at publish time and latest-vs-live (`preview`, `versions.py:196-210`).
- `CurationVersionInput` keeps region, island, year and annotator per version item (`models.py:256-277`), so breakdowns of a past version can be computed.

**Headers differ by page.**
- `templates/_header.html` lists Library · Curation · Frame Analyser. It is used by curation, account and users.
- `library.html`, `collections.html`, `collection_form.html` and `index.html` (`/frames`) hand-write their own headers in other orders, with page-specific links (Datasets, My collections, + Add collection). That is the "tabs swap" bug.

**Views have no URLs.**
- `setView` (`curation.js:531-555`) only toggles classes, so a refresh drops you on the default view.

**Model registration blocks its dialog.**
- `registry.register_model` (`curation/registry.py:203-237`) downloads the `.pt` inside the request.

**The model cache doesn't persist.**
- `modelcache.resolve` (`library/modelcache.py:163-176`) returns bare names like `yolo11n.pt` unchanged, so Ultralytics downloads into the container's working directory on every recreate.
- `_writable_cache_dir` (`modelcache.py:28`) falls back to a temp dir when `/models` (a bind mount) isn't writable. That is also lost on recreate.

---

## Design

### 1. Include / Hold / Exclude in quality review

**Server.** `review_quality` mapping:

| Button | Stored decision | Inclusion |
|---|---|---|
| Include | `accept` | `included=True` |
| Exclude | `reject` | `included=False` |
| Hold | `manual_review` | **unchanged** |

- The event already records the old and new `included` (`service._state`).
- Existing items held under the old rule stay excluded. Their cards show "Held · Excluded" and one click fixes them. No data migration.

**New bulk route.** `POST /api/curation/projects/{id}/quality/bulk`, guarded by `require_curator`.
- Body: `{scope: "patch"|"source", decisions: [{item_id, decision, score, flags, expected_revision}]}`, at most 500 per request.
- It applies all decisions under one `operation_id`.
- It returns `{applied, conflicts:[{id, revision, updated_by_name}]}`, the same shape as `bulk_update_items` (`service.py:262-290`).
- Revision mismatches are skipped, not fatal.

**UI** (`templates/curation.html:151-202`, `curation.js` quality section):
- Each card gets three buttons, **✓ Include / ⏸ Hold / ✕ Exclude**, plus a badge: "In dataset" (green) or "Excluded" (grey), and "Held" when held.
- The current decision's button is highlighted.
- Filter labels become "In dataset", "Held", "Excluded" and "Not reviewed".
- The bulk buttons become "Include recommended (N)" and "Exclude recommended (N)". They send one bulk request and report conflicts in a notice.
- Recommendations stay in the browser.

### 1b. Bulk relabel in the crop grid

The server already accepts `current_label` on `POST /items/bulk` (`curation/schemas.py:190-202`). The matrix view already has a Relabel bulk action with a label box and a validity check (`curation.js:1416-1418`). The crop grid's bulk select (`templates/curation.html:334-337`) has only Include / Exclude / review states.

**Crop grid bulk bar:**
- **Relabel to…** with the same label input as the matrix, backed by the project's taxonomy labels list (a datalist). Unknown labels are refused client-side, matching the server's check.
- An optional **Reason** box. It goes into each event's `reason`, so a relabel batch can say "split ACAS vs ACBR per new guide".
- **Select all on this page** / **Clear selection**.
- A count on Apply ("Relabel 37 crops to POCS").
- Items sent with the revision the user saw. Conflicts are reported in a notice, as the matrix does.

Share one `bulkRelabelControls()` helper between grid and matrix, so the two don't drift.

### 2. Split breakdowns

**One server function** `curation/breakdown.py: breakdown(db, project, *, source, by)`:
- `source` is `"live"` (included items) or a version number, read from `CurationVersionInput`.
- `by` is one of `label | region | island | survey_year | annotator`.
- It returns `{by, splits:["train","val","test","unassigned"], rows:[{key, counts:{train,val,test,unassigned}, total}], totals, rare_min}` from a single `GROUP BY key, split`.
- `rare_min` comes from `CURATION_RARE_MIN` (default 10). A row is **rare** when val or test is below it.

**Routes:**
- `GET /projects/{id}/breakdown?by=label` for live;
- `GET /projects/{id}/versions/{n}/breakdown?by=…`.

Both are viewer-readable.

**Dataset status view.** A new "Splits" card holds:
- a group-by selector (Class / Region / Island / Year / Annotator);
- a sortable table with train / val / test / unassigned counts and %;
- a CSS stacked bar per row, the same technique as the existing bars (`curation.js:1479-1482`);
- rare rows flagged, and a "Show rare only" toggle;
- a CSV download built in the browser from the same JSON.

**Versions view.** Each card gets "Breakdown ▸", which expands the same table component for that version. It also shows the stored leakage figure (`cross_split_source_images`) and split origins (`split_origins`) as one line each.

### 3. Compare any two versions

**Generalise `versions.compare`.** For items in both sets, also count **split moves**:
- per class `{moved_to: {train, val, test}}`;
- a from→to split matrix.

Existing callers keep their keys, and the new fields are additive.

**Route:** `GET /projects/{id}/versions/compare?base=<n|live>&target=<n|live>`, viewer-readable.
- `base` and `target` resolve to `CurationVersionItem` subqueries, or `_live` (`versions.py:86-95`).
- It returns 422 when they are equal or a version isn't `published`.

**UI.** A "Compare" bar sits at the top of the Versions view: two selects (versions plus "Live") and a Compare button. Results show:
- totals (in / out / relabelled / split moves);
- a per-class table: before, after, Δ, in, out, relabelled, moved;
- the top relabel pairs;
- the split-move matrix.

Clicking a class filters the crop grid to that class.

### 4. One shared header

`_header.html` takes `active` (`library|curation|frames|account|users`) and an optional `page_actions` block, set with `{% set page_actions %}…{% endset %}` before the include.
- The main links are always **Library · Curation · Frame Analyser**, in that order, with `active` highlighted.
- Page-specific links (Datasets, My collections, + Add collection; the frame analyser's in-page anchors) render in `page_actions` after them.

Pages switched to the partial:
- `library.html`
- `collections.html`
- `collection_form.html`
- `index.html`

Header CSS moves to `static/app.css`, if those pages don't already load it, so they look the same. Also fix the stale `/library` references in `docs/IMAGE_LIBRARY.md`.

### 5. Links to a view

The curation URL hash holds `project`, `view` and, for the matrix view, `run`, for example `/curation#project=8341…&view=versions`.
- `setView`, project switch and run select call `history.replaceState`.
- `init()` reads the hash, falling back to the current default.
- A `hashchange` listener handles back/forward.
- An unknown project or view falls back silently.

### 6. Background model registration

The rule: one coordinator per database, the existing rule (`settings.background_workers()`).

**`POST /hub/models` → 202.**
- Lookup, policy checks and file choice stay synchronous: they are fast and give immediate 4xx.
- It then creates the entry with `status="queued"` and returns it.

**Coordinator loop.** A new `curation/registry_worker.py` starts in the lifespan when background workers are on, next to the import and evaluation pools.
1. Every 5 s it claims one `queued` model entry by conditional update to `downloading`.
2. It downloads, using the HF cache so retries are cheap.
3. It calls `register_stream`, sets `classifier_id`, `status="ready"`, and submits validation (`model_registry.submit_validation`), as the UI did.
4. On error it sets `status="failed"` with `error_message`.

The race cleanup from part 1 (`_discard`) is kept.

**Startup recovery** returns stale `downloading` entries to `queued`.

**A retry route** sends a failed entry back to the queue: `POST /hub/entries/{id}/retry`, behind `require_creator`.

**UI.** The dialog closes on 202 with "Downloading in the background…". The Registry panel and the model list poll every 3 s while any entry is queued or downloading, showing status and error.

The T3 review (workers off) can queue registrations and the main app processes them.

### 7. Model cache persists

`modelcache.resolve(bare_name)`:
- If the name isn't in the cache, it returns `<writable cache dir>/<name>`. Ultralytics downloads known asset names to the given path.
- `_writable_cache_dir`'s fallback becomes `$LIB_DATA_DIR/models` (a persistent volume) before the temp dir.

**Verify in the container:** recreate the app container, then confirm `yolo11n.pt` is not downloaded a second time.

---

## Files

**New:**
- `curation/breakdown.py`
- `curation/registry_worker.py`
- `tests/test_breakdown.py`
- `tests/test_version_compare.py`

**Changed:**
- Services: `curation/service.py` (`review_quality`, `bulk_review_quality`), `curation/versions.py` (`compare` split moves, `resolve_snapshot`), `curation/registry.py` (queue instead of download), `curation/settings.py` (`rare_min`)
- Routers and app: `routers/curation.py` (quality bulk, breakdown, compare), `routers/registry.py` (202, retry), `main.py` (start the registry worker)
- Model cache: `library/modelcache.py`
- Templates and static: `templates/_header.html`, `templates/{library,collections,collection_form,index,curation}.html`, `static/curation/curation.js`, `static/curation/hub.js`, `static/app.css`
- Tests: `tests/test_curation.py`, `tests/test_registry.py`, `tests/test_versions.py`
- Docs: `docs/CURATION.md`, `docs/IMAGE_LIBRARY.md`, `.env.example` (`CURATION_RARE_MIN`)

No new tables. `CurationHubEntry.status` gains `queued` and `downloading` (`Str(20)` already fits).

## Verification

1. **Offline suite:** `docker compose --env-file <dummy> run --build --rm test`. `--build` is needed because code is baked into the image. New tests:
   - **hold** keeps `included`; **include** and **exclude** set it;
   - bulk applies, groups under one `operation_id`, and reports conflicts;
   - breakdown counts for live and for a version, by label and by region; `rare` flag at `CURATION_RARE_MIN`;
   - compare v1→v2 split moves and relabels; live as target; 422 on equal or unpublished;
   - `POST /hub/models` returns 202 with `queued`; the worker turns it into `ready` with the fake hub; failure → `failed`, then retry → `queued`; recovery returns `downloading` → `queued`;
   - `modelcache.resolve("yolo11n.pt")` returns a path in the cache dir;
   - the header renders the same three links in the same order on `/`, `/collections`, `/frames` and `/curation`.
2. **Oracle suite:** `docker compose -f docker-compose.stack.yml --profile test run --build --rm test-oracle`.
3. **Local deployment.** Rebuild only the app container (`up -d --build --no-deps oceanframe-app`), signed in as `tester`:
   - The quality review buttons work, and bulk include sends one request.
   - Relabel 3 selected crops in the grid with a reason; the activity log shows 3 events under one operation, carrying that reason.
   - The Splits card on the NOAA project (941k) is fast enough, under 2 s.
   - Compare v1 vs Live on the HF test project.
   - Header order is the same across pages.
   - A refresh keeps the view.
   - Registering a second HF model returns at once and becomes `ready`.
   - Recreating the container doesn't re-download `yolo11n.pt`.

## After approval

1. Write the spec to `docs/superpowers/specs/2026-10-11-curation-p1-polish-design.md` and commit it on a new branch off `fix/nonblocking-status`, so it includes the freeze fix and create role.
2. Run writing-plans for the implementation plan, then build subagent-driven as before.
3. Next brainstorm: **P2 collaboration**, covering change sets with messages, members, and combining datasets.
