# Curation overhaul, part 1: Oracle by default + Hugging Face registry

## Context

The curation tools work well, but three things are missing:
- They appear not to use the Oracle database.
- There is no way to register datasets and models from Hugging Face, e.g.
  `NMFS-OSI/noaa-pacific-benthic-cover-t3` and `NMFS-OSI/yolo11m-cls-noaa-pacific-benthic-cover-t3`.
- Curating with several people is awkward.

That request covers five largely independent subsystems, so we split it into sub-projects. Each gets its own spec, plan and build.

| # | Sub-project | Status |
|---|---|---|
| **A** | Oracle as the default everywhere | **this spec** |
| **B** | Hugging Face registry (pull only) | **this spec** |
| D | Easier curation: patch models reading from the cloud, fixing and adding boxes and patches, smoother flow between tabs, confusion-matrix and embedding mining, FiftyOne bridge | next |
| C | Multi-user workflow: project membership, assignments, review queues, proposals | after D |
| E | Detection and segmentation datasets and models | after C |

### What exploration found

**Curation already supports Oracle; the default commands just don't start it.**
- `appdb/engine.py:58-75` picks Oracle when `OCEANFRAME_DB_USER`/`OCEANFRAME_DB_DSN` are set. Auth and all 11 `curation_*` tables go through it.
- Only `docker-compose.stack.yml` starts Oracle (`gvenzl/oracle-free:23-slim`).
- `make up`, `make up-ml` and `make up-gpu` use `docker-compose.yml`, which has no Oracle, so they fall back to `$LIB_DATA_DIR/app.sqlite`.
- `docker-compose.t3-review.yml` pins `OCEANFRAME_DB_URL=sqlite:///...` and runs with sign-in off.
- No local `app.sqlite` holds curation data (only `library_data/t3_review/catalog.sqlite`, the library catalog). **So no SQLite-to-Oracle copy script is needed.**

**The HF repos already line up with the existing T3 v0 pins.**
- The dataset's commit is `b47bff4e…`; `curation/evaluation.py:32-33` has it as `EXPECTED_DATASET_REVISION`.
- The model's commit is `77e10ec1…`, already `EXPECTED_MODEL_REVISION`.

**The HF dataset is a list of patch URIs, not image bytes.**
- Files: `data/{train,val,test}.parquet` (plus CSV and COCO copies).
- Columns: `image_url, gcs_uri, label, label_name, label_desc, file_name, split`.
- Splits: train 265,046 / val 56,770 / test 56,864.
- `file_name` follows the CSV importer's crop naming (`curation/importer.py:93`): `{label}_{source_image_stem}_{ordinal}.png`. For example, `ACAS_AGU-641_2025_A_09_9.png` is source image `AGU-641_2025_A_09`, point 9.
- `service.attach_curation_links` (`curation/service.py:580`) already matches crops to items by (source_label, file_name).

**The HF model is an Ultralytics classifier.**
- `pipeline_tag=image-classification`, `library_name=ultralytics`, license AGPL-3.0.
- The repo has two checkpoints, `weights/best.pt` and `yolo11m_cls_noaa-pacific-benthic-t3.pt`, plus an `.onnx` and several confusion-matrix PNGs.
- The card links the dataset (`datasets: [NMFS-OSI/noaa-pacific-benthic-cover-t3]`).

**Images come from Google Cloud Storage, configured by env.** You asked about this:
- The default PIFSC bucket is in `curation/settings.py:15`.
- `CURATION_SPLIT_ROOT` (default `…/pacific_t3_splits`) is exactly where the HF rows' `gcs_uri` points.
- `CURATION_T3_ALL_ROOT`, `CURATION_IMPORT_SOURCES` (regional `root_uri`s) and the allow-list `CURATION_IMAGE_BUCKETS` are also env settings.
- Part B uses those as defaults and lets the lead override them per dataset.

### Decisions made with you
- Pull only. No pushing to HF yet.
- Public repos only, no tokens. Only leads register.
- Registering an HF dataset supports both outcomes:
  - **create a new patch-only project**, or
  - **link it to an existing project** (adopt its splits and record provenance).
- Image location defaults to the env bucket config, with an optional per-dataset override.
- T3 review moves to the shared Oracle database with sign-in **on**.

---

## Part A: Oracle as the default

1. **Compose.** Add the `oceanframe-oracle` service to `docker-compose.yml`, copied from `docker-compose.stack.yml:99-115`, with the same volume, healthcheck and loopback-only port.
   - Give `app`, `app-ml`, `app-gpu` and `dev` the `OCEANFRAME_DB_USER/PASSWORD/DSN` env (reuse the stack's YAML anchor pattern) and `depends_on: {oceanframe-oracle: service_healthy}`.
   - `test` and `quickstart` stay on SQLite. Unit tests need it, and quickstart only indexes the library.
   - `make env` / `.env.example` must make sure `ORACLE_PASSWORD`, `APP_SCHEMA_NAME` and `APP_SCHEMA_PASSWORD` exist. Generate random values on first `make env` if they are missing, and print where they went.
2. **T3 review.** Drop the SQLite `OCEANFRAME_DB_URL` and set `OCEANFRAME_AUTH: "on"`.
   - Join the main compose network and Oracle (same DSN and schema env), so reviews land in the real projects, attributed to accounts.
   - Keep port 8094, read-only eval mount, and no Torch.
3. **Make the backend visible.** Nobody should end up on SQLite without knowing.
   - `appdb/engine.py`: add `backend_name()`, which returns `"oracle"` or `"sqlite"`.
   - Log `App database: oracle (host/service)` once at startup (`main.py` lifespan).
   - `/healthz` adds `"app_db": backend_name()` without opening a connection (keep its current no-I/O rule, `main.py:74`).
   - Admins see a small badge in the curation header when the backend is SQLite: "Local SQLite database — not shared".
4. **Opt-in strict mode.** With `OCEANFRAME_REQUIRE_ORACLE=on`, `init_app_db()` refuses to start on SQLite. The new compose services and the stack set it. The SQLite fallback in `engine.py` stays for tests and laptops.
5. **Docs.** Update the README and `docs/CURATION.md`:
   - `make up` now starts Oracle.
   - How to point at an external Oracle (`OCEANFRAME_DB_DSN`).
   - T3 review needs sign-in.

## Part B: Hugging Face registry

### Data model

Add **one new table** in `curation/models.py`. It is created by `create_all`, so no migration is needed. Use `Str(n)` and `JSONText` and keep index names short, as Oracle requires.

```
curation_hub_entries  (CurationHubEntry)
  id            Integer Identity PK
  kind          Str(10)   'dataset' | 'model'
  repo_id       Str(200)  e.g. NMFS-OSI/noaa-pacific-benthic-cover-t3
  revision      Str(40)   resolved commit sha (pinned)
  requested_ref Str(100)  what the lead typed (main, a tag, a sha)
  task          Str(40)   image-classification | object-detection | … (from card; D/E reuse it)
  runnable      Boolean   True only for what this app can use today
  card          JSONText  pipeline_tag, library_name, license, tags, linked datasets, splits+counts, features
  files         JSONText  [{path, size}] at that revision
  options       JSONText  dataset: column_map, image_root override, source_image_root; model: weight_file
  classifier_id FK curation_classifiers.id NULL   (model entries, once weights are registered)
  status        Str(20)   registered | importing | ready | failed
  error_message Text
  created_by_user_id FK of_users, created_at
  UNIQUE (kind, repo_id, revision)   -- re-registering is idempotent; a new revision is a new entry
```

Projects and items already have JSON columns for provenance, so they need **no** schema change:
- `CurationProject.source_config["hub"] = {entry_id, repo_id, revision}`
- `CurationItem.metadata_json["hub"] = {repo_id, revision, split, row}`

### Modules
- **`curation/huggingface.py`.** The only code that talks to the network; tests replace it with a fake.
  - `parse_ref(text)` accepts `https://huggingface.co/datasets/O/N`, `https://huggingface.co/O/N`, `…/tree/<rev>` and `O/N` plus a kind.
  - `repo_info(kind, repo_id, ref)` uses `huggingface_hub.HfApi`. It resolves the sha and rejects private or gated repos with a 422 "Only public Hugging Face repositories are supported".
  - `open_file(kind, repo_id, sha, path)` streams `hf_hub_download(..., revision=sha)`, capped at `CURATION_HUB_MAX_BYTES`.
  - Errors map to HTTP: hub unreachable 502, repo or revision missing 404.
  - Respects `CURATION_HF_ALLOWED_ORGS` (optional comma list; empty means any public repo).
- **`curation/registry.py`.** The service layer:
  - `lookup(text, kind)` returns a preview: card summary, files, splits, detected column map, `runnable` and the reason when it is false, plus warnings.
  - `register_dataset(...)` and `register_model(...)`.
  - `create_project_from_dataset(...)`, `link_dataset(..., dry_run)`.
- **`routers/registry.py`.** Prefix `/api/curation/hub`, registered in `main.py`. GET routes are open to any signed-in user; writes use `Depends(require_lead)`, the same pattern as `routers/curation.py:79`. `routers/curation.py` (725 lines) stays as it is.
- **Dependencies** (`requirements.txt`, core image, no Torch): `huggingface_hub`, `pyarrow`. Parquet is HF's standard format, and the CSV copies are not guaranteed to exist.

### Flow 1: register a model
1. The lead pastes a URL or repo id and clicks **Look up**. The preview shows task, library, license and linked dataset, plus links to the card's confusion-matrix images.
   - The lead **picks the `.pt` file**; this repo has two.
   - Non-classification or non-Ultralytics repos can be saved as **catalog-only** entries (`runnable=false`, reason shown) so D and E can use them later.
2. **Register** streams the chosen file into the existing `model_registry.register_stream`, which already handles SHA-256, the size cap and the trusted confirmation.
   - Provenance is `{source: "huggingface", repo_id, revision, file}`. The entry's `classifier_id` is then set.
   - The web process never unpickles the weights.
3. **Validate** uses the existing `model_registry.submit_validation`, which runs in the isolated worker (`network_mode: none`).
4. **Evaluate on a version** jumps to the confusion-matrix view with that classifier preselected (existing `evaluation_runner.start_run`).
5. The pinned T3 v0 baseline gains a link to the matching registry entry when one exists with the same repo and sha (`evaluation.py:172` provenance fields).

### Flow 2: register a dataset
1. **Look up** reads `cardData` (configs, splits, features) and lists the parquet files at the pinned sha.
2. **Column mapping** is auto-detected and editable:
   - image: `gcs_uri`, else `image_url`, else `file_name` with a root
   - label: `label_name`, else `label` with ClassLabel names
   - split: `split`, else the config split
   - file name: `file_name`, else the basename of the image
   - **Scope:** datasets whose rows hold image URIs or paths. Datasets that embed image bytes are rejected with a clear message (out of scope for v1).
3. **Image location**, i.e. where patches are read from:
   - (a) the dataset's own `gcs_uri`. This is the default when present, and here it already points at the env's `CURATION_SPLIT_ROOT` bucket.
   - (b) the env default root `{CURATION_SPLIT_ROOT}/{split}/{label}/{file_name}`.
   - (c) a lead-supplied `gs://` prefix with the same `{split}/{label}/{file_name}` template.
   - Any root must pass the same bucket allow-list as the image proxy (`curation/images.py`, `CURATION_IMAGE_BUCKETS`), or registration fails with 422. This stops the proxy from becoming a way to read arbitrary URLs.
   - `gs://` is preferred over `https` and goes through `curation/storage.py`.
   - Before saving, 5 sample rows are fetched and the result is shown: "5/5 patches found".
4. **Optional source image root** for context. Defaults to the regional `root_uri`s from `CURATION_IMPORT_SOURCES`, plus `images/{stem}.*`.
   - The source stem is parsed from `{label}_{stem}_{ordinal}.png`.
   - It is resolved lazily when an item is viewed, not at import.
5. The lead then chooses one of two outcomes.

**5a. Create a new patch-only project**
- A new `source_kind="huggingface"` item generator runs **through the existing `CurationImportOperation` pipeline** in `curation/importer.py`. That gives progress, cancel, batching, the Oracle statistics refresh and idempotent `source_key` for free.
- Rows are read in batches with `pyarrow.parquet.ParquetFile.iter_batches`.
- Per row:
  - `source_key = "hf:{split}:{label}:{file_name}"`
  - `crop_uri` comes from the image location
  - `source_image_uri = crop_uri`, because the column is NOT NULL; the source image is only resolved lazily (step 4)
  - `row_px`/`column_px` are null
  - `split` comes from the row
  - labels pass through the same `taxonomy.effective_label` rule as the CSV importer
- The preview lists labels missing from the project's tier, with counts, before import starts.

**5b. Link to an existing project**
- Matching generalises `service.attach_curation_links` (`curation/service.py:580`) into a shared matcher on (source_label, file_name), chunked IN lists.
- A **dry run** reports four counts: matched, unmatched (dataset rows with no item), ambiguous, and split changes.
- **Apply** writes `metadata_json["hub"]` and, if "adopt dataset splits" is ticked, sets `split`.
  - Every change bumps `revision` and writes a `curation_events` row (`event_type="hub_link"` or `"split_adopted"`, with old and new values), so it appears in the audit and the next version's CHANGELOG.
  - Items already in published versions keep their frozen version rows; only future versions change. The dry run says so.
- Runs as a background operation with progress, because there are about 379k rows.

### Versions
For projects with `source_config["hub"]`, or items linked to an HF dataset, `curation/versions.py` adds `{repo_id, revision}` to `manifest.json` and a "Derived from" line to `CHANGELOG.md`.

### UI
No new tab: you've said the tabs are already disjointed, and D reworks navigation.
- **New project dialog** (`templates/curation.html:513`) gets a "From Hugging Face" option: paste URL → preview → column map and image location → create.
- **Register model form** (confusion-matrix view, `curation.html:429`) gets a "Hugging Face" source next to upload and catalog: paste URL → pick `.pt` → register → validate.
- **Registry list:**
  - A small read-only panel opened from a header button "Registry" (all roles): entries with repo, short sha (linking to `huggingface.co/<repo>/tree/<sha>`), task, runnable, status, who registered it and when, and the linked project or classifier.
  - Leads also get "Link to project…" on dataset entries.
- New JS lives in `static/curation/hub.js` (an ES module, like `static/library/*.js`) so `curation.js` (2,092 lines) doesn't grow.
- **Patch-only items:** the source-image view hides the point overlay and "Add point" when `row_px` is null and shows "Patch only". `images.crop_from_source` returns early for those items.

### Files touched (main ones)
- New:
  - `curation/huggingface.py`, `curation/registry.py`, `routers/registry.py`
  - `static/curation/hub.js`
  - `tests/test_registry.py`, `tests/fixtures/hf/*.json` (the API responses fetched during planning)
- Changed:
  - `curation/models.py`, `curation/importer.py`, `curation/service.py` (shared matcher), `curation/images.py`, `curation/versions.py`, `curation/settings.py` (new env getters), `curation/model_registry.py` (provenance passthrough only)
  - `main.py`, `appdb/engine.py`, `appdb/bootstrap.py` (strict mode)
  - `templates/curation.html`, `static/curation/curation.js` (patch-only guard, hooks to `hub.js`)
  - `requirements.txt`
  - `docker-compose.yml`, `docker-compose.t3-review.yml`, `Makefile`, `.env.example`
  - `README.md`, `docs/CURATION.md`

---

## Verification
1. **Unit tests, on SQLite:** `python -m pytest`. New `tests/test_registry.py`, following the stubbing style of `tests/test_evaluations.py`:
   - `parse_ref` variants.
   - Lookup pins the sha; gated or private repos are rejected; an org outside the allow-list is rejected.
   - Column auto-detect on the real NMFS-OSI schema.
   - Image-location modes (a), (b) and (c), plus a rejected bucket outside the allow-list.
   - Create-project imports a small `pyarrow`-written parquet with correct `crop_uri`, splits and labels, and is idempotent when re-run.
   - Link dry run and apply: counts, events written, revision bumped, published versions untouched.
   - Model registration streams the chosen file into `register_stream` with HF provenance; a non-classification repo becomes catalog-only.
   - Role checks: a curator gets 403 on writes; a viewer can read the registry.
   - The HF client is faked everywhere; no network.
2. **Oracle suite:** `docker compose -f docker-compose.stack.yml --profile test run --rm test-oracle` runs the whole suite, registry tests included, against Oracle Free.
3. **Opt-in live test:** `OCEANFRAME_LIVE_TESTS=1` registers the two real NMFS-OSI repos. It checks that the shas equal `EXPECTED_*_REVISION` and that 5/5 sample patches resolve.
4. **End to end:**
   - `make up`, then `/healthz` shows `"app_db": "oracle"` and the startup log names Oracle.
   - Sign in as a lead and register both repos.
   - Create a project from the test split; it shows 56,864 patch-only items.
   - Link the dataset to the NOAA Pacific project with a dry run, then apply.
   - Validate the model in the worker and run an evaluation on a published version.
   - Confirm the matrix renders and the version manifest carries the HF provenance.
   - Bring up `docker-compose.t3-review.yml`; it asks for sign-in and shows the same Oracle projects.

## After approval
1. Copy this spec to `docs/superpowers/specs/2026-10-11-curation-oracle-hf-registry-design.md` and commit it on the current branch. The branch has many uncommitted changes, so commit only the spec file.
2. Use the writing-plans skill to turn this into a step-by-step implementation plan, then build with TDD.
3. Next brainstorm: **D**, easier curation and tab flow.
