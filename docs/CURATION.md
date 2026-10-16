# Classification curation

`/curation` is where a team cleans a point-annotation classification dataset:
find where the labels and the classifier disagree, look at those points on the
full survey image, and decide — relabel, exclude, flag, or accept. It is OASIS's
classification curation, ported into OceanFrame and made safe for several
people working at once.

## Using the app

### The shell

Every page sits in the same shell (`templates/base.html`).

- **Rail** (left edge; a tab bar along the bottom of the screen at 900 px and
  below): Library, Collections, Curation and Frame Analyser, with the current
  page highlighted. **Users** appears for admins only. The avatar at the foot of
  the rail opens the user menu.
- **Top bar**: the page title, then whatever the page puts there (on
  `/curation` that is the project picker, the view tabs and the **Project**
  menu). **Search ⌘K** is at the right.
- **Command palette** (**Ctrl+K**, or **⌘K** on a Mac, or the Search button): type
  to filter, Up and Down to move, Enter to run, Esc to close. It lists the pages,
  the theme actions and, on `/curation`, every project × view, images by name
  (three or more characters, searched in the active project), and curation
  actions such as "Check for missing images" and "Toggle Hide missing images".
  Jumps made from the palette go through the unsaved-edits guard.
- **Theme** is in the avatar menu: Dark, Light or System. The choice is stored in
  the browser and applied before the page paints, so a reload does not flash.
  The palette has the same three actions. Motion is switched off for people who
  ask their system to reduce it.
- Messages appear as toasts at the bottom of the screen; confirmations and forms
  are sheets (modal panels, closed with Esc or their close button). There are no
  browser pop-up dialogs, except the browser's own "leave this page?" prompt when
  unsaved edits exist.

### The curation layout

- **Filters** (the button at the left of the filter bar) opens a popover with
  search, taxonomy and class-code filters, review state, inclusion, label origin,
  survey facets (region, island, year, site, annotator), **Hide missing images**
  and **Show removed**. **Apply** loads the queue; **Clear** resets. Each active
  filter then shows as a **chip** next to the button (remove one with its x), with
  a running count. The Export format and **Export filtered** sit at the right of
  the same bar.
- **Project menu** (top bar, the **Project** button, called More in the code):
  Import regions, New project, Registry, **Members**, **Releases** and
  Delete project. Members and Releases open as sheets.
- **Queue** (left column): the source images of the current filter, each with its
  thumbnail, point count, labels and reviewed progress. **Load more** appends the
  next page. The chevron at the head collapses the column to give the stage the
  room; the choice is remembered, and screens of 900 px or less start collapsed.
- **Stage** (centre): the image with its points. The **stage toolbar** floats at
  its top left: overlay mode (Patch squares or Points), **Add point**, **Bulk add
  around** (when a point is selected), zoom out and in with the level shown, fit,
  and full screen. Button labels collapse to icons when the stage is narrow. The
  **stepper** at the top right shows the position ("3 / 30+") with previous and
  next buttons; they walk the queue and load more as needed. Neither the toolbar,
  the stepper nor the pill below pan, zoom or add a point when clicked.
- **Unsaved-edits pill** (bottom centre of the stage, only while edits are
  pending): "N unsaved edits" with a breakdown (moves, resizes, removals, adds),
  and **Review**, **Discard** and **Save**. Review opens a list above the pill
  of every pending edit on this image, each with its preview, and the optional
  reason for the batch. Save sends everything in one request. See
  [Pending edits](#pending-edits-and-save-n-edits).
- **Inspector** (right column): three tabs, **Point** (the crop, label, patch
  size, remove or restore, review state, include in the next version), **Image**
  (facts about the source image, its metadata and a table of all its points) and
  **Model** (classify the selected point with a model you choose, and see its
  suggestions). The tab you
  last clicked is remembered; selecting a point switches to Point. On narrow
  screens the inspector drops below the stage.
- **Back**: opening a point's source image from the Crop grid, quality review or
  the matrix puts a **Back** bar above the workspace that returns to that view
  (its label names the view).

The other views (Crop grid, Quality review, Dataset status, Confusion matrix,
Versions) use the same shell, filter bar and sheets. In the Crop grid, selecting
tiles shows a selection bar at the foot of the page with the bulk actions; on
touch screens each tile has an always-visible Open source image button.

### Keyboard shortcuts

| key | does |
|---|---|
| Ctrl/⌘ + K | open the command palette (Esc closes it) |
| Up / Down, Enter | in the palette and in menus: move, choose |
| Esc | close a sheet, popover or menu; cancel a marker drag |
| Arrow keys | Source view: nudge the selected point 1 px |
| Shift + arrow | nudge 10 px |
| Delete or Backspace | remove the selected point; restores it if already removed |
| Alt + Left / Right | previous / next image in the queue |
| Left / Right | previous / next image, when the selected point is not editable |
| A / F / X | Source view: accept (reviewed, included), flag, or exclude the selected point, save, and open the next one |
| R | Source view: put the cursor in the label, to relabel |
| Shift + Enter | in the point form: save and open the next point (the Save & next button) |
| Arrow keys, Space | Crop grid: move between tiles, toggle selection |
| Ctrl/⌘ + click, Alt + click | Crop grid lasso: toggle, or deselect, instead of select |

The editor keys act only on the Source images view, and not while typing in a
field or with a sheet open. Details are in [Keyboard](#keyboard) below.

## The workflow

1. **A lead creates a project** at tier **T3** (fine-grained classes, e.g. `ACBR`,
   `TURFH`) or **T1** (broad groups, e.g. `CORAL`). Every raw annotation code is
   projected to its single ancestor at that tier using the taxonomy tree; codes
   with no T1 home become `UC`.
2. **The lead imports annotations** — by default the NOAA PIFSC CoralNet regional
   CSVs (Hawaii, Mariana, Samoa/PRIA), read straight from the public bucket. Each
   row becomes an *item*: a point on a source image with its original label, a
   current label, and the classifier's top-5 suggestions. Re-importing is safe;
   rows already present are skipped.
3. **Curators start with model evaluation**, then work through the dataset:
   - **Confusion matrix** opens on a completed model evaluation of a published
     version, initially its Test split when available. Training, Validation and
     All evaluated splits use their own saved predictions. Cells drill into
     saved crops; row normalization shows recall, column normalization precision.
     Lowest recall / Most errors ordering prioritizes weak classes.
     **All project items / CSV suggestions** remains a separate, optional view;
     it is not a reproducible model evaluation.
   - **Source images** — the full image with every point as a patch square; the
     224×224 crop beside it; the inspector to relabel, include/exclude and set a
     review state, with the reason recorded. Points can be added by hand, and a
     classification model can score a point (or a grid around it) to help label it.
   - **Crop grid** — tiles of crops with lasso and keyboard selection for bulk
     include/exclude/review and **Relabel** (with an optional reason). **Select
     page** selects every tile on the page. The curation URL keeps the project,
     view and evaluation run, so a refresh or a shared link lands in the same place.
   - **Quality review** — sharpness, exposure, contrast and colour cast measured
     in the browser, per patch first and then per source image. Each item gets
     Include, Hold or Exclude; Hold leaves inclusion as it was. **Include
     recommended (N)** and **Exclude recommended (N)** send one bulk request.
   - **Dataset status** — counts per label and region, **who is curating** (changes
     and items per person), recent changes with before → after and the reason, and
     the full audit trail as CSV. The **Splits** card shows included items across train / val / test /
     unassigned, grouped by a selector (class / region / island / year / annotator). Groups with fewer than `CURATION_RARE_MIN` items in val or test
     are flagged; **Rare only** filters to them, and the breakdown downloads as CSV.
     Each published version has its own **Breakdown** button.
4. **Publish a version** when a round of curation is done: a frozen, numbered
   dataset with train/val/test splits and a change log of what came in and went
   out (see below). For an ad-hoc slice, **Export filtered** gives the current
   filtered items as CSV or JSONL.

## Working as a team on one dataset

Projects are shared: everyone signed in sees the same projects, and every
curator's changes go into the same dataset. The usual way to split the work is
by class — Ana filters to `ACBR` (or a whole taxonomy group, such as all branching
corals) and reviews those, Ben takes `POCS`, someone else takes a region — and
the dataset grows from all of it at once. Nobody needs their own copy.

What keeps that safe is below; what makes it *visible* is versions.

## Versions and the change log

A **version** freezes the project's included items at a moment — v1, v2, … —
with each item's label, train/val/test split, source identity, crop references,
coordinates and metadata. **Versions** (the last
view) shows:

- **What publishing now would change** compared with the latest version: how
  many items would come in, go out or be relabelled, per class, with the net
  change — and who made those changes and on which classes. Read this before
  publishing; it is exactly what the change log will say.
- **Compare** picks any two published versions, or a version and the live draft,
  and shows in / out / relabelled / changed-split counts with a per-class table.
  Clicking a class opens the crop grid filtered to it. Items that moved to another
  split count as changed-split.
- **Publish version** (leads): adds release notes and freezes it. Building runs
  in the background; on a full NOAA project it takes a minute or two.
- **Every version so far**, each with its numbers, notes and contributors, and
  three files:
  - `CHANGELOG.md` — the in/out/relabel table per class, the relabels
    (e.g. `ACBR → POCS: 41`) and who contributed what;
  - `manifest.csv` — one row per item: label, split, crop and source-image
    URIs, location, annotator, last changed by;
  - `manifest.json` — the counts by class and split, the change summary and the
    contributors, for scripts.
- **Write image folders** (leads, optional): copies a version's crops into
  `train/<class>/…`, `val/…`, `test/…` — the layout YOLO and timm classifiers
  train from. It copies every crop, so it is slow on a large version; the
  manifest is enough to train from if your loader reads URIs.

**Original splits take priority.** A CSV `Split` (or `split`) column or an
item's `split` preserves original membership; `validation` normalizes to `val`.
Other nonblank values are rejected. OASIS migration preserves valid membership
and reports/skips invalid values. Only missing splits are assigned from a hash
of the source image; new items receive that generated split on creation.
Existing membership is never silently rearranged. Version statistics record
imported/generated/legacy split provenance and source images spanning multiple
splits. Generated grouping prevents leakage; original imported splits still need
checking. Seed and ratios are `CURATION_SPLIT_SEED` and
`CURATION_SPLIT_RATIOS` (default `0.8,0.1,0.1`). Different versions may have
different test membership or labels and are not automatically comparable.

Files go to `$LIB_DATA_DIR/curation_versions/<project>/v<N>/` (on the library
volume, and in backups); set `CURATION_PUBLISH_URI=gs://bucket/prefix` to also
copy them to a bucket you can write to. Published versions never change.

## Starter data on a new install

On a fresh install the app fills itself in, in the background, so people have
something to open. Nothing blocks start-up: the app is usable at once, and the
Curation page shows each part as it lands.

| What | Where it shows | Size / time |
|---|---|---|
| **NOAA Pacific T3**: Hawaii, Mariana and Samoa/PRIA point annotations from the public PIFSC bucket | a project | ~160 MB, 941,550 points, about 25 minutes |
| **NOAA Pacific T3 (Hugging Face)**: the `NMFS-OSI/noaa-pacific-benthic-cover-t3` dataset, all splits | a project (the Hub tab lists the dataset entry) | patches are read from the public bucket |
| **Source images for the Hugging Face patches**: once both imports are complete, each patch takes its full image, point position, site, region, island, year and annotator from the regional point with the same label and file name (`{label}_{image}_{n}.png`; all 378,680 match). Labels, splits, review state and the patch that is scored stay as the dataset has them (`curation/source_context.py`) | Source images and filters of the Hugging Face project | about 12 minutes, once (Oracle) |
| **yolo11m-cls NOAA Pacific T3**: the `NMFS-OSI` classifier (`weights/best.pt`) | the Hub tab and the classifier list | ~40 MB download |
| **Baseline evaluation**: the classifier, validated in the evaluation worker, scored once on the Hugging Face project's test and val splits (after v1 is published from the import) | the project's Confusion matrix | 113,634 patches: about 15 minutes to fetch them, then about 2 minutes of scoring on a GPU (much longer on CPU) |

It runs on the coordinator at every start and only fills what is missing: a
failed import of an empty starter project is queued again, an import cut off by
a restart is resumed (rows already in are skipped), a failed model download is
retried, and anything that needs Hugging Face is retried with
a growing wait (1 minute up to 30) until the network answers. Once everything
is in place, nothing more happens.

- **Turn it off or narrow it:** `CURATION_STARTER_DATA=off`, `regional` (only the
  bucket project) or `hub` (only the dataset and model). The compose files and the
  installer turn on `noaa-t3` (everything); without a setting the app does nothing.
  The installer's `OCEANFRAME_SEED_NOAA=no` sets `off`.
  The older `CURATION_SEED=noaa-pacific` still works and means `regional`.
- **Deleting sticks.** Delete a starter project and it is not created again on
  the next start (the data it imported stays in the bucket; you can always
  import it by hand).
- **The model** needs sign-in, or `CURATION_ALLOW_LOCAL_MODELS` for trusted
  local use, like any model registration; without either it is skipped with one
  log line. The Hugging Face owner must be allowed by `CURATION_HF_ALLOWED_ORGS`
  when that is set.
- Starter projects are marked in their settings (`starter`), which is how the
  app finds them again; a project called "NOAA Pacific T3" made by an earlier
  release is adopted, not duplicated.
- **The starter accounts** in `OCEANFRAME_STARTER_USERS` (the deploy scripts make
  `test`, a lead, and `user1`, a curator) are added to each starter project as it is
  created, as leads or curators, so they can curate from the first sign-in.
- **The baseline** waits for the import, the model and the worker; it publishes v1
  only when the project has no published version, and starts its run once (the
  run's id is kept in the project's settings as `baseline_run`). A run that fails
  is not restarted by itself: start a new one under Models and evaluation runs.

## Several people at once

- **Every change is attributed** to the signed-in person — never to a name the
  browser sends.
- **Every change is checked against the version the editor saw.** Each item has a
  `revision`. If Ana saves first, Ben's save of the same item is refused with
  "Ana changed this item while you had it open"; his screen then shows her
  version, and the message keeps what he had typed. A bulk edit applies every
  item that is unchanged and lists the ones that were not.
- **Everything is audited**: one event per change, with before, after, reason,
  person and time. Deleting a project removes its history with it; nothing else
  deletes history.
- **Roles** are per project, set in the Members dialog (opened from the curation
  header). A **curator** edits the project: items, bulk edits, quality decisions,
  points, imports and evaluations. A **lead** does all of that and also publishes
  versions, writes image folders, applies Hugging Face links and manages members.
  Whoever creates a project becomes its lead, and a project always keeps at least
  one lead. App-wide admins act as lead on every project. Any active user can be a
  member, including an app-wide viewer. Non-members see a read-only workspace with
  a notice naming the leads. App-wide roles still govern what a user may do outside
  a project: viewers read and export; curators create projects, register Hugging
  Face datasets and models, and register, upload and validate evaluation models
  (raise that bar with `CURATION_CREATE_ROLE=lead`). Imports and evaluations are
  project work: they need the project curator role. A project is deleted only by
  its owner or an admin.

Started through compose, curation lives in that deployment's Oracle database
(`make up` and the stack each run one as `oceanframe-oracle`), so everyone using
the same deployment sees the same data. `make up` and the stack are
alternatives, not one shared database: both publish Oracle on host port 1522,
so run one at a time, and data in one is not in the other. `/healthz` names the
backend; on SQLite the curation page shows everyone a "Local SQLite database —
not shared" badge (on Oracle there is no badge).

**One coordinator per database.** Exactly one app process per database runs the
import and evaluation workers and their start-up recovery (which marks imports,
version builds and evaluations left running by a dead process as interrupted).
Any other app process on the same database must set
`OCEANFRAME_BACKGROUND_WORKERS=off`, or it would interrupt the coordinator's live
work and claim its queued jobs. The T3 review (below) runs with workers off;
imports and evaluations it starts are run by the main app.

**Sign-in.** `make up` publishes on `127.0.0.1` (`OCEANFRAME_BIND`) with sign-in
off, so every visitor acts as the built-in admin; the app logs a warning when
sign-in is off on Oracle. For the LAN set `OCEANFRAME_BIND=0.0.0.0` together with
`OCEANFRAME_AUTH=on`. The stack signs in by default.

## Working together

**Committing.** Edits are live as soon as they are saved, for everyone. Each
person's edits collect as *uncommitted changes*: the header shows a pill such as
"3 uncommitted changes · Commit…". **Commit** opens a dialog where you write a
message (1–500 characters) and it groups your pending edits into one change set.
A lead can commit a member's leftover changes too; the change set then records
"committed by X for Y". Imports and Hugging Face link events are never part of a
change set; they stay in the audit trail only.

**History.** The top of the Versions view lists change sets, newest first, with
filters for person and "since vN", and an "Uncommitted: Ana 37 · Ben 4" line for
work not yet committed. Each change set expands to its items; **Show these items**
opens the crop grid on them (at most 1,500). Publish previews, `manifest.json`
and `CHANGELOG.md` list the change sets in each version and the uncommitted work
by person, so a version says who changed what.

**Comparing people.** Dataset status shows each person's changes and change sets,
the overlap (items more than one person touched), and disagreements (items where
their latest label or inclusion differ). **Show these items** opens them in the
crop grid.

**Comparing projects.** The Releases panel compares two projects, each either live
or at a published version. Items are matched by crop file name and source label.
It shows counts, a per-class table, and label and split disagreements; **Show in
A** opens project A's crop grid on the disagreeing items.

**Combined releases.** The Releases panel also builds one release from a published
version of each project, all in the same dataset mode. Only an app-wide lead who
is also a project lead on every source can build one.

- A crop with the same label in several sources is kept once.
- A crop with different labels in different sources is a conflict: it is left out
  and listed in `conflicts.csv`.
- A source image that falls in more than one split is listed in `leakage.csv`.
  Splits are not changed.
- Files: `manifest.csv` (with project and version columns), `manifest.json`, and
  `CHANGELOG.md` (the sources, and which source versions changed since the previous
  release of that name), plus `conflicts.csv` and `leakage.csv`. They are written to
  `_releases/<id>-<name>/r<N>/` under the versions directory
  (`$LIB_DATA_DIR/curation_versions/`), and uploaded to `CURATION_PUBLISH_URI` when
  it is set.
- A build interrupted by a restart is marked failed; start it again.

## Editing patches on the source image

On the **Source images** view a project curator can fix the points themselves,
not just their labels. Select a point, then:

- **Move** it by dragging its marker, or with the arrow keys.
- **Resize** it by dragging the corner handle, typing a size in the patch-size
  field, or using a size preset. Sizes are limited to 32–1024 px.
- **Remove** it with the Remove button (or Delete). A removed point is hidden
  from the source queue, crop grid, quality review, matrix evidence cards and
  exports, and left out of published versions, but nothing is deleted. On its
  source image it is always drawn, dimmed and dashed, whatever the filters: select
  it there and press **Restore**. Tick **Show removed** in the filters to list
  removed points in those other views again. Removing also excludes the point and
  flags it for review; restoring puts it back in the dataset.

Editing needs the project curator role and a source image that loaded. Patch-only
projects (from the Hugging Face registry) have no source image, and a point whose
image is missing from its bucket cannot be edited.

### Pending edits and "Save N edits"

Edits are not applied as you make them. They collect in a tray under the image,
each with a small preview cut in the browser, and the tray's button reads
**Save N edits**. Add an optional reason, then save: all the edits go to the
server in one request, as one batch in your uncommitted changes (one
`operation_id`, one audit event per item). **Discard** empties the tray.

Each item is checked once against the revision you loaded. If someone changed it
meanwhile, none of that item's edits are applied (a move and a resize of one
point succeed or conflict together); the others are, and the conflicts are
reported. A request with a point outside the image or an unknown label code is
refused whole and changes nothing.

Unsaved edits are guarded: switching view, project or image, or closing the tab,
asks before discarding them.

### Bulk add around a patch

**Bulk add** (on the selected point) lays out candidate patches in a **3×3** or
**5×5** grid around it, `Spacing` px apart (32–512, starting from the patch
size). Candidates that fall outside the image are left out. Choose a
classification model under **Model hints** (optional) and each candidate gets its
top three suggested labels with confidences; with **None**, or when no classifier
is available (you are told), candidates simply take the selected point's label.

Candidates join the tray as unsaved **Add** rows and appear on the image as
pending markers. Before saving you can drag a candidate to slide it, retype its
label (it must be a valid code for this project), click a hint chip to use that
label, or **drop** it. Nothing exists until you save. The grid centres on the
point where the image shows it, an unsaved move included. One save takes at most
200 new points: a bulk add that would go past that is refused with a notice, so
save, then add more.

### Keyboard

With the Source images view focused (not while typing in a field or with a dialog
open), on an editable point:

| key | does |
|---|---|
| Arrow keys | nudge the selected point 1 px |
| Shift + arrow | nudge 10 px |
| Delete or Backspace | remove the point; restores it if already removed |
| Esc | cancel a drag in progress |
| Alt + Left / Right | previous / next image in the queue |
| Left / Right | previous / next image, when the point is not editable |

Nudges are queued as pending moves like any other edit; a removed point does not
nudge.

### Where patches come from

Patches are cut from the source images, not read from stored crops, whenever a
point has been moved, resized or added by hand: previews, quality review, the
image-folder export of a version and model evaluations (the patches staged for
scoring and the version-member evidence cards) use a fresh cut at the published
geometry, never an old crop of the previous position. If that source image is
missing or unreadable, the evaluation counts the patch as missing, like any
unreadable crop. The CSV manifests (the item export, a version's manifest and a
release's CSV) still list the item's original `crop_uri`: for an edited patch,
cut it from `source_image_uri` at `row`/`column` (and `crop_size`, in version and
release manifests) instead of fetching that file. Source
images and the patches cut from them are cached on the server's disk
(`CURATION_IMAGE_CACHE`, bounded by `CURATION_IMAGE_CACHE_MB`; oldest-used
entries go first), so dragging and paging do not keep re-reading the bucket.
Nothing is ever written to a bucket. If the cache cannot be written, requests are
served without it. A moved patch has a new cache key, so it never shows its old
crop.

### Missing source images

Some annotations point at images that are no longer in the bucket. The **missing
source-image check** lists each `images/` prefix a project uses once (about a
minute for 93,000 images) and flags every point whose image is not there. It
runs automatically after each import, and on demand from **Dataset status** →
**Missing images** → **Check again** (curators). That card shows how many images
and points are missing, by region and year, and **Download CSV** gives
`source_image_uri, region, survey_year, points`.

In the source queue, **Hide missing images** (on by default) removes them; with
it off they are sorted after the available images. A missing image still opens,
with a notice naming the bucket path, but its points cannot be edited.

### Navigation

Opening an image from the crop grid, quality review or an evaluation evidence
card under the confusion matrix (the **Source image** buttons) shows a
**Back to …** bar above the image. Clicking a matrix cell, a change set or a
compare class link opens its view without a Back bar. It returns
to that view at the scroll position you left. The bar disappears when you change
view or project, or arrive by a link, and Back stays put (keeping your edits) if
you decline to discard unsaved ones.

The address keeps your place: `/curation#project=<id>&view=source&image=<id>&item=<id>`
opens that project's image and selects that point, so a link can be shared. `image`
is the id of the image's first item; `item` is optional. Links that change
project, view or image behave like navigation, not Back.

### Releases and old versions

Releases and project comparison need the frozen per-item inputs that versions
record. A version published before releases were supported has none, so building
a release from it or comparing it is refused (422) with "was published before
releases were supported — publish a new version first". Publish a new version and
use that.

## Data and settings

| setting | default | what |
|---|---|---|
| `CURATION_TAXONOMY_FILE` | `curation/data/taxonomy_tree.json` | the TIER0 → TIER_1 → … → CODE tree |
| `CURATION_IMPORT_SOURCES` | NOAA PIFSC regional CSVs | JSON list of `{name, root_uri, csv_uri}` |
| `CURATION_IMAGE_BUCKETS` | — | extra buckets the image proxy may read (comma separated) |
| `CURATION_T1_CROP_ROOT`, `CURATION_T3_ALL_ROOT`, `CURATION_SPLIT_ROOT` | PIFSC mirror | where pre-cut crops are looked for before cutting one from the source image |
| `CURATION_RARE_MIN` | 10 | a class, region or other group is rare when val or test holds fewer items |
| `CURATION_EVAL_DIR` | `$LIB_DATA_DIR/curation_eval` | pinned evaluation artifacts (below) |
| `CURATION_ALLOW_LOCAL_SOURCES` | off | let imports read CSVs from the server's disk (development only) |
| `CURATION_EVALUATION_SPOOL` | `$CURATION_EVAL_DIR/worker` | shared worker inputs and outputs, separate from finalized results |
| `CURATION_TRUSTED_CHECKPOINTS` | off | explicit operator opt-in to execute trusted `.pt` checkpoints in the isolated worker |
| `CURATION_ALLOW_LOCAL_MODELS` | off | permit model registration with authentication disabled, trusted local development only |
| `CURATION_MODEL_MAX_BYTES` | 1 GiB | streamed checkpoint size limit |
| `CURATION_EVAL_FETCH_WORKERS` | 32 | patches fetched in parallel while staging an evaluation |
| `CURATION_EVAL_MAX_MISSING` | 0.05 | share of patches that may stay unscored before a run fails |
| `CURATION_PATCH_CACHE` | `$CURATION_EVAL_DIR/patch_cache` | downloaded evaluation patches; `off` disables |
| `CURATION_IMAGE_CACHE` | `$LIB_DATA_DIR/curation_cache` | disk cache of source images and cut patches (never written to buckets) |
| `CURATION_IMAGE_CACHE_MB` | 2048 | size cap for that cache; oldest-used entries are evicted first |
| `OCEANFRAME_BACKGROUND_WORKERS` | on | `off` for every app process but one on a database (see above) |

The Hugging Face registry has its own settings, listed in its section below.

**CSV format.** CoralNet's annotation export: `Name, Date, Region, Island, Site,
Row, Column, Label code` are required; `Label ID, Annotator, Date annotated,
Latitude, Longitude, Split` and `Machine suggestion 1–5` / `Machine confidence 1–5`
(percent) are used when present. Images are expected at `<root_uri>images/<Name>`
and crops at `<root_uri>crops/<label>/<label>_<stem>_<n>.png`.

**Images** are served through `/api/curation/image`, which reads only from the
buckets above (the server's credentials are not a general-purpose proxy). The
source view asks for the raw EXIF orientation, because annotation coordinates
are in the camera's pixel grid.

**The external T3 benchmark** is distinct from a project's generated splits.
Its original test bundle lives in `$CURATION_EVAL_DIR/v0/`; optional real
training/validation bundles live under `v0/train/` and `v0/val/`. A matrix is
offered only for splits with predictions. Dataset-member browsing of the
configured original train/val folders does not invent predicted labels.
The seeded NOAA project explicitly links this baseline; unrelated T3 projects
can choose it as an external reference, while T1 projects cannot use it.
No installed results means an evaluation-empty state, not a silent switch to CSV.

**Point classification** in the add-point dialog needs a YOLO classification
model (`*.pt`) in `LIB_MODEL_DIR` and the ML image; the slim image says so (501).

## Hugging Face registry

Datasets and models can be registered from public Hugging Face repositories.
Each registration is pinned to one commit (the `sha`, not a branch), so what
you registered today is what an evaluation reads next year. Only public
repositories are supported, and no token is ever sent. A private or unknown
repository answers 404 (the hub does not say which); a gated one is refused
with 422. Curators register and create projects from datasets (the
`CURATION_CREATE_ROLE` bar); applying a link to an existing project needs the
project lead role, though anyone who may register can preview one. Any signed-in user can
read. Set `CURATION_HF_ALLOWED_ORGS` to restrict registration to named owners.

**Datasets.** The column map (image location, label, split, and the rest) is
detected from the dataset card's features; after **Register** the dialog shows
it as editable choices, with the result of the sample check ("N of 5 sample
patches found") and the label report (labels outside the chosen tier, with
counts). Only then can a project be created or linked. Changing a column
registers again; registering the same commit with another column map or
location is a 409 naming what it is registered with. The patches are found one
of three ways:

- `column`: the image column itself holds `gs://…` or
  `https://storage.googleapis.com/…` (stored as `gs://`);
- `env_root`: this server's `CURATION_SPLIT_ROOT`;
- `custom`: another `gs://` root.

Both roots are read as `root/split/label/file`. Every patch must be inside a
curated image bucket (`CURATION_IMAGE_BUCKETS` or the built-in NOAA ones),
because the image proxy only serves those.

A dataset becomes either a **new patch-only project** or is **linked to an
existing project**. A patch-only project has no source images: its items show
"Patch only" and manual points are refused. (The starter NOAA Pacific T3 Hugging Face
project does not stay patch-only: its patches are given their regional source images; see
[Starter data on a new install](#starter-data-on-a-new-install). That dataset was split by
patch, not by image, so most full images have patches in more than one split, and versions
report them as cross-split source images.) Splits outside train/val/test (e.g.
`unlabeled`) can be imported; their items arrive unassigned and publishing gives
each a split by hash, while the dataset's own split name stays in the item's
metadata. An import that failed, was cancelled or was interrupted can be
resumed (**Resume import**, leads): the same splits are read again and rows
already imported are skipped. **Linking never adds items**: it attaches the
dataset to items the project already has. It previews first (what matches,
what is ambiguous, which splits would change), writes audit events for what it
changes, never overrides splits that came from a CSV, never adopts a split
outside train/val/test, and leaves published versions untouched.

**Models.** Pick a `.pt` file from the repository. The weights go through the
same trusted-classifier path as an upload (`CURATION_TRUSTED_CHECKPOINTS`) and
are validated in the isolated worker; the web process never unpickles them.
Models for other tasks are catalogued only: recorded and linked, never loaded.
Registering returns at once: the coordinator app downloads the weights in the
background, then registers and validates them (a second app on the same
database can queue registrations; the coordinator runs them). A failed
download shows in the Registry panel with a **Retry** button (curators).

**Versions** published from a registered dataset record `derived_from`: for each
dataset the project came from or was linked to, its `repo_id`, `revision` (the
pinned commit), `relation` (`source` or `linked`) and `partial: true` for a link
that did not finish. No file hashes are recorded. The change log lists the same,
marking partial links.

| setting | default | what |
|---|---|---|
| `CURATION_HF_ALLOWED_ORGS` | any | comma-separated owners that may be registered |
| `CURATION_HF_ENDPOINT` | `https://huggingface.co` | hub base URL (a mirror, or a test server) |
| `CURATION_HUB_CACHE` | `$CURATION_EVAL_DIR/hub` | pinned parquet and weights downloaded from the hub |
| `CURATION_HUB_MAX_BYTES` | 2 GiB | largest single file the server will download |

The web container needs outbound HTTPS to huggingface.co. The evaluation
worker stays offline: it only ever sees files the web process already staged.

## Evaluating Published Versions

1. Publish a version. In **Models and evaluation runs**, register configured
   weights or upload a `.pt` classifier, explicitly confirming its trusted source.
   Registering, uploading and validating evaluation models follows
   `CURATION_CREATE_ROLE`; project curators and leads start evaluations;
   everyone else reads results.
2. Validate the classifier in the isolated worker. Detection/SAM checkpoints and
   incompatible class labels are rejected. Registration itself only stores and
   hashes bytes; it never deserializes a checkpoint in the web process.
3. Choose a published version, validated classifier, CPU/GPU, batch size and
   splits (all available by default), then **Evaluate**. The worker applies the
   classifier's Ultralytics preprocessing at 224 pixels, records top-k
   predictions and computes scikit-learn metrics. No confidence cutoff or
   detection/background matching is applied.
4. Inspect a split's matrix or the explicitly mixed-split aggregate. Training
   accuracy and mixed-split accuracy are not held-out performance. Coverage,
   SHA256 model/input identity, runtime versions and leakage warnings are retained.

Evaluation inputs are materialized from the version's frozen references before
inference, converted to RGB PNGs, and content-hashed. Saved crops, original
labels/splits and predictions are immutable after completion. A remote URI is
not a guarantee of unchanged bytes before materialization: compare input
manifest hashes, not just version numbers. Legacy versions without frozen
input records must be republished. Patches that cannot be fetched or decoded are
reported, not hidden (see Missing patches below). Failed, cancelled and interrupted runs
have no completed matrix; create a new run to retry.


**Staging.** Patches are fetched in parallel (`CURATION_EVAL_FETCH_WORKERS`,
default 32), which is what makes a 50,000-image split practical. Downloaded
patches are kept in a cache (`CURATION_PATCH_CACHE`, default
`$CURATION_EVAL_DIR/patch_cache`; `off` disables it) so the next run on the
same version starts from disk. The cache is keyed by URI and never
revalidated: if objects at the same URIs are replaced, delete the directory.

**Missing patches.** A patch that cannot be fetched or decoded is left
unscored rather than guessed. The run lists each one, with a reason, in
`missing.csv` (a download on the run's page), and coverage reports the
shortfall. If more than `CURATION_EVAL_MAX_MISSING` of the patches are missing
(default 0.05, i.e. 5%) the run fails instead of publishing numbers on a
shrunken test set. The worker's inputs in the spool are removed after a run,
so the spool does not grow with every evaluation.

The web coordinator and worker communicate through staged files. One app
coordinator and one worker are supported, not multiple app processes sharing
the same queue: any second app on the database runs with
`OCEANFRAME_BACKGROUND_WORKERS=off`. Validating and evaluating a model needs the
worker running (`oceanframe-evaluation`, part of the stack);
`make up` passes the `CURATION_*` settings but runs no worker. Terminal results/progress live in the application database;
worker inference can finish while the web app restarts. Interrupted staging
or worker execution is reported explicitly. Keep the evaluation store and
worker spool in backups; inference disk usage includes checkpoint copies and
both staged and finalized images. Task/output retention is operator-managed.

### Draft Corrections

**Model predictions** shows scored examples; **Dataset members** shows the
selected immutable version's membership, including splits that were not scored.
Actual/predicted class selections in the latter select dataset classes, not
imaginary predictions. Only uniquely linked project items are editable.

Corrections change the current draft using revision checks and the existing
audit trail, with the originating evaluation/version/split recorded. Test
corrections require confirmation and mark future versions `test_informed`.
They do not change old results, benchmark labels or split membership. Test-
informed datasets are not an untouched holdout; use a separate holdout for an
independent claim of model improvement. Publish and evaluate again for new
results. Project deletion is blocked until active evaluation work is cancelled.

### Isolated Worker Deployment

`.pt` checkpoints may execute code. Upload only trusted weights; extension/hash
checks do not make an untrusted pickle safe. Execution requires explicit
`CURATION_TRUSTED_CHECKPOINTS=on` in both app and worker configuration. The worker
image contains only the evaluator module and dependencies, with no app source,
database/cloud credentials, network or writable root filesystem. Inputs are
read-only; output and bounded temporary directories are writable. It has dropped
capabilities and CPU/memory/PID limits. These controls reduce exposure, not a
guarantee that arbitrary hostile checkpoints are safe.

The worker, `oceanframe-evaluation`, is part of `docker-compose.stack.yml` and
starts with it. Its spool is two named volumes (`oceanframe-eval-inputs`,
`oceanframe-eval-outputs`), or the host directories in `OCEANFRAME_EVAL_INPUTS`
and `OCEANFRAME_EVAL_OUTPUTS`, owned by the UID/GID the images were built with.
`CURATION_TRUSTED_CHECKPOINTS` is `off` unless `.env` turns it on, which the deploy
scripts do. With it on, anyone who may register models (curators, by default, as
`CURATION_CREATE_ROLE`) can upload a `.pt` that the worker unpickles; uploads are not
limited by `CURATION_HF_ALLOWED_ORGS`, which only governs Hugging Face references.

```bash
docker compose -f docker-compose.stack.yml up -d --build
```

For GPU, add `-f docker-compose.evaluation.gpu.yml` (the worker) and, if you
want CLIP and SAM 3 on it too, `-f docker-compose.stack.gpu.yml` (the app); select
GPU 0 in the evaluation form;
`EVALUATION_CPUS` (2) and `EVALUATION_MEMORY` (8g) bound worker resources.
Do not mount the Docker socket, deployment `.env`, service-account files or
database volumes into the worker. The core app can read existing results without
the ML packages or worker being installed.

### Installing The OASIS T3 Results

Locate the completed bundle in the OASIS evaluation workspace or its backed-up
storage. Install it with:

```bash
python -m scripts.import_curation_evaluation /path/to/results/v0 \
  --model /path/to/yolo11m_cls_noaa-pacific-benthic-t3.pt
```

The optional model argument verifies actual weight bytes. The installer requires
67 classes, 56,864 test predictions, completed pinned model/dataset revisions,
and agreement among CSV predictions, confusion counts, metrics and provenance.
It stages the bundle before installation, permits identical reruns and refuses
to overwrite different installed evidence. Supplemental bundles must have their
true `dataset_split` and the exact same model hash and class ordering:

```bash
python -m scripts.import_curation_evaluation /path/to/train-results --split train
python -m scripts.import_curation_evaluation /path/to/val-results --split val
```

If the test bundle is missing, retrieve the classifier at model revision
`77e10ec10d33dfb7f0a9ebabc542b2008bb96637` and original dataset revision
`b47bff4ecdcdb026d31d1a154ff267227ff0f1b6`. Run OASIS's
`evaluate_t3_v0.py --model ... --test-root ... --output ... --device cpu` in an
isolated ML environment with only trusted weights, the original test folder and
an empty output directory mounted. Do not reuse an old predictions CSV: that
script resumes by filename and can otherwise mix runs. Then install and verify
the artifacts. Regeneration cannot recover missing original dataset membership;
do not substitute OceanFrame's generated splits and call it the original T3 test.

### CPU Review Of The Original OASIS Results

`docker-compose.t3-review.yml` runs the core image, with no Torch, GPU or
inference worker. It serves installed results from a read-only
`library_data/curation_eval` mount. The imported annotation pool and the reviews
live in the shared Oracle database, the same projects the main app shows. Contents of
the old `library_data/t3_review` SQLite database are not migrated and need re-importing.
It binds to localhost only
and requires a sign-in. Start an Oracle first, since reviews are saved there:
`make up` (the review joins `oceanframe-local`, the default) or the stack (set
`OCEANFRAME_NETWORK=oceanframe-net`). It runs with
`OCEANFRAME_BACKGROUND_WORKERS=off`: one coordinator per database, and the main
app is it.

```bash
docker compose -f docker-compose.t3-review.yml up -d --build                                  # with make up
OCEANFRAME_NETWORK=oceanframe-net docker compose -f docker-compose.t3-review.yml up -d --build   # with the stack
```

Sign in, then open
`http://127.0.0.1:8094/curation`; `T3_REVIEW_PORT` can override the port.
Restart with `docker compose -f docker-compose.t3-review.yml restart`.

The restored August 29 OASIS baseline contains 56,864 actual Test examples
across 67 classes: top-1 accuracy 59.84%, top-5 accuracy 85.91%, macro F1 53.85%.
Re-run through the Hugging Face registry on 2026-10-12, it reproduced. The setup:
the model at `77e10ec1` (`weights/best.pt`), scored on v1 of a patch-only project
built from the dataset's test split at `b47bff4e`, on CPU at 224 px. All 56,864
patches were scored, with none missing. Top-1 was 59.86%, top-5 85.93% and macro F1
53.87%. The run took 21 minutes in total, about 30 patches/s while staging.
The original 941,550-point annotation pool is **unsplit**, not another
evaluation split. Its missing split values remain missing after migration.
The linked project starts on the pinned Test matrix; All project items is the
separate full-pool option. Original OASIS files are never modified. Training
and validation folder inventories may be browsed, but have no model results
unless corresponding evaluated predictions are installed.

### API Additions

- `/evaluation-models`: catalog and configured-reference registration;
  `/upload` stages weights and `/{id}/validate` submits isolated validation.
- `/projects/{id}/evaluations`: list/create runs; `/{run_id}` reads durable
  state and `/{run_id}/cancel` requests cancellation.
- `/projects/{id}/confusion-matrix?matrix_source=evaluation&evaluation_id=...&split=test`
  reads one run; `split=all` aggregates its evaluated splits.
- `/projects/{id}/matrix-assets?evaluation_id=...&split=...&evidence_kind=predictions|inventory`
  returns paginated evidence or version members; evidence for removed points is
  left out unless `removed=any`. A version member that was moved, resized or
  added is previewed by `/evaluations/{run_id}/inputs/{item_id}/image`, cut from
  its source at the frozen geometry. Corrections use the run's
  `/predictions/{prediction_id}/item` or `/inputs/{item_id}/item` PATCH routes.
  Existing imported-CSV and pinned-test endpoints remain compatible.

## Coming from OASIS

What changed, for anyone who used the OASIS version:

- API at `/api/curation/…` (was `/api/v1/classification-curation/…`), same shape.
  `curator_name` / `reviewer` in requests are accepted and ignored.
- Quality decisions now go through the revision check and the audit trail (in
  OASIS they bypassed both); bulk edits can carry revisions and report conflicts.
- `island` and `annotator` are real, indexed columns rather than JSON lookups —
  which is also what makes it run on Oracle.
- Move existing work with `scripts/import_oasis_curation.py` (see the README).
