# Curation overhaul, part 4 (P3a): editing patches on the source image, and smoother navigation

## Context

P1 (polish) and P2 (collaboration) are merged into local `main`. You asked next for easier curation:
- fix and add patches from cloud images;
- bulk-add patches around a patch, then slide them into place;
- smoother flow between the curation tabs.

You also reported that source images and patches "don't load anymore".

Old phase D is split, in an order agreed with you:

| # | Sub-project | Status |
|---|---|---|
| **P3a** | **editing patches on the source image, navigation between tabs, missing-image handling, the P2 legacy-version gap** | **this spec** |
| P3b | mining: confusion pairs, likely mislabels, similar-patch search with CLIP | next |
| P3c | FiftyOne bridge | after |
| P4 | library ↔ curation | later |
| P5 | detection and segmentation | last |

### Why images "don't load": diagnosed

The app and its image proxy work. An image present in the bucket loads in about 1 s, and so does its patch: Hawaii `FFS-B009_2019_01.JPG` returned 200, 9.4 MB, and item 16411's patch returned 200.

The NOAA CoralNet mirror is incomplete. Of the source images the NOAA Pacific T3 project references, these are missing from `gs://nmfs_odp_pifsc/…/coralnet_mirror/<region>/images/`:

| Region | Images referenced | Missing | Detail |
|---|---|---|---|
| Hawaii | 32,176 | 1,419 | all from the 2024 survey (e.g. `FFS-4659_2024_A_01.JPG`); their crops are missing too |
| Mariana | 30,598 | 2 | |
| Samoa/PRIA | 31,381 | 1 | |

The source-image queue opens on those missing Hawaii 2024 images first, and a missing image shows as blank. This spec makes missing images visible, filterable and reportable. It does not try to make them load: they aren't there.

### What exists today
- **Geometry.** A patch is a point (`row_px`, `column_px`) plus a square `crop_size`; there are no boxes.
- **Source view.** It can select a point, relabel, include/exclude, set review state, add a point (`POST …/points`) and classify a neighbourhood (`POST …/classify`). It cannot move, resize or remove a point.
- **Patch previews.** `crop-preview` serves the stored `crop_uri`, falling back to `images.crop_from_source`. Manual points' `crop_uri` is a made-up path that is never written, so they are always cut on the fly.
- **No caching.** Every source image and every cut patch is re-read from GCS.
- **Navigation.** Matrix → source, crop tile → source, and change set → crop grid exist. Nothing leads back, quality cards can't open their source image, and the URL doesn't hold the image or point.
- **P2 gap.** Releases and project comparisons read `curation_version_inputs`. NOAA Pacific T3 v1 (published 2026-10-02) has none, so it is silently treated as empty.

### Decisions made with you
- **Edits:** move a point, resize its square patch, remove a point (restorable), and bulk-add patches around a patch. No rectangular boxes; that is P5.
- **Bulk-added patches** start with the source patch's label. When a classifier is registered, each shows the model's top guess, which you can accept per patch. You slide or drop candidates, then save them all at once.
- **Editing style:** edit directly on the source image, with a pending-edits tray and one Save.
- **Crops are cut from the source image and never written back to the buckets.** A disk cache keeps this fast.
- **Patch-only projects** (no source coordinates) stay view-only for geometry.

---

## Design

### 1. Server

**Item edits.**
- `CurationItemUpdate` (`curation/schemas.py:177`) gains optional `row_px`, `column_px` and `crop_size` (32–1024).
- `service.update_item` applies them through `_record_change`: same revision check, audit event and change set.
- `_state()` gains `row_px`, `column_px`, `crop_size` and `removed`, so events record before and after.
- Geometry edits on an item without source coordinates (a patch-only project) return 422.

**Removing a point.**
- Sets `removed = True` (a new boolean column on `curation_items`, added by migration), `included = False` and `review_state = "flagged"`. The event records who and when.
- Restoring clears the flag and sets `included = True`.
- The item filter gains `removed` (`hide` by default, `only`, `any`). The crop grid, source queue, quality review and matrix assets all hide removed items by default. Stats count them as excluded.
- No row is ever deleted. Published versions keep their frozen rows.

**`POST /projects/{id}/source-images/{anchor_item_id}/edits`** (`members.require_project_curator`)
- Body: `{reason?, edits: [{op: "move"|"resize"|"remove"|"restore", item_id, expected_revision, row_px?, column_px?, crop_size?} …], added: [{row_px, column_px, crop_size, label} …]}`.
  - At most 200 edits and 200 added per request.
  - Every `item_id` must be on the anchor's source image.
- **Bounds.** The image's width and height come from the image cache (§3). A point's centre must lie inside the image; a patch may extend past the edge, as crops already do.
- **One transaction, one `operation_id`.** Stale revisions are skipped and returned as `conflicts` (the P1 bulk shape). Added points use `service.create_manual_point`'s rules: label validated against the project taxonomy, same split as the image, `manual_point` metadata.
- **Response:** `{operation_id, applied, added: [items], conflicts}`.
- **Event types** stay committable (P2): moves, resizes, removes and restores write `item_updated`; added points write `manual_point_created`.
- **Change-set summaries** gain the kinds `move`, `resize`, `remove` and `restore`, from the before/after geometry and the removed flag.

**`POST /projects/{id}/source-images/{anchor_item_id}/suggest`** (`members.require_project_curator`)
- Body: `{item_id, grid: 3|5, spacing_px: 32–512, crop_size?: 32–1024, model_name?}`.
- Returns candidate centres in a grid around the item: 8 for 3×3, 24 for 5×5. Candidates whose centre falls outside the image are clipped away.
- When `model_name` is given and the classifier is available, each candidate gets the model's top 3, using `scoring.classify_neighborhood`'s model loading on the cached source image.
- Without a model, candidates carry no hints.
- Response: `{candidates: [{row_px, column_px, crop_size, hints?: [{label, confidence}]}], model_available}`.

**Crop serving.** `crop-preview` serves the stored `crop_uri` only when the item's geometry has never been edited and it isn't a manual point (`metadata_json["geometry_edited"]` absent). Otherwise it cuts from the source image via the cache. "Write image folders" (`versions._export_images`) applies the same rule, so edited and new patches export correctly.

**Image dimensions.** `GET /projects/{id}/source-images/{anchor}` gains `width` and `height` from the cache, so the editor can clamp positions and the server can check bounds.

### 2. Missing source images

**`curation/availability.py`, a background operation on the import pipeline** (`CurationImportOperation`, `kind = "availability_check"`). The coordinator runs it.
1. Lists each distinct `<root>/images/` prefix the project's items use, with `storage` listing: one paged listing per prefix. 93k images take about a minute.
2. Compares each item's source image name against the listing.
3. Sets or clears `source_missing`, a second new boolean column added by the same migration, in batches. Columns rather than JSON flags, because every list view filters on them over about 1M rows, and JSON text can't be filtered quickly on Oracle.

It runs automatically after each import completes, and on demand: `POST /projects/{id}/availability` (project curator). For local-path sources, a file-exists check stands in for the listing.

**UI.**
- **Source queue:** missing images sort last and a "Hide missing images" filter is on by default. The filter is `source_missing`: `hide` | `only` | `any`.
- **When an image or patch fails to load:** the viewer and tiles show "Source image not in the bucket", plus the URI, instead of a blank.
- **Dataset status:** a "Missing source images" card counts missing images by region and survey year, e.g. "Hawaii 2024: 1,419 missing", with a CSV download and a "Check again" button.

### 3. Image cache

**`curation/image_cache.py`.** Every source read in `images`, `scoring`, `versions._export_images` and the proxy goes through it.
- **Location:** `CURATION_IMAGE_CACHE` (default `$LIB_DATA_DIR/curation_cache`), holding `sources/<sha1(uri)>` and `patches/<sha1(uri)>_<row>_<col>_<size>.png`.
- **Cap:** `CURATION_IMAGE_CACHE_MB` (default 2048) total. Least recently used files go first, by access time, when a write exceeds the cap.
- **Single download:** a per-key lock, so concurrent requests for the same image make one download.
- **Dimensions:** `dimensions(uri)` reads them with PIL from the cached file without decoding pixels, memoised.
- **Failures:** a missing object raises `FileNotFoundError` as `storage` does today. Nothing is cached for a miss.
- **Safety:** files are written under a temporary name and renamed into place, so a crash never leaves a half-written file.

### 4. The editor on the source image

In `static/curation/editor.js`, a new classic script loaded after `collab.js`:
- **Select a point:** its marker gets a move handle (drag the marker) and a corner resize handle. The point panel gains Remove/Restore, and size presets of 160, 224 and 320, plus a number field.
- **Bulk add around…** in the point panel opens a small form: grid 3×3 or 5×5, spacing (defaulting to the patch size), and a model if one is registered. It calls `/suggest` and draws the candidates as dashed, draggable squares. Each candidate has a label chip (the source patch's label), a model hint chip when available (click to accept it), and a drop ×.
- **Pending-edits tray** below the image:
  - one row per move, resize, remove or new patch;
  - each row has a live preview cut in the browser with canvas from the already-loaded image, plus the old → new values;
  - a Reason box;
  - "Save N edits" and "Discard".
- **Saving** sends one `/edits` request, reports conflicts the P1 way, then reloads the image's points and refreshes the commit pill.
- **Unsaved edits:** switching image, view or project with edits pending asks "You have N unsaved edits — discard them?".
- **Keyboard:** ← and → step to the previous or next image in the queue. Delete marks the selected point for removal. Arrow keys nudge the selected point 1 px, or 10 px with Shift. Escape cancels a drag.
- **Who can edit:** editing is enabled only with `canDo('project-curator')`. Viewers and non-members get the same read-only view as today.

### 5. Navigation

- **Back:** arriving at the source view from a matrix cell, an evaluation card, a crop tile, a quality card, a change set or a compare result records where you came from. "← Back to <matrix | crop grid | quality review | history | compare>" restores that view, its filters, its selection and its scroll position.
- **Open source image:** added to crop tiles (on hover), quality cards and evaluation cards, next to the existing "Full image".
- **The URL hash** (`readHash`/`writeHash`, P1) gains `image` (the first item id of the source image) and `item` (the selected point). A refresh or shared link opens the same image with the point selected. If the image is missing, the "not in the bucket" notice shows.

### 6. The P2 legacy-version gap

`releases.start` and `project_compare._side` (version side) check that the version has `curation_version_inputs` rows (`EXISTS`). If it has none but `item_count > 0`, they return 422: "v{n} of {project} was published before releases were supported — publish a new version first." The release form and compare form show that message.

---

## Files

**New:**
- `curation/image_cache.py`, `curation/availability.py`, `curation/editing.py` (batch edits and suggest)
- `static/curation/editor.js`
- `tests/test_image_cache.py`, `tests/test_editing.py`, `tests/test_availability.py`

**Changed:**
- **Server:**
  - `curation/schemas.py` (update fields, edit batch, suggest)
  - `curation/service.py` (`_state`, `update_item`, the removed filter, `item_query`)
  - `curation/images.py` (crop rule, cache)
  - `curation/scoring.py` (cached source)
  - `curation/versions.py` (export crop rule)
  - `curation/change_sets.py` (new kinds)
  - `curation/importer.py` (run the availability check after import)
  - `curation/releases.py`, `curation/project_compare.py` (legacy 422)
  - `routers/curation.py` (edits, suggest, availability, width/height, filters)
- **UI:**
  - `templates/curation.html` (tray, bulk form, missing-image notice, back bar, filters, status card)
  - `static/curation/curation.js` (hash, back stack, open-source buttons, missing filter, hooks)
  - `static/curation.css`
- **Config:** `docker-compose.stack.yml` (`test-oracle` list), `.env.example` (cache settings)
- **Docs:** `docs/CURATION.md`

## Verification
1. **Offline suite** (`--build`). New tests cover:
   - **Batch edits:** move, resize, remove and restore; revision conflicts; bounds checked against image size; the 200-item limits; items not on the anchor image refused; a geometry edit on a patch-only item refused.
   - **Added points:** taxonomy-validated, with the image's split.
   - **Previews:** a moved item's crop is re-cut, never the stale `crop_uri`, and the export uses the same rule.
   - **Removed items:** hidden by default and counted as excluded.
   - **Change-set summary:** the new kinds appear.
   - **`/suggest`:** grid sizes, clipping at image edges, hints with the stub classifier, none without.
   - **Image cache:** a hit avoids a read; the cap evicts least recently used files; concurrent first reads make one download; no partial files; dimensions are read without decoding.
   - **Availability check:** a fake listing marks missing items; running again clears fixed ones; the sort and filter work; the status counts group by region and year.
   - **Legacy versions:** 422 for releases and compare.
2. **Oracle suite**, with the new test files in `test-oracle`.
3. **Local deployment:** rebuild the app only.
   - Run the availability check on NOAA Pacific T3: expect about 1,422 images flagged (1,419 Hawaii 2024).
   - The queue opens on an image that loads.
   - Move, resize and remove a point; bulk-add 3×3 around a patch; save; check the history entry and the commit summary.
   - Back from the crop grid; shared image link; keyboard navigation.
   - Browser check with the Playwright image.
