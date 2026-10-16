# Oracle by Default + Hugging Face Registry: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- `make up` runs the curation app on Oracle, and the app always says which database it is on.
- Leads can register public Hugging Face datasets and models, pinned to a commit, and turn them into curation projects, split links, or evaluation classifiers.
- A registered model can be scored once on the val and test splits (about 113k patches) in minutes on the server. That run reproduces the published T3 baseline and feeds the interactive confusion matrix from stored predictions.

**Architecture:**
- **Part A** is configuration plus a small visibility layer in `appdb`: `backend_name()`, a strict mode, `/healthz`, and a header badge.
- **Part B** adds five pieces:
  - one table, `curation_hub_entries`;
  - a network-only client, `curation/huggingface.py`;
  - pure row logic, `curation/hub_rows.py`;
  - background operation bodies, `curation/hub_import.py`, run by the **existing** `CurationImportPool`;
  - a service, `curation/registry.py`, and a router, `routers/registry.py`.
- Model weights go through the existing `model_registry.register_stream`. Validation and evaluation stay in the isolated worker.

**Tech Stack:** FastAPI, SQLAlchemy 2 (Oracle via python-oracledb thin, SQLite for tests), `huggingface_hub`, `pyarrow`, vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-10-11-curation-oracle-hf-registry-design.md`

## Global Constraints

- **Python has no local interpreter on this machine.** Run tests in the container:
  `docker compose --profile test run --rm --build test sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q <targets>"`
  The Oracle suite runs as `docker compose -f docker-compose.stack.yml --profile test run --rm --build test-oracle`.
- **Schema rules.** Every string column uses `Str(n)` and every JSON column uses `JSONText` (from `appdb.base`). Index and constraint names stay ≤ 30 characters.
  - New tables come from `create_all`. **No changes to existing tables**, so no `MIGRATIONS` entry.
  - IN lists over 1000 values go through `curation.service.in_chunks`.
- **Pull only, public only.** Never send an HF token: `token=False` on every hub call. Gated or private repositories get a 422 with the message `"Only public Hugging Face repositories are supported"`.
- **Who may write.** Leads register and run hub operations (`Depends(require_lead)`). GET routes need only a signed-in user, as in `routers/curation.py`.
- **Weights.** The web process never unpickles weights. A `.pt` file only goes through `model_registry.register_stream` and the worker.
- **Images.** Every patch URI the app will serve must pass `curation.images.is_allowed(db, uri)`. URIs are stored as `gs://`, never `https://`.
- **Pinned shas.** The NOAA Pacific dataset's sha is `b47bff4ecdcdb026d31d1a154ff267227ff0f1b6`. The model's sha is `77e10ec10d33dfb7f0a9ebabc542b2008bb96637`. Both are already `EXPECTED_DATASET_REVISION` / `EXPECTED_MODEL_REVISION` in `curation/evaluation.py`.
- **Commits.** Commit only the files each task names; the branch carries unrelated uncommitted work. Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Re-registering.** Registering the same repo at the same sha twice must return the same entry, not raise an IntegrityError. A **different** sha is a new entry. *Pinned in Task 5.*
2. **Re-running a dataset import.** Running an import twice, or after a cancel, must not duplicate items: same `source_key`, rows counted as skipped. *Pinned in Task 6.*
3. **Splits from a CSV.** Linking with "adopt splits" must never overwrite an item whose split came from its CSV (`metadata_json.split_origin == "imported"`). Such items are counted as `kept_csv_split`. *Pinned in Task 7.*
4. **Image location outside the allowed buckets.** A custom root, or an `https://` column not on storage.googleapis.com, is rejected with 422 before anything is saved. *Pinned in Task 5.*
5. **Hub unreachable mid-import.** The operation must end as `failed` with a readable `error_message`, and the items already imported are kept. *Pinned in Task 6.*
6. **Patches missing during a 113k-image evaluation.** The rest are scored, the gap is counted per split and downloadable as CSV, and above 5% missing the run fails. *Pinned in Task 9.*
7. **A model whose classes don't match the project's labels** (e.g. a T1 project). It is refused before staging, and the UI says so when the model is chosen. *Pinned in Task 9.*

## Deliberate deviations from the spec (tell the user)

- **No lazy source-image root (spec Flow 2, step 4).** HF patches don't carry the source image's region root or extension, so a lazy lookup would have to probe three regional roots for each image. **Link mode** already gives these items their exact source image and point. Mapping patches back to source images is deferred to sub-project D, which covers patch editing.
- **`images.crop_from_source` is unchanged.** All three callers (`routers/curation.py:366`, `curation/versions.py:481`, `curation/evaluation_runner.py:108`) already skip items without coordinates.
- **`hub.js` is a classic script loaded after `curation.js`, not an ES module.** It uses `curation.js`'s top-level `request`, `byId`, `escapeHtml`, `canDo`, `state`, `loadProjects` and `loadEvaluationCatalog`. Top-level `const`/`function` declarations in classic scripts are shared, and an ES module could not see them without changing `curation.js`.

---

### Task 1: Show which database the app is on, and add a strict mode

**Files:**
- Modify: `appdb/engine.py` (add `backend_name()` and `describe()` after `db_url()`)
- Modify: `appdb/bootstrap.py:96-106` (`init_app_db`, strict-mode check)
- Modify: `main.py` (lifespan log line, `/healthz`, curation page context)
- Modify: `templates/curation.html:21` (badge)
- Test: `tests/test_appdb_backend.py` (new)

**Interfaces:**
- Produces:
  - `appdb.engine.backend_name() -> str`: `"oracle"`, `"sqlite"` or another SQLAlchemy backend name. Never opens a connection.
  - `appdb.engine.describe() -> str`, e.g. `"oracle (oceanframe-oracle:1521/FREEPDB1)"` or `"sqlite (/data/library/app.sqlite)"`.
  - `appdb.bootstrap.require_oracle() -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_appdb_backend.py
from __future__ import annotations

import pytest


def test_backend_name_reads_configuration_only(library, monkeypatch):
    from appdb import engine

    monkeypatch.delenv("OCEANFRAME_DB_URL", raising=False)
    monkeypatch.delenv("OCEANFRAME_DB_USER", raising=False)
    monkeypatch.delenv("OCEANFRAME_DB_DSN", raising=False)
    assert engine.backend_name() == "sqlite"
    assert engine.describe().startswith("sqlite (")

    monkeypatch.setenv("OCEANFRAME_DB_USER", "oceanframe")
    monkeypatch.setenv("OCEANFRAME_DB_PASSWORD", "Secret#1")
    monkeypatch.setenv("OCEANFRAME_DB_DSN", "oceanframe-oracle:1521/FREEPDB1")
    assert engine.backend_name() == "oracle"
    described = engine.describe()
    assert described == "oracle (oceanframe-oracle:1521/FREEPDB1)"
    assert "Secret" not in described


def test_strict_mode_refuses_sqlite(library, monkeypatch):
    from appdb import engine
    from appdb.bootstrap import init_app_db

    if engine.dialect_name() != "sqlite":
        pytest.skip("strict mode only refuses SQLite")
    monkeypatch.setenv("OCEANFRAME_REQUIRE_ORACLE", "on")
    with pytest.raises(RuntimeError, match="OCEANFRAME_REQUIRE_ORACLE"):
        init_app_db()


def test_healthz_names_the_backend(library):
    from fastapi.testclient import TestClient

    import main
    from appdb.engine import dialect_name

    body = TestClient(main.app).get("/healthz").json()
    assert body == {"status": "ok", "app_db": dialect_name()}


def test_sqlite_badge_on_curation_page(library):
    from fastapi.testclient import TestClient

    import main
    from appdb.engine import dialect_name

    html = TestClient(main.app).get("/curation").text
    assert ("Local SQLite database" in html) == (dialect_name() == "sqlite")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `docker compose --profile test run --rm --build test sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q tests/test_appdb_backend.py"`
Expected: FAIL with `AttributeError: module 'appdb.engine' has no attribute 'backend_name'`.

- [ ] **Step 3: Implement**

In `appdb/engine.py`, change the import line to `from sqlalchemy.engine import URL, Engine, make_url` and add these after `db_url()`:

```python
def _url() -> URL:
    url = db_url()
    return url if isinstance(url, URL) else make_url(url)


def backend_name() -> str:
    """'oracle' or 'sqlite' (or another SQLAlchemy backend), from configuration alone.

    Never connects: /healthz and the page header call it, and neither may wait
    on the database.
    """
    return _url().get_backend_name()


def describe() -> str:
    """The backend and where it is, for logs and the UI. Never includes a password."""
    url = _url()
    if url.get_backend_name() == "sqlite":
        return f"sqlite ({url.database})"
    service = url.query.get("service_name") or url.database or ""
    return f"{url.get_backend_name()} ({url.host}:{url.port or 1521}/{service})"
```

In `appdb/bootstrap.py`, add `import os` at the top and this above `init_app_db`:

```python
def require_oracle() -> bool:
    """OCEANFRAME_REQUIRE_ORACLE=on: refuse to start on anything but Oracle.

    The compose services set it, so a missing DSN fails loudly instead of
    quietly curating into a SQLite file nobody else can see.
    """
    return os.getenv("OCEANFRAME_REQUIRE_ORACLE", "").strip().lower() in {"1", "true", "yes", "on"}
```

In `init_app_db`, right after `engine = get_engine()`:

```python
    if require_oracle() and engine.dialect.name != "oracle":
        raise RuntimeError(
            f"OCEANFRAME_REQUIRE_ORACLE is on but the app database is {engine.dialect.name}: "
            "set OCEANFRAME_DB_USER and OCEANFRAME_DB_DSN (or OCEANFRAME_DB_URL)."
        )
```

In `main.py`:
- Add `import logging` and `from appdb.engine import backend_name, describe`.
- Add `logger = logging.getLogger("oceanframe")` after the imports.
- In `lifespan`, after `await run_in_threadpool(init_app_db)`, add `logger.info("App database: %s", describe())`.
- Change `healthz` to `return {"status": "ok", "app_db": backend_name()}` and add the sentence "Names the configured app database without connecting to it." to its docstring.
- Change `curation_page` to:

```python
@app.get("/curation")
async def curation_page(request: Request):
    """Classification curation: interactive confusion matrix, relabelling, review."""
    return templates.TemplateResponse(request=request, name="curation.html",
                                      context={"app_db": backend_name()})
```

In `templates/curation.html` line 21, after the `dataset-mode-badge` span (inside the same `div`), add:

```html
{% if app_db == "sqlite" %}<span id="app-db-badge" class="badge text-bg-warning" data-min-role="admin" title="Curation is being saved to a SQLite file on this server, not the shared Oracle database. Start the app with make up, or set OCEANFRAME_DB_USER and OCEANFRAME_DB_DSN.">Local SQLite database — not shared</span>{% endif %}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run the Step 2 command, plus `tests/test_curation.py -q`.
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add appdb/engine.py appdb/bootstrap.py main.py templates/curation.html tests/test_appdb_backend.py
git commit -m "The app says which database it is on, and can refuse SQLite

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Make `make up` and T3 review run on Oracle

**Files:**
- Modify: `docker-compose.yml` (add Oracle service, DB env anchor, network, `depends_on`)
- Modify: `docker-compose.t3-review.yml` (Oracle and sign-in)
- Modify: `docker-compose.stack.yml` (`OCEANFRAME_REQUIRE_ORACLE`; add `tests/test_registry.py` and `tests/test_appdb_backend.py` to `test-oracle`)
- Modify: `Makefile` (`env` target generates Oracle passwords)
- Modify: `.env.example` (document `OCEANFRAME_REQUIRE_ORACLE` and the `make up` Oracle)
- Modify: `README.md`, `docs/CURATION.md`

**Interfaces:**
- Consumes: `OCEANFRAME_REQUIRE_ORACLE` (Task 1). `docker-entrypoint.sh` (it already waits for the DB and runs `python -m appdb.bootstrap`).
- Produces:
  - The compose network is named `oceanframe-local`.
  - The Oracle container is `oceanframe-oracle`, reachable as `oceanframe-oracle:1521/FREEPDB1`.

- [ ] **Step 1: Add Oracle to `docker-compose.yml`**

After the `x-common-env` block, add:

```yaml
# The application database: users, sign-in and curation live in Oracle Free,
# the same as the multi-user stack. The library's derived catalog stays in
# SQLite on the library volume.
x-db-env: &db-env
  OCEANFRAME_DB_USER: "${APP_SCHEMA_NAME:-oceanframe}"
  OCEANFRAME_DB_PASSWORD: "${APP_SCHEMA_PASSWORD:?Run make env (or set APP_SCHEMA_PASSWORD in .env)}"
  OCEANFRAME_DB_DSN: "oceanframe-oracle:1521/${DB_SERVICE_NAME:-FREEPDB1}"
  OCEANFRAME_REQUIRE_ORACLE: "on"

x-db-wait: &db-wait
  depends_on:
    oceanframe-oracle:
      condition: service_healthy
  command: ["bash", "/app/docker-entrypoint.sh"]
```

Make these service changes:
- **`app`, `app-ml`, `app-gpu`:** change `environment: *common-env` to

```yaml
    environment:
      <<: [*common-env, *db-env]
```

  and add `<<: *db-wait` as a key of each service.
- **`dev`:** its `environment` already merges `*common-env`. Change that to `<<: [*common-env, *db-env]`, and add `depends_on` from `*db-wait`, **but not `command`**, because dev keeps its uvicorn reload command.
- **`test`, `test-live`, `quickstart`:** leave unchanged; they stay on SQLite.

Add the service (copied from `docker-compose.stack.yml:99-122`, minus the stack network):

```yaml
  # ── Oracle Free (the application database) ─────────────────────────────
  oceanframe-oracle:
    container_name: oceanframe-oracle
    image: "gvenzl/oracle-free:${ORACLE_IMAGE_TAG:-23-slim}"
    restart: unless-stopped
    environment:
      ORACLE_PASSWORD: "${ORACLE_PASSWORD:?Run make env (or set ORACLE_PASSWORD in .env)}"
      APP_USER: "${APP_SCHEMA_NAME:-oceanframe}"
      APP_USER_PASSWORD: "${APP_SCHEMA_PASSWORD:?Run make env (or set APP_SCHEMA_PASSWORD in .env)}"
    ports:
      - "127.0.0.1:${ORACLE_HOST_PORT:-1522}:1521"
    volumes:
      - "${OCEANFRAME_ORACLE_DATA:-oceanframe-oracle-data}:/opt/oracle/oradata"
    healthcheck:
      test: ["CMD", "healthcheck.sh"]
      interval: 30s
      timeout: 10s
      retries: 100
      start_period: 60s
```

At the bottom, add `oceanframe-oracle-data:` under `volumes:` and:

```yaml
networks:
  default:
    # Named so docker-compose.t3-review.yml can join it.
    name: oceanframe-local
```

Update the header comment: "`docker compose up -d --build` starts the app **and Oracle Free**".

- [ ] **Step 2: Move T3 review onto Oracle with sign-in**

In `docker-compose.t3-review.yml`:
- Replace `OCEANFRAME_AUTH: "off"` and the `OCEANFRAME_DB_URL` line with:

```yaml
      OCEANFRAME_AUTH: "on"
      OCEANFRAME_DB_USER: "${APP_SCHEMA_NAME:-oceanframe}"
      OCEANFRAME_DB_PASSWORD: "${APP_SCHEMA_PASSWORD:?Set APP_SCHEMA_PASSWORD in .env}"
      OCEANFRAME_DB_DSN: "oceanframe-oracle:1521/${DB_SERVICE_NAME:-FREEPDB1}"
      OCEANFRAME_REQUIRE_ORACLE: "on"
```

- Add `command: ["bash", "/app/docker-entrypoint.sh"]` to the `review` service.
- Add `networks: [oceanframe-local]` to the `review` service.
- At the bottom, add:

```yaml
networks:
  # The main compose file's network: start `make up` first so Oracle is running.
  oceanframe-local:
    external: true
```

- Add a header comment: "Reviews are saved to the main app's Oracle database and need a sign-in. Run `make up` first."

- [ ] **Step 3: Strict mode in the stack, and new tests in `test-oracle`**

In `docker-compose.stack.yml`'s `x-app-env`, under the DB block, add `OCEANFRAME_REQUIRE_ORACLE: "on"`.

In `test-oracle`'s command, replace the pytest file list with:
`tests/test_auth.py tests/test_curation.py tests/test_oasis_import.py tests/test_versions.py tests/test_evaluations.py tests/test_appdb_backend.py tests/test_registry.py`

- [ ] **Step 4: `make env` creates the Oracle passwords**

In the `Makefile` `env` target, before the final `@echo`, add:

```make
	@# Oracle needs passwords that start with a letter; generate any that are missing.
	@for key in ORACLE_PASSWORD APP_SCHEMA_PASSWORD; do \
	  grep -q "^$$key=." .env || { sed -i.bak "/^$$key=/d" .env; \
	    echo "$$key=Of$$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> .env; \
	    echo "Generated $$key in .env"; }; done
	@grep -q '^APP_SCHEMA_NAME=' .env || echo "APP_SCHEMA_NAME=oceanframe" >> .env
	@rm -f .env.bak
```

In `.env.example`, change the "Application database outside the stack" block to say: "`make up` runs Oracle Free (`oceanframe-oracle`); `make env` generates `ORACLE_PASSWORD` and `APP_SCHEMA_PASSWORD`. To use an existing Oracle instead, set `OCEANFRAME_DB_DSN=host:port/service` with `OCEANFRAME_DB_USER`/`OCEANFRAME_DB_PASSWORD`. `OCEANFRAME_REQUIRE_ORACLE=on` refuses to start on SQLite."

- [ ] **Step 5: Verify the compose files**

Run:
`docker compose config --quiet && docker compose -f docker-compose.t3-review.yml config --quiet && docker compose -f docker-compose.stack.yml config --quiet`
Expected: no output, exit 0. If `.env` lacks the passwords, run `make env` first.

Also run `docker compose -f docker-compose.yml -f docker-compose.evaluation.yml config --quiet`. The evaluation worker must still share the spool volume with an app that now waits on Oracle. If the overlay names services or volumes that changed, fix the overlay, not the worker.

Run:
`make up && sleep 90; docker compose logs app | grep "App database"; curl -s localhost:8080/healthz`
Expected: the log line `App database: oracle (oceanframe-oracle:1521/FREEPDB1)` and `{"status":"ok","app_db":"oracle"}`.

- [ ] **Step 6: Docs**

**README:** the quick start now says `make up` starts Oracle Free and the first start takes about a minute while Oracle initialises. Add a line on pointing at an external Oracle with `OCEANFRAME_DB_DSN`. The T3 review section now says it requires `make up` first and a sign-in.

**`docs/CURATION.md`:** under "Concurrency" or "Deployment", add one paragraph: curation always lives in the shared Oracle database when started through compose; `/healthz` and the admin badge show the backend.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml docker-compose.t3-review.yml docker-compose.stack.yml Makefile .env.example README.md docs/CURATION.md
git commit -m "make up and T3 review run on Oracle; review needs a sign-in

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Hugging Face client (the only code that touches the network)

**Files:**
- Create: `curation/huggingface.py`
- Modify: `curation/settings.py` (append getters)
- Modify: `requirements.txt` (add `huggingface_hub>=0.34,<2` and `pyarrow>=16`)
- Create: `tests/fixtures/hf/dataset_info.json`, `tests/fixtures/hf/model_info.json`
- Create: `tests/hub_fakes.py` (shared fakes for Tasks 3–8)
- Test: `tests/test_registry.py` (new; this task adds its first section)

**Interfaces:**
- Produces:
  - `HubRef(kind: str, repo_id: str, ref: str)`, a frozen dataclass.
  - `RepoInfo(kind, repo_id, sha, card: dict, files: list[dict], gated: bool | str = False, private: bool = False)`.
  - `parse_ref(text: str, kind: str | None = None) -> HubRef` (HTTPException 422/403).
  - `repo_info(ref: HubRef) -> RepoInfo`. HTTPException codes: 422 gated or private, 404 missing, 502 unreachable.
  - `download(kind: str, repo_id: str, sha: str, path: str) -> Path`, a local cached file. HTTPException 502/404.
  - `browse_url(kind: str, repo_id: str, sha: str) -> str`.
  - `PUBLIC_ONLY = "Only public Hugging Face repositories are supported"`.
  - `curation.settings`: `hf_allowed_orgs() -> set[str]`, `hf_endpoint() -> str`, `hub_cache_dir() -> Path`, `hub_max_bytes() -> int`.
  - `tests/hub_fakes.py`: `dataset_info()`, `model_info()`, `install_fake_hub(monkeypatch, infos: dict[str, RepoInfo], files: dict[str, Path]) -> None`.

- [ ] **Step 1: Write the fixtures and the fake**

`tests/fixtures/hf/dataset_info.json`, recorded from `https://huggingface.co/api/datasets/NMFS-OSI/noaa-pacific-benthic-cover-t3`:

```json
{
  "kind": "dataset",
  "repo_id": "NMFS-OSI/noaa-pacific-benthic-cover-t3",
  "sha": "b47bff4ecdcdb026d31d1a154ff267227ff0f1b6",
  "gated": false,
  "private": false,
  "card": {
    "task_categories": ["image-classification"],
    "size_categories": ["100K<n<1M"],
    "configs": [{"config_name": "default", "data_files": [
      {"split": "train", "path": "data/train.parquet"},
      {"split": "validation", "path": "data/val.parquet"},
      {"split": "test", "path": "data/test.parquet"}]}],
    "dataset_info": {
      "features": [
        {"name": "image_url", "dtype": "string"}, {"name": "gcs_uri", "dtype": "string"},
        {"name": "label", "dtype": "int64"}, {"name": "label_name", "dtype": "string"},
        {"name": "label_desc", "dtype": "string"}, {"name": "file_name", "dtype": "string"},
        {"name": "split", "dtype": "string"}],
      "splits": [
        {"name": "train", "num_examples": 265046},
        {"name": "validation", "num_examples": 56770},
        {"name": "test", "num_examples": 56864}]
    },
    "tags": ["coral", "noaa", "pacific"]
  },
  "files": [
    {"path": ".gitattributes", "size": 2400}, {"path": "README.md", "size": 9000},
    {"path": "class_list_full.csv", "size": 3000},
    {"path": "data/train.parquet", "size": 9000000}, {"path": "data/val.parquet", "size": 2000000},
    {"path": "data/test.parquet", "size": 2000000},
    {"path": "data_csv/test.csv", "size": 9000000}
  ]
}
```

`tests/fixtures/hf/model_info.json`:

```json
{
  "kind": "model",
  "repo_id": "NMFS-OSI/yolo11m-cls-noaa-pacific-benthic-cover-t3",
  "sha": "77e10ec10d33dfb7f0a9ebabc542b2008bb96637",
  "gated": false,
  "private": false,
  "card": {
    "pipeline_tag": "image-classification", "library_name": "ultralytics", "license": "agpl-3.0",
    "datasets": ["NMFS-OSI/noaa-pacific-benthic-cover-t3"], "base_model": ["Ultralytics/YOLO11"],
    "tags": ["yolo", "coral"]
  },
  "files": [
    {"path": "README.md", "size": 12000}, {"path": "confusion_matrix_normalized.png", "size": 400000},
    {"path": "weights/best.pt", "size": 41000000},
    {"path": "yolo11m_cls_noaa-pacific-benthic-t3.pt", "size": 41000000},
    {"path": "yolo11m_cls_noaa-pacific-benthic-t3.onnx", "size": 80000000}
  ]
}
```

`tests/hub_fakes.py`:

```python
"""A stand-in for curation.huggingface: recorded repo metadata, local files, no network."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hf"


def _load(name: str):
    from curation.huggingface import RepoInfo

    return RepoInfo(**json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def dataset_info():
    return _load("dataset_info.json")


def model_info():
    return _load("model_info.json")


def install_fake_hub(monkeypatch, infos: dict, files: dict[str, Path]) -> None:
    """infos: repo_id -> RepoInfo; files: 'repo_id/path' -> local file."""
    from curation import huggingface

    def repo_info(ref):
        info = infos.get(ref.repo_id)
        if info is None or info.kind != ref.kind:
            raise HTTPException(404, f"{ref.repo_id} was not found on Hugging Face, or it is private")
        if info.gated or info.private:
            raise HTTPException(422, huggingface.PUBLIC_ONLY)
        return info if ref.ref in {"main", info.sha} else replace(info, sha=ref.ref.ljust(40, "0")[:40])

    def download(kind, repo_id, sha, path):
        local = files.get(f"{repo_id}/{path}")
        if local is None:
            raise HTTPException(404, f"{path} is not in {repo_id}@{sha[:8]}")
        return local

    monkeypatch.setattr(huggingface, "repo_info", repo_info)
    monkeypatch.setattr(huggingface, "download", download)
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_registry.py
"""Hugging Face registry: datasets and models pinned to a commit, pulled into curation."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hub_fakes import dataset_info, install_fake_hub, model_info

DATASET = "NMFS-OSI/noaa-pacific-benthic-cover-t3"
MODEL = "NMFS-OSI/yolo11m-cls-noaa-pacific-benthic-cover-t3"
DATASET_SHA = "b47bff4ecdcdb026d31d1a154ff267227ff0f1b6"
MODEL_SHA = "77e10ec10d33dfb7f0a9ebabc542b2008bb96637"


# ── Parsing references ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,kind,expected", [
    (f"https://huggingface.co/datasets/{DATASET}", None, ("dataset", DATASET, "main")),
    (f"https://huggingface.co/{MODEL}", None, ("model", MODEL, "main")),
    (f"https://huggingface.co/{MODEL}/tree/{MODEL_SHA}", None, ("model", MODEL, MODEL_SHA)),
    (f"https://huggingface.co/datasets/{DATASET}/blob/main/README.md", None, ("dataset", DATASET, "main")),
    (f"{DATASET}", "dataset", ("dataset", DATASET, "main")),
    (f"{MODEL}@v1.0", "model", ("model", MODEL, "v1.0")),
])
def test_parse_ref_accepts_links_and_ids(text, kind, expected):
    from curation.huggingface import parse_ref

    ref = parse_ref(text, kind)
    assert (ref.kind, ref.repo_id, ref.ref) == expected


@pytest.mark.parametrize("text,kind", [
    ("https://example.com/NMFS-OSI/x", None),
    ("https://huggingface.co/spaces/owner/app", None),
    ("not a repo", "model"),
    (DATASET, None),  # a bare id needs a kind
])
def test_parse_ref_rejects_everything_else(text, kind):
    from curation.huggingface import parse_ref

    with pytest.raises(HTTPException) as caught:
        parse_ref(text, kind)
    assert caught.value.status_code == 422


def test_parse_ref_rejects_a_link_of_the_other_kind():
    from curation.huggingface import parse_ref

    with pytest.raises(HTTPException, match="dataset, not a model"):
        parse_ref(f"https://huggingface.co/datasets/{DATASET}", "model")


def test_allowed_orgs_limit_registration(monkeypatch):
    from curation.huggingface import parse_ref

    monkeypatch.setenv("CURATION_HF_ALLOWED_ORGS", "NMFS-OSI, noaa")
    assert parse_ref(DATASET, "dataset").repo_id == DATASET
    with pytest.raises(HTTPException) as caught:
        parse_ref("someone/else", "dataset")
    assert caught.value.status_code == 403


# ── Mapping hub metadata ──────────────────────────────────────────────────────

def test_repo_info_pins_the_sha_and_never_sends_a_token(monkeypatch):
    from curation import huggingface

    calls = {}

    class FakeApi:
        def __init__(self, endpoint=None, token=None):
            calls["token"] = token

        def repo_info(self, repo_id, repo_type, revision, files_metadata):
            calls["args"] = (repo_id, repo_type, revision, files_metadata)
            return SimpleNamespace(
                sha=MODEL_SHA, gated=False, private=False, pipeline_tag="image-classification",
                library_name="ultralytics",
                card_data=SimpleNamespace(to_dict=lambda: {"license": "agpl-3.0"}),
                siblings=[SimpleNamespace(rfilename="weights/best.pt", size=41)],
            )

    monkeypatch.setattr(huggingface, "_api_class", lambda: FakeApi)
    info = huggingface.repo_info(huggingface.HubRef("model", MODEL, "main"))
    assert calls["token"] is False
    assert calls["args"] == (MODEL, "model", "main", True)
    assert info.sha == MODEL_SHA
    assert info.card == {"license": "agpl-3.0", "pipeline_tag": "image-classification", "library_name": "ultralytics"}
    assert info.files == [{"path": "weights/best.pt", "size": 41}]


def test_repo_info_refuses_gated_repositories(monkeypatch):
    from curation import huggingface

    class FakeApi:
        def __init__(self, **_):
            pass

        def repo_info(self, *_args, **_kwargs):
            return SimpleNamespace(sha=MODEL_SHA, gated="manual", private=False, pipeline_tag=None,
                                   library_name=None, card_data=None, siblings=[])

    monkeypatch.setattr(huggingface, "_api_class", lambda: FakeApi)
    with pytest.raises(HTTPException) as caught:
        huggingface.repo_info(huggingface.HubRef("model", MODEL, "main"))
    assert caught.value.status_code == 422
    assert caught.value.detail == huggingface.PUBLIC_ONLY


def test_repo_info_reports_an_unreachable_hub(monkeypatch):
    from curation import huggingface

    class FakeApi:
        def __init__(self, **_):
            pass

        def repo_info(self, *_args, **_kwargs):
            raise ConnectionError("name resolution failed")

    monkeypatch.setattr(huggingface, "_api_class", lambda: FakeApi)
    with pytest.raises(HTTPException) as caught:
        huggingface.repo_info(huggingface.HubRef("model", MODEL, "main"))
    assert caught.value.status_code == 502
```

- [ ] **Step 3: Run them to verify they fail**

Run: `docker compose --profile test run --rm --build test sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q tests/test_registry.py"`
Expected: FAIL with `ModuleNotFoundError: No module named 'curation.huggingface'`.

- [ ] **Step 4: Implement**

Append to `curation/settings.py`:

```python
def hf_allowed_orgs() -> set[str]:
    """Optional CURATION_HF_ALLOWED_ORGS=NMFS-OSI,other: only these owners may be registered."""
    return {org.strip().lower() for org in os.getenv("CURATION_HF_ALLOWED_ORGS", "").split(",") if org.strip()}


def hf_endpoint() -> str:
    return os.getenv("CURATION_HF_ENDPOINT", "https://huggingface.co").rstrip("/")


def hub_cache_dir() -> Path:
    """Where pinned Hugging Face files (parquet, weights) are cached."""
    return Path(os.getenv("CURATION_HUB_CACHE", str(eval_dir() / "hub")))


def hub_max_bytes() -> int:
    """Largest single Hugging Face file the server will download (default 2 GiB)."""
    try:
        return max(1, int(os.getenv("CURATION_HUB_MAX_BYTES", str(2 * 1024 ** 3))))
    except ValueError:
        return 2 * 1024 ** 3
```

Add to `requirements.txt`:

```
# Hugging Face registry (curation/huggingface.py): metadata, pinned downloads, parquet rows.
huggingface_hub>=0.34,<2
pyarrow>=16
```

Create `curation/huggingface.py`:

```python
"""
The Hugging Face Hub, for the curation registry.

Everything here touches the network and nothing else in curation does, so
tests replace these functions (tests/hub_fakes.py). Public repositories only:
no token is ever sent, and gated or private repositories are refused rather
than half-working. Every read is pinned to the commit sha resolved at
registration, so a registry entry means the same bytes for ever.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from curation import settings

PUBLIC_ONLY = "Only public Hugging Face repositories are supported"
KINDS = ("dataset", "model")

_URL = re.compile(
    r"^https?://(?:www\.)?huggingface\.co/(?:(datasets)/)?([\w.-]+/[\w.-]+)"
    r"(?:/(?:tree|blob|resolve)/([^/?#]+)(?:/[^?#]*)?)?/?(?:[?#].*)?$"
)
_REPO_ID = re.compile(r"^[\w.-]+/[\w.-]+$")


@dataclass(frozen=True)
class HubRef:
    kind: str
    repo_id: str
    ref: str = "main"


@dataclass
class RepoInfo:
    kind: str
    repo_id: str
    sha: str
    card: dict = field(default_factory=dict)
    files: list[dict] = field(default_factory=list)
    gated: bool | str = False
    private: bool = False


def _check_owner(repo_id: str) -> None:
    allowed = settings.hf_allowed_orgs()
    if allowed and repo_id.split("/", 1)[0].lower() not in allowed:
        raise HTTPException(403, f"{repo_id.split('/', 1)[0]} is not in CURATION_HF_ALLOWED_ORGS")


def parse_ref(text: str, kind: str | None = None) -> HubRef:
    """A huggingface.co link, or ``owner/name[@ref]`` with a kind."""
    text = (text or "").strip()
    match = _URL.match(text)
    if match:
        url_kind = "dataset" if match.group(1) else "model"
        if match.group(2).split("/", 1)[0] in {"spaces", "datasets", "models"}:
            raise HTTPException(422, "Paste a link to a Hugging Face dataset or model")
        if kind and kind != url_kind:
            raise HTTPException(422, f"That link is a Hugging Face {url_kind}, not a {kind}")
        ref = HubRef(url_kind, match.group(2), match.group(3) or "main")
    else:
        repo_id, _, revision = text.partition("@")
        if kind not in KINDS or not _REPO_ID.match(repo_id):
            raise HTTPException(422, "Paste a huggingface.co link or an owner/name repository id")
        ref = HubRef(kind, repo_id, revision or "main")
    _check_owner(ref.repo_id)
    return ref


def _api_class():
    from huggingface_hub import HfApi  # noqa: PLC0415 — only needed when someone registers

    return HfApi


def repo_info(ref: HubRef) -> RepoInfo:
    from huggingface_hub.utils import (  # noqa: PLC0415
        GatedRepoError, RepositoryNotFoundError, RevisionNotFoundError,
    )

    api = _api_class()(endpoint=settings.hf_endpoint(), token=False)
    try:
        info = api.repo_info(ref.repo_id, repo_type=ref.kind, revision=ref.ref, files_metadata=True)
    except GatedRepoError as exc:
        raise HTTPException(422, PUBLIC_ONLY) from exc
    except RevisionNotFoundError as exc:
        raise HTTPException(404, f"Revision {ref.ref!r} was not found in {ref.repo_id}") from exc
    except RepositoryNotFoundError as exc:
        raise HTTPException(404, f"{ref.repo_id} was not found on Hugging Face, or it is private") from exc
    except Exception as exc:  # noqa: BLE001 — the hub's client raises several transport types
        raise HTTPException(502, f"Hugging Face did not answer: {exc}") from exc
    if getattr(info, "gated", False) or getattr(info, "private", False):
        raise HTTPException(422, PUBLIC_ONLY)
    card = info.card_data.to_dict() if getattr(info, "card_data", None) else {}
    for key in ("pipeline_tag", "library_name"):
        value = getattr(info, key, None) or card.get(key)
        if value:
            card[key] = value
    files = [{"path": sibling.rfilename, "size": getattr(sibling, "size", None)}
             for sibling in (getattr(info, "siblings", None) or [])]
    return RepoInfo(kind=ref.kind, repo_id=ref.repo_id, sha=info.sha, card=card, files=files)


def download(kind: str, repo_id: str, sha: str, path: str) -> Path:
    """One file at a pinned commit, into the hub cache; returns the local path."""
    from huggingface_hub import hf_hub_download  # noqa: PLC0415
    from huggingface_hub.utils import EntryNotFoundError  # noqa: PLC0415

    try:
        local = hf_hub_download(repo_id, path, repo_type=kind, revision=sha,
                                cache_dir=settings.hub_cache_dir(), token=False,
                                endpoint=settings.hf_endpoint())
    except EntryNotFoundError as exc:
        raise HTTPException(404, f"{path} is not in {repo_id}@{sha[:8]}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not download {path} from Hugging Face: {exc}") from exc
    return Path(local)


def browse_url(kind: str, repo_id: str, sha: str) -> str:
    prefix = "datasets/" if kind == "dataset" else ""
    return f"{settings.hf_endpoint()}/{prefix}{repo_id}/tree/{sha}"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run the Step 3 command.
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add curation/huggingface.py curation/settings.py requirements.txt tests/fixtures/hf tests/hub_fakes.py tests/test_registry.py
git commit -m "Hugging Face client for the curation registry: public repos, pinned to a commit

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Dataset row logic (splits, columns, image locations)

**Files:**
- Create: `curation/hub_rows.py`
- Test: `tests/test_registry.py` (append section)

**Interfaces:**
- Consumes: `RepoInfo` (Task 3); `curation.splits.normalize_split`; `curation.taxonomy.effective_label`.
- Produces:
  - `parquet_by_split(card: dict, files: list[dict]) -> dict[str, list[str]]`. Keys are `train|val|test|<other>`; values are parquet paths.
  - `features(card: dict) -> dict[str, str]`, mapping column name to dtype (`"image"` for embedded images).
  - `detect_columns(columns: dict[str, str]) -> dict[str, str | None]`. Keys are `image`, `label`, `file_name`, `split`; raises HTTPException 422.
  - `class_names(card: dict, label_column: str) -> list[str]`, for int ClassLabel columns.
  - `crop_uri(row: dict, split: str, label: str, file_name: str, column_map: dict, location: dict) -> str`.
    - `location = {"mode": "column"}`
    - or `{"mode": "env_root"}`
    - or `{"mode": "custom", "root": "gs://bucket/prefix"}`
  - `row_fields(row: dict, split: str, column_map: dict, names: list[str]) -> tuple[str, str, str]`, returning `(split, label, file_name)`.
  - `source_image_stem(label: str, file_name: str) -> str | None`.
  - `iter_rows(entry_like, splits: list[str] | None, columns: list[str]) -> Iterator[tuple[str, dict]]`. Reads the pinned parquet files in batches. `entry_like` has `.kind .repo_id .revision .card .files`.
  - `sample_rows(entry_like, n: int = 5) -> list[tuple[str, dict]]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_registry.py`)

```python
# ── Dataset rows ──────────────────────────────────────────────────────────────

def test_parquet_files_follow_the_card_configs():
    from curation.hub_rows import parquet_by_split

    info = dataset_info()
    assert parquet_by_split(info.card, info.files) == {
        "train": ["data/train.parquet"], "val": ["data/val.parquet"], "test": ["data/test.parquet"]}


def test_parquet_files_fall_back_to_file_names():
    from curation.hub_rows import parquet_by_split

    files = [{"path": "data/train-00000-of-00002.parquet"}, {"path": "data/train-00001-of-00002.parquet"},
             {"path": "data/validation-00000-of-00001.parquet"}, {"path": "README.md"}]
    assert parquet_by_split({}, files) == {
        "train": ["data/train-00000-of-00002.parquet", "data/train-00001-of-00002.parquet"],
        "val": ["data/validation-00000-of-00001.parquet"]}


def test_columns_are_detected_on_the_noaa_schema():
    from curation.hub_rows import detect_columns, features

    assert detect_columns(features(dataset_info().card)) == {
        "image": "gcs_uri", "label": "label_name", "file_name": "file_name", "split": "split"}


def test_embedded_images_are_out_of_scope():
    from curation.hub_rows import detect_columns

    with pytest.raises(HTTPException, match="embeds image bytes"):
        detect_columns({"image": "image", "label": "int64"})


def test_missing_label_column_names_what_was_found():
    from curation.hub_rows import detect_columns

    with pytest.raises(HTTPException, match="gcs_uri, notes"):
        detect_columns({"gcs_uri": "string", "notes": "string"})


ROW = {"image_url": "https://storage.googleapis.com/nmfs/t3/test/ACAS/ACAS_AGU-641_2025_A_09_9.png",
       "gcs_uri": "gs://nmfs/t3/test/ACAS/ACAS_AGU-641_2025_A_09_9.png", "label": 0,
       "label_name": "ACAS", "file_name": "ACAS_AGU-641_2025_A_09_9.png", "split": "test"}
MAP = {"image": "gcs_uri", "label": "label_name", "file_name": "file_name", "split": "split"}


def test_crop_uri_for_each_image_location(monkeypatch):
    from curation.hub_rows import crop_uri

    monkeypatch.setenv("CURATION_SPLIT_ROOT", "gs://nmfs/splits")
    args = (ROW, "test", "ACAS", ROW["file_name"], MAP)
    assert crop_uri(*args, {"mode": "column"}) == ROW["gcs_uri"]
    assert crop_uri(*args, {"mode": "env_root"}) == "gs://nmfs/splits/test/ACAS/ACAS_AGU-641_2025_A_09_9.png"
    assert crop_uri(*args, {"mode": "custom", "root": "gs://bucket/mine/"}) == \
        "gs://bucket/mine/test/ACAS/ACAS_AGU-641_2025_A_09_9.png"
    https_map = {**MAP, "image": "image_url"}
    assert crop_uri(ROW, "test", "ACAS", ROW["file_name"], https_map, {"mode": "column"}) == ROW["gcs_uri"]


def test_crop_uri_refuses_other_web_addresses():
    from curation.hub_rows import crop_uri

    row = {**ROW, "image_url": "https://example.com/a.png"}
    with pytest.raises(HTTPException) as caught:
        crop_uri(row, "test", "ACAS", "a.png", {**MAP, "image": "image_url"}, {"mode": "column"})
    assert caught.value.status_code == 422


def test_row_fields_use_class_names_for_integer_labels():
    from curation.hub_rows import row_fields

    split, label, file_name = row_fields({**ROW, "split": "validation"}, "test", {**MAP, "label": "label"}, ["ACAS"])
    assert (split, label, file_name) == ("val", "ACAS", ROW["file_name"])


def test_source_image_stem_follows_the_crop_naming():
    from curation.hub_rows import source_image_stem

    assert source_image_stem("ACAS", "ACAS_AGU-641_2025_A_09_9.png") == "AGU-641_2025_A_09"
    assert source_image_stem("ACAS", "other.png") is None


def _parquet(path, rows):
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


def test_iter_rows_reads_the_pinned_parquet(tmp_path, monkeypatch):
    from curation.hub_rows import iter_rows

    info = dataset_info()
    test_rows = [{**ROW, "file_name": f"ACAS_IMG-{n}_1.png"} for n in range(3)]
    install_fake_hub(monkeypatch, {DATASET: info}, {
        f"{DATASET}/data/test.parquet": _parquet(tmp_path / "test.parquet", test_rows)})
    entry = SimpleNamespace(kind="dataset", repo_id=DATASET, revision=DATASET_SHA, card=info.card, files=info.files)
    rows = list(iter_rows(entry, ["test"], ["gcs_uri", "label_name", "file_name", "split"]))
    assert [split for split, _ in rows] == ["test"] * 3
    assert rows[0][1]["file_name"] == "ACAS_IMG-0_1.png"
    assert "image_url" not in rows[0][1]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `... python -m pytest -q tests/test_registry.py -k "parquet or columns or embedded or label_column or crop_uri or row_fields or stem or iter_rows"`
Expected: FAIL with `ModuleNotFoundError: No module named 'curation.hub_rows'`.

- [ ] **Step 3: Implement `curation/hub_rows.py`**

```python
"""
Rows of a Hugging Face dataset, as curation sees them.

Pure logic plus one reader. Which parquet file holds which split, which
column is the image, label, file name and split, and where each patch lives.
v1 covers datasets whose rows *point at* images (gs:// or
storage.googleapis.com); datasets that embed image bytes are refused.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Iterator

from fastapi import HTTPException

from curation import huggingface, settings
from curation.splits import normalize_split

IMAGE_COLUMNS = ("gcs_uri", "image_uri", "image_url", "image_path", "file_path", "path")
LABEL_COLUMNS = ("label_name", "label", "class_name", "class", "category")
FILE_COLUMNS = ("file_name", "filename")
SPLIT_COLUMNS = ("split",)
_GCS_HTTPS = re.compile(r"^https://storage\.googleapis\.com/([^/]+)/(.+)$")
_SPLIT_IN_NAME = re.compile(r"(?:^|[/_-])(train|training|validation|valid|val|test)(?:[/_.-]|$)")


def _split(name: str) -> str:
    return normalize_split(name) or name


def parquet_by_split(card: dict, files: list[dict]) -> dict[str, list[str]]:
    """split -> parquet paths, from the card's configs, else from the file names."""
    present = {entry["path"] for entry in files}
    result: dict[str, list[str]] = {}
    for config in card.get("configs") or []:
        if config.get("config_name", "default") != "default":
            continue
        data_files = config.get("data_files") or []
        if isinstance(data_files, dict):
            data_files = [{"split": key, "path": value} for key, value in data_files.items()]
        for spec in data_files:
            paths = spec.get("path") if isinstance(spec, dict) else None
            for path in [paths] if isinstance(paths, str) else (paths or []):
                if path in present and path.endswith(".parquet"):
                    result.setdefault(_split(spec.get("split", "train")), []).append(path)
    if result:
        return result
    for path in sorted(present):
        if not path.endswith(".parquet"):
            continue
        match = _SPLIT_IN_NAME.search(path.lower())
        result.setdefault(_split(match.group(1)) if match else "train", []).append(path)
    return result


def features(card: dict) -> dict[str, str]:
    """column -> dtype from the card; 'image' for an embedded image, 'class_label' for ClassLabel."""
    info = card.get("dataset_info") or {}
    if isinstance(info, list):
        info = info[0] if info else {}
    columns = {}
    for feature in info.get("features") or []:
        dtype = feature.get("dtype")
        if dtype is None and "_type" in feature:
            dtype = feature["_type"]
        if isinstance(dtype, dict):
            dtype = "class_label" if "class_label" in dtype else "struct"
        if "class_label" in feature:
            dtype = "class_label"
        columns[feature["name"]] = str(dtype or "unknown").lower()
    return columns


def class_names(card: dict, label_column: str) -> list[str]:
    info = card.get("dataset_info") or {}
    if isinstance(info, list):
        info = info[0] if info else {}
    for feature in info.get("features") or []:
        if feature.get("name") == label_column:
            names = ((feature.get("class_label") or {}).get("names")
                     or ((feature.get("dtype") or {}) if isinstance(feature.get("dtype"), dict) else {})
                     .get("class_label", {}).get("names"))
            if isinstance(names, dict):
                return [names[key] for key in sorted(names, key=lambda k: int(k))]
            return list(names or [])
    return []


def _first(columns: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    return next((name for name in candidates if name in columns), None)


def detect_columns(columns: dict[str, str]) -> dict[str, str | None]:
    if "image" in columns.values() or columns.get("image") == "struct":
        raise HTTPException(422, "This dataset embeds image bytes; only datasets whose rows point at "
                                 "gs:// images are supported for now")
    mapping = {"image": _first(columns, IMAGE_COLUMNS), "label": _first(columns, LABEL_COLUMNS),
               "file_name": _first(columns, FILE_COLUMNS), "split": _first(columns, SPLIT_COLUMNS)}
    missing = [role for role in ("image", "label") if not mapping[role]]
    if missing:
        raise HTTPException(422, f"Could not find a {' or '.join(missing)} column; the dataset has "
                                 f"{', '.join(sorted(columns)) or 'no described columns'}")
    return mapping


def row_fields(row: dict, split: str, column_map: dict, names: list[str]) -> tuple[str, str, str]:
    """(split, label, file name) for one row."""
    value = row.get(column_map["label"])
    if isinstance(value, int) and not isinstance(value, bool) and names:
        value = names[value] if 0 <= value < len(names) else str(value)
    label = str(value if value is not None else "").strip().lstrip("*").strip()
    image = str(row.get(column_map["image"]) or "")
    file_name = str(row.get(column_map["file_name"]) or "") if column_map.get("file_name") else ""
    file_name = file_name or PurePosixPath(image).name
    row_split = normalize_split(row.get(column_map["split"])) if column_map.get("split") else None
    if not label or not file_name:
        raise ValueError("label and file name are required")
    return row_split or split, label, file_name


def crop_uri(row: dict, split: str, label: str, file_name: str, column_map: dict, location: dict) -> str:
    mode = location.get("mode", "column")
    if mode == "column":
        value = str(row.get(column_map["image"]) or "").strip()
        https = _GCS_HTTPS.match(value)
        if https:
            return f"gs://{https.group(1)}/{https.group(2)}"
        if not value.startswith("gs://"):
            raise HTTPException(422, f"Image column {column_map['image']!r} holds {value[:80]!r}; "
                                     "choose a gs:// root for the patches instead")
        return value
    root = settings.split_root() if mode == "env_root" else str(location.get("root") or "")
    if not root.startswith("gs://"):
        raise HTTPException(422, "The patch root must be a gs:// location")
    return f"{root.rstrip('/')}/{split}/{label}/{file_name}"


def source_image_stem(label: str, file_name: str) -> str | None:
    """'ACAS_AGU-641_2025_A_09_9.png' -> 'AGU-641_2025_A_09' (curation/importer.py names crops this way)."""
    stem = PurePosixPath(file_name).stem
    if not stem.startswith(f"{label}_"):
        return None
    rest, _, ordinal = stem[len(label) + 1:].rpartition("_")
    return rest if rest and ordinal.isdigit() else None


def iter_rows(entry, splits: list[str] | None, columns: list[str],
              batch_size: int = 2000) -> Iterator[tuple[str, dict]]:
    import pyarrow.parquet as pq  # noqa: PLC0415

    sizes = {item["path"]: item.get("size") or 0 for item in entry.files}
    for split, paths in parquet_by_split(entry.card, entry.files).items():
        if splits and split not in splits:
            continue
        for path in paths:
            if sizes.get(path, 0) > settings.hub_max_bytes():
                raise HTTPException(413, f"{path} exceeds CURATION_HUB_MAX_BYTES")
            local = huggingface.download(entry.kind, entry.repo_id, entry.revision, path)
            reader = pq.ParquetFile(local)
            # No columns named (a card without features): read them all.
            wanted = [name for name in dict.fromkeys(columns) if name in reader.schema_arrow.names] \
                if columns else reader.schema_arrow.names
            for batch in reader.iter_batches(batch_size=batch_size, columns=wanted):
                for row in batch.to_pylist():
                    yield split, row


def sample_rows(entry, n: int = 5) -> list[tuple[str, dict]]:
    rows = []
    for split_row in iter_rows(entry, None, list(features(entry.card)) or [], batch_size=n):
        rows.append(split_row)
        if len(rows) >= n:
            break
    return rows
```

- [ ] **Step 4: Run the tests to verify they pass**

Run the Step 2 command.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add curation/hub_rows.py tests/test_registry.py
git commit -m "Hugging Face dataset rows: splits, columns and patch locations

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Registry table, lookup and registration (datasets and models)

**Files:**
- Modify: `curation/models.py` (append `CurationHubEntry`)
- Create: `curation/registry.py`
- Create: `routers/registry.py`
- Modify: `main.py` (`include_router(registry.router, prefix="/api")`)
- Test: `tests/test_registry.py` (append section)

**Interfaces:**
- Consumes: Tasks 3–4; `model_registry.register_stream(db, stream, *, name, filename, trusted, user, provenance)`; `images.is_allowed(db, uri)`; `storage.read_bytes(uri)`; `evaluation.EXPECTED_MODEL_REVISION` / `EXPECTED_DATASET_REVISION`.
- Produces:
  - `CurationHubEntry` with columns `id kind repo_id revision requested_ref task runnable card files options classifier_id status error_message created_by_user_id created_by_name created_at`.
  - `registry.lookup(db, text: str, kind: str) -> dict`.
  - `registry.register_dataset(db, user, *, ref: str, column_map: dict | None, location: dict) -> tuple[CurationHubEntry, dict]`, returning `(entry, sample_report)`.
  - `registry.register_model(db, user, *, ref: str, weight_file: str | None, name: str | None, trusted: bool) -> CurationHubEntry`.
  - `registry.summary(db, entry) -> dict`.
  - `registry.get_entry_or_404(db, entry_id: int, kind: str) -> CurationHubEntry`.
  - HTTP routes under `/api/curation/hub`:
    - `GET /entries?kind=`
    - `POST /lookup` `{ref, kind}`
    - `POST /datasets` `{ref, column_map?, location}`
    - `POST /models` `{ref, weight_file?, name?, trusted}`
  - Fixture `hub_client` (in `tests/test_registry.py`): a TestClient with the fake hub, `CURATION_ALLOW_LOCAL_MODELS=on`, and `storage.read_bytes` stubbed.

- [ ] **Step 1: Write the failing tests** (append)

```python
# ── Registering ───────────────────────────────────────────────────────────────

P = "/api/curation"


@pytest.fixture
def hub_files(tmp_path):
    rows = {"train": [], "val": [], "test": []}
    for split in rows:
        for n, label in enumerate(["ACAS", "ACAS", "POCS"]):
            file_name = f"{label}_IMG-{split}-{n}_{n + 1}.png"
            rows[split].append({"image_url": f"https://storage.googleapis.com/nmfs/t3/{split}/{label}/{file_name}",
                                "gcs_uri": f"gs://nmfs/t3/{split}/{label}/{file_name}", "label": 0,
                                "label_name": label, "label_desc": label, "file_name": file_name, "split": split})
    weights = tmp_path / "best.pt"
    weights.write_bytes(b"hf-trusted-weights")
    return {
        f"{DATASET}/data/{name}.parquet": _parquet(tmp_path / f"{name}.parquet", rows[split])
        for name, split in (("train", "train"), ("val", "val"), ("test", "test"))
    } | {f"{MODEL}/weights/best.pt": weights, f"{MODEL}/yolo11m_cls_noaa-pacific-benthic-t3.pt": weights}


@pytest.fixture
def hub_client(library, monkeypatch, hub_files):
    from fastapi.testclient import TestClient

    import main
    from curation import storage

    monkeypatch.setenv("CURATION_IMAGE_BUCKETS", "nmfs,bucket")
    monkeypatch.setenv("CURATION_ALLOW_LOCAL_MODELS", "on")
    install_fake_hub(monkeypatch, {DATASET: dataset_info(), MODEL: model_info()}, hub_files)
    monkeypatch.setattr(storage, "read_bytes", lambda uri: b"png")
    return TestClient(main.app)


COLUMN = {"mode": "column"}


def test_lookup_previews_a_dataset(hub_client):
    response = hub_client.post(f"{P}/hub/lookup", json={"ref": f"https://huggingface.co/datasets/{DATASET}"})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["kind"] == "dataset" and preview["revision"] == DATASET_SHA
    assert preview["runnable"] is True
    assert preview["column_map"] == MAP
    assert preview["splits"] == {"train": 265046, "val": 56770, "test": 56864}
    assert preview["matches_t3_baseline"] is True
    assert preview["existing_entry_id"] is None


def test_lookup_lists_model_weights_and_flags_runnability(hub_client):
    preview = hub_client.post(f"{P}/hub/lookup", json={"ref": MODEL, "kind": "model"}).json()
    assert preview["weight_files"] == ["weights/best.pt", "yolo11m_cls_noaa-pacific-benthic-t3.pt"]
    assert preview["runnable"] is True
    assert preview["linked_datasets"] == [DATASET]


def test_register_dataset_pins_the_revision_and_is_idempotent(hub_client):
    body = {"ref": f"https://huggingface.co/datasets/{DATASET}", "location": COLUMN}
    first = hub_client.post(f"{P}/hub/datasets", json=body)
    assert first.status_code == 201, first.text
    entry = first.json()
    assert entry["revision"] == DATASET_SHA and entry["status"] == "registered"
    assert entry["sample"] == {"checked": 5, "found": 5}
    assert entry["url"] == f"https://huggingface.co/datasets/{DATASET}/tree/{DATASET_SHA}"
    again = hub_client.post(f"{P}/hub/datasets", json=body)
    assert again.status_code == 201 and again.json()["id"] == entry["id"]
    other = hub_client.post(f"{P}/hub/datasets", json={**body, "ref": f"{DATASET}@{'1' * 40}" , "kind": "dataset"})
    # the fake resolves any other ref to a different sha: a new revision is a new entry
    assert other.status_code == 201 and other.json()["id"] != entry["id"]
    listed = hub_client.get(f"{P}/hub/entries?kind=dataset").json()
    assert {item["id"] for item in listed} == {entry["id"], other.json()["id"]}


def test_register_dataset_refuses_a_bucket_outside_the_allow_list(hub_client):
    response = hub_client.post(f"{P}/hub/datasets", json={
        "ref": DATASET, "kind": "dataset", "location": {"mode": "custom", "root": "gs://someone-elses-bucket/x"}})
    assert response.status_code == 422
    assert "CURATION_IMAGE_BUCKETS" in response.json()["detail"]
    assert hub_client.get(f"{P}/hub/entries").json() == []


def test_register_dataset_fails_when_no_sample_patch_exists(hub_client, monkeypatch):
    from curation import storage

    def missing(uri):
        raise FileNotFoundError(uri)

    monkeypatch.setattr(storage, "read_bytes", missing)
    response = hub_client.post(f"{P}/hub/datasets", json={"ref": DATASET, "kind": "dataset", "location": COLUMN})
    assert response.status_code == 422
    assert "None of 5 sample patches" in response.json()["detail"]


def test_register_model_streams_the_chosen_weights(hub_client):
    response = hub_client.post(f"{P}/hub/models", json={
        "ref": f"https://huggingface.co/{MODEL}", "weight_file": "weights/best.pt", "trusted": True})
    assert response.status_code == 201, response.text
    entry = response.json()
    assert entry["runnable"] is True and entry["classifier_id"]
    models = hub_client.get(f"{P}/evaluation-models").json()["models"]
    model = next(m for m in models if m["id"] == entry["classifier_id"])
    assert model["name"] == "yolo11m-cls-noaa-pacific-benthic-cover-t3"
    from appdb import session_scope
    from curation.models import CurationClassifier

    with session_scope() as db:
        provenance = db.get(CurationClassifier, entry["classifier_id"]).provenance
    assert provenance == {"source": "huggingface", "repo_id": MODEL, "revision": MODEL_SHA,
                          "file": "weights/best.pt", "license": "agpl-3.0", "trusted": True}


def test_register_model_needs_trust_and_a_pt_file(hub_client):
    untrusted = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                        "weight_file": "weights/best.pt", "trusted": False})
    assert untrusted.status_code == 422
    onnx = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model", "trusted": True,
                                                   "weight_file": "yolo11m_cls_noaa-pacific-benthic-t3.onnx"})
    assert onnx.status_code == 422


def test_non_classifier_models_are_catalog_only(hub_client, monkeypatch, hub_files):
    from dataclasses import replace

    detector = replace(model_info(), repo_id="NMFS-OSI/detector",
                       card={"pipeline_tag": "object-detection", "library_name": "ultralytics"})
    install_fake_hub(monkeypatch, {DATASET: dataset_info(), MODEL: model_info(), "NMFS-OSI/detector": detector},
                     hub_files)
    response = hub_client.post(f"{P}/hub/models", json={"ref": "NMFS-OSI/detector", "kind": "model"})
    assert response.status_code == 201, response.text
    entry = response.json()
    assert entry["runnable"] is False and entry["classifier_id"] is None
    assert entry["task"] == "object-detection"
    assert "classification" in entry["not_runnable_reason"]


def test_gated_repositories_are_refused(hub_client, monkeypatch, hub_files):
    from dataclasses import replace

    install_fake_hub(monkeypatch, {DATASET: replace(dataset_info(), gated="auto")}, hub_files)
    response = hub_client.post(f"{P}/hub/lookup", json={"ref": DATASET, "kind": "dataset"})
    assert response.status_code == 422
    assert response.json()["detail"] == "Only public Hugging Face repositories are supported"


def test_hub_roles(team, monkeypatch, hub_files):
    install_fake_hub(monkeypatch, {DATASET: dataset_info(), MODEL: model_info()}, hub_files)
    body = {"ref": DATASET, "kind": "dataset", "location": COLUMN}
    assert team["ana"].post(f"{P}/hub/lookup", json={"ref": DATASET, "kind": "dataset"}).status_code == 403
    assert team["ana"].post(f"{P}/hub/datasets", json=body).status_code == 403
    assert team["vic"].get(f"{P}/hub/entries").status_code == 200
```

To make the `team` fixture (login on: an admin, curators `ana`/`ben`, viewer `vic`) visible, add this import at the top of `tests/test_registry.py`:
`from test_curation import _test_buckets, team  # noqa: F401 — fixtures`.

- [ ] **Step 2: Run them to verify they fail**

Run: `... python -m pytest -q tests/test_registry.py -k "lookup or register or catalog or gated or roles"`
Expected: FAIL with 404 on `/api/curation/hub/...`.

- [ ] **Step 3: Implement the table**

Append to `curation/models.py`:

```python
class CurationHubEntry(Base):
    """A Hugging Face dataset or model, pinned to the commit it was registered at."""

    __tablename__ = "curation_hub_entries"
    __table_args__ = (
        UniqueConstraint("kind", "repo_id", "revision", name="uq_cur_hub_entry"),
        Index("ix_cur_hub_kind", "kind", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Str(10), nullable=False)
    repo_id: Mapped[str] = mapped_column(Str(200), nullable=False)
    revision: Mapped[str] = mapped_column(Str(40), nullable=False)
    requested_ref: Mapped[str] = mapped_column(Str(100), nullable=False, default="main")
    task: Mapped[str | None] = mapped_column(Str(40))
    runnable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    card: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    files: Mapped[list] = mapped_column(JSONText, nullable=False, default=list)
    options: Mapped[dict] = mapped_column(JSONText, nullable=False, default=dict)
    classifier_id: Mapped[str | None] = mapped_column(ForeignKey("curation_classifiers.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(Str(20), nullable=False, default="registered")
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("of_users.id", ondelete="SET NULL"))
    created_by_name: Mapped[str | None] = mapped_column(Str(NAME_LEN))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
```

(`UniqueConstraint`, `Index`, `Boolean`, `Text`, `Identity`, `ForeignKey` are already imported at the top of `models.py`; check the import block and add any that are missing.)

- [ ] **Step 4: Implement `curation/registry.py`**

```python
"""
The Hugging Face registry: look a repository up, register it at a pinned
commit, and hand datasets to the import pool and model weights to the
existing classifier registry (curation/model_registry.py).
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth.service import CurrentUser
from curation import evaluation, huggingface, hub_rows, images, model_registry, settings, storage
from curation.models import CurationHubEntry, CurationProject

RUNNABLE_MODEL = {"pipeline_tag": "image-classification", "library_name": "ultralytics"}
SAMPLE = 5


def _baseline(sha: str) -> bool:
    return sha in {evaluation.EXPECTED_MODEL_REVISION, evaluation.EXPECTED_DATASET_REVISION}


def _task(info: huggingface.RepoInfo) -> str | None:
    if info.kind == "model":
        return info.card.get("pipeline_tag")
    categories = info.card.get("task_categories") or []
    return categories[0] if categories else None


def _weight_files(info: huggingface.RepoInfo) -> list[str]:
    return sorted(entry["path"] for entry in info.files if entry["path"].lower().endswith(".pt"))


def _model_runnable(info: huggingface.RepoInfo) -> tuple[bool, str | None]:
    if info.card.get("library_name") != RUNNABLE_MODEL["library_name"]:
        return False, "Only Ultralytics models can be evaluated here for now"
    if info.card.get("pipeline_tag") != RUNNABLE_MODEL["pipeline_tag"]:
        return False, (f"{info.card.get('pipeline_tag') or 'This task'} is catalogued only; "
                       "evaluation supports image classification")
    if not _weight_files(info):
        return False, "The repository has no .pt checkpoint"
    return True, None


def _dataset_shape(info: huggingface.RepoInfo) -> tuple[dict, dict, dict, str | None]:
    """(parquet by split, columns, detected column map, reason it cannot be used)."""
    parquet = hub_rows.parquet_by_split(info.card, info.files)
    columns = hub_rows.features(info.card)
    if not parquet:
        return parquet, columns, {}, "The dataset has no parquet files"
    try:
        return parquet, columns, hub_rows.detect_columns(columns), None
    except HTTPException as exc:
        return parquet, columns, {}, str(exc.detail)


def _split_counts(card: dict) -> dict[str, int]:
    info = card.get("dataset_info") or {}
    if isinstance(info, list):
        info = info[0] if info else {}
    counts = {}
    for split in info.get("splits") or []:
        name = hub_rows._split(split.get("name", ""))  # noqa: SLF001 — same package
        counts[name] = counts.get(name, 0) + int(split.get("num_examples") or 0)
    return counts


def _existing(db: Session, info: huggingface.RepoInfo) -> CurationHubEntry | None:
    return db.scalar(select(CurationHubEntry).where(
        CurationHubEntry.kind == info.kind, CurationHubEntry.repo_id == info.repo_id,
        CurationHubEntry.revision == info.sha))


def lookup(db: Session, text: str, kind: str | None) -> dict:
    info = huggingface.repo_info(huggingface.parse_ref(text, kind))
    existing = _existing(db, info)
    preview = {
        "kind": info.kind, "repo_id": info.repo_id, "revision": info.sha, "task": _task(info),
        "license": info.card.get("license"), "tags": info.card.get("tags") or [],
        "url": huggingface.browse_url(info.kind, info.repo_id, info.sha),
        "matches_t3_baseline": _baseline(info.sha),
        "existing_entry_id": existing.id if existing else None,
    }
    if info.kind == "model":
        runnable, reason = _model_runnable(info)
        return {**preview, "runnable": runnable, "not_runnable_reason": reason,
                "library_name": info.card.get("library_name"), "weight_files": _weight_files(info),
                "linked_datasets": list(info.card.get("datasets") or []),
                "images": [entry["path"] for entry in info.files if entry["path"].lower().endswith(".png")]}
    parquet, columns, column_map, reason = _dataset_shape(info)
    return {**preview, "runnable": reason is None, "not_runnable_reason": reason,
            "columns": columns, "column_map": column_map, "parquet": parquet,
            "splits": _split_counts(info.card) or {split: None for split in parquet}}


def _create(db: Session, info: huggingface.RepoInfo, requested: str, user: CurrentUser, **fields) -> CurationHubEntry:
    entry = CurationHubEntry(kind=info.kind, repo_id=info.repo_id, revision=info.sha, requested_ref=requested[:100],
                             task=_task(info), card=info.card, files=info.files,
                             created_by_user_id=user.id, created_by_name=user.label, **fields)
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        # Two leads registered the same commit at once: the other one won.
        db.rollback()
        entry = _existing(db, info)
    db.refresh(entry)
    return entry


def _check_location(db: Session, info: huggingface.RepoInfo, column_map: dict, location: dict) -> dict:
    if location.get("mode") not in {"column", "env_root", "custom"}:
        raise HTTPException(422, "Choose where the patches are: the dataset's column, the configured root, or a gs:// root")
    entry_like = SimpleNamespace(kind=info.kind, repo_id=info.repo_id, revision=info.sha,
                                 card=info.card, files=info.files)
    names = hub_rows.class_names(info.card, column_map["label"])
    found, checked = 0, 0
    for split, row in hub_rows.sample_rows(entry_like, SAMPLE):
        split, label, file_name = hub_rows.row_fields(row, split, column_map, names)
        uri = hub_rows.crop_uri(row, split, label, file_name, column_map, location)
        images.require_allowed(db, uri)
        checked += 1
        try:
            storage.read_bytes(uri)
            found += 1
        except (FileNotFoundError, PermissionError):
            pass
    if checked and not found:
        raise HTTPException(422, f"None of {checked} sample patches were found where this location points")
    return {"checked": checked, "found": found}


def register_dataset(db: Session, user: CurrentUser, *, ref: str, kind: str | None = "dataset",
                     column_map: dict | None, location: dict) -> tuple[CurationHubEntry, dict]:
    info = huggingface.repo_info(huggingface.parse_ref(ref, kind or "dataset"))
    if info.kind != "dataset":
        raise HTTPException(422, "That is a model; register it as a model")
    parquet, columns, detected, reason = _dataset_shape(info)
    if reason and not column_map:
        raise HTTPException(422, reason)
    column_map = {**detected, **(column_map or {})}
    unknown = [name for name in column_map.values() if name and columns and name not in columns]
    if unknown:
        raise HTTPException(422, f"Unknown column(s): {', '.join(unknown)}")
    existing = _existing(db, info)
    if existing:
        return existing, existing.options.get("sample", {})
    sample = _check_location(db, info, column_map, location)
    entry = _create(db, info, ref, user, runnable=True,
                    options={"column_map": column_map, "location": location, "sample": sample})
    return entry, sample


def register_model(db: Session, user: CurrentUser, *, ref: str, kind: str | None = "model",
                   weight_file: str | None, name: str | None, trusted: bool) -> CurationHubEntry:
    info = huggingface.repo_info(huggingface.parse_ref(ref, kind or "model"))
    if info.kind != "model":
        raise HTTPException(422, "That is a dataset; register it as a dataset")
    existing = _existing(db, info)
    if existing:
        chosen = existing.options.get("weight_file")
        if weight_file and chosen and weight_file != chosen:
            raise HTTPException(409, f"This commit is already registered with {chosen}")
        return existing
    runnable, reason = _model_runnable(info)
    if not runnable:
        return _create(db, info, ref, user, runnable=False, options={"not_runnable_reason": reason})
    files = {entry["path"]: entry.get("size") or 0 for entry in info.files}
    if not weight_file or weight_file not in files or not weight_file.lower().endswith(".pt"):
        raise HTTPException(422, f"Choose one of the repository's .pt files: {', '.join(_weight_files(info))}")
    if not trusted:
        raise HTTPException(422, "Confirm that these classifier weights come from a trusted source")
    if files[weight_file] > settings.model_upload_limit():
        raise HTTPException(413, "Classifier exceeds CURATION_MODEL_MAX_BYTES")
    local = huggingface.download("model", info.repo_id, info.sha, weight_file)
    with local.open("rb") as stream:
        classifier = model_registry.register_stream(
            db, stream, name=(name or info.repo_id.split("/", 1)[1])[:200], filename=weight_file,
            trusted=trusted, user=user,
            provenance={"source": "huggingface", "repo_id": info.repo_id, "revision": info.sha,
                        "file": weight_file, "license": info.card.get("license")})
    return _create(db, info, ref, user, runnable=True, classifier_id=classifier.id,
                   options={"weight_file": weight_file})


def get_entry_or_404(db: Session, entry_id: int, kind: str | None = None) -> CurationHubEntry:
    entry = db.get(CurationHubEntry, entry_id)
    if entry is None or (kind and entry.kind != kind):
        raise HTTPException(404, "Registry entry not found")
    return entry


def summary(db: Session, entry: CurationHubEntry, projects: list[CurationProject] | None = None) -> dict:
    projects = projects if projects is not None else db.scalars(select(CurationProject)).all()
    linked = [{"id": project.id, "name": project.name} for project in projects
              if (project.source_config or {}).get("hub", {}).get("entry_id") == entry.id
              or any(link.get("entry_id") == entry.id for link in (project.source_config or {}).get("hub_links", []))]
    return {
        "id": entry.id, "kind": entry.kind, "repo_id": entry.repo_id, "revision": entry.revision,
        "short_revision": entry.revision[:8], "requested_ref": entry.requested_ref,
        "url": huggingface.browse_url(entry.kind, entry.repo_id, entry.revision),
        "task": entry.task, "runnable": entry.runnable,
        "not_runnable_reason": entry.options.get("not_runnable_reason"),
        "license": entry.card.get("license"), "splits": _split_counts(entry.card) if entry.kind == "dataset" else None,
        "options": {key: value for key, value in entry.options.items() if key != "sample"},
        "sample": entry.options.get("sample"), "classifier_id": entry.classifier_id,
        "status": entry.status, "error_message": entry.error_message,
        "created_by_name": entry.created_by_name, "created_at": entry.created_at,
        "matches_t3_baseline": _baseline(entry.revision), "projects": linked,
    }
```

- [ ] **Step 5: Implement `routers/registry.py`, and register it**

```python
"""
Hugging Face registry API — /api/curation/hub/*

Everyone signed in can read the registry; leads look repositories up,
register them and start what follows from them.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from appdb.engine import get_db
from auth.deps import require_lead
from auth.service import CurrentUser
from curation import registry
from curation.models import CurationHubEntry, CurationProject

router = APIRouter(prefix="/curation/hub", tags=["curation-hub"])

KIND = Field(default=None, pattern="^(dataset|model)$")


class LookupIn(BaseModel):
    ref: str = Field(min_length=3, max_length=500)
    kind: Optional[str] = KIND


class Location(BaseModel):
    mode: str = Field(pattern="^(column|env_root|custom)$")
    root: Optional[str] = Field(default=None, max_length=900)


class RegisterDatasetIn(LookupIn):
    column_map: Optional[dict[str, Optional[str]]] = None
    location: Location = Field(default_factory=lambda: Location(mode="column"))


class RegisterModelIn(LookupIn):
    weight_file: Optional[str] = Field(default=None, max_length=500)
    name: Optional[str] = Field(default=None, max_length=200)
    trusted: bool = False


@router.get("/entries")
def list_entries(kind: Optional[str] = Query(None, pattern="^(dataset|model)$"), db: Session = Depends(get_db)):
    query = select(CurationHubEntry).order_by(CurationHubEntry.created_at.desc())
    if kind:
        query = query.where(CurationHubEntry.kind == kind)
    projects = db.scalars(select(CurationProject)).all()
    return [registry.summary(db, entry, projects) for entry in db.scalars(query)]


@router.post("/lookup", dependencies=[Depends(require_lead)])
def lookup(body: LookupIn, db: Session = Depends(get_db)):
    return registry.lookup(db, body.ref, body.kind)


@router.post("/datasets", status_code=201)
def register_dataset(body: RegisterDatasetIn, user: CurrentUser = Depends(require_lead), db: Session = Depends(get_db)):
    entry, sample = registry.register_dataset(db, user, ref=body.ref, kind=body.kind or "dataset",
                                              column_map=body.column_map, location=body.location.model_dump())
    return {**registry.summary(db, entry), "sample": sample}


@router.post("/models", status_code=201)
def register_model(body: RegisterModelIn, user: CurrentUser = Depends(require_lead), db: Session = Depends(get_db)):
    entry = registry.register_model(db, user, ref=body.ref, kind=body.kind or "model",
                                    weight_file=body.weight_file, name=body.name, trusted=body.trusted)
    return registry.summary(db, entry)
```

Register it in `main.py`: change `from routers import auth, curation, export, library, stream, upload` to also import `registry`, and add `app.include_router(registry.router, prefix="/api")` after the curation router.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `... python -m pytest -q tests/test_registry.py tests/test_evaluations.py`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add curation/models.py curation/registry.py routers/registry.py main.py tests/test_registry.py
git commit -m "Leads register Hugging Face datasets and models, pinned to a commit

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Create a patch-only project from a registered dataset

**Files:**
- Create: `curation/hub_import.py`
- Modify: `curation/importer.py` (`run_import`: dispatch on `source["kind"]`)
- Modify: `curation/registry.py` (add `create_project_from_dataset`, `label_report`)
- Modify: `routers/registry.py` (two routes)
- Test: `tests/test_registry.py` (append section)

**Interfaces:**
- Consumes: Tasks 4–5. `importer._insert_batch(db, records) -> (imported, skipped)`. `service.create_project(db, CurationProjectCreate, user)`. `taxonomy.effective_label`, `taxonomy.project_code`, `taxonomy.available`.
- Produces:
  - `hub_import.record_for(row, split, *, project_id, entry, dataset_mode, names) -> dict`.
  - `hub_import.import_rows(db, operation, source: dict, cancel_event, dataset_mode: str) -> None`.
  - Import source shape: `{"kind": "hub_dataset", "name": str, "entry_id": int, "splits": list[str]}`.
  - `registry.create_project_from_dataset(db, user, entry, *, name, description, dataset_mode, splits) -> tuple[CurationProject, CurationImportOperation]`.
  - `registry.label_report(entry, dataset_mode: str) -> list[dict]`: `[{label, count, effective, known}]`.
  - Routes:
    - `POST /api/curation/hub/datasets/{entry_id}/projects` → 202 `{project, operation}`.
    - `GET /api/curation/hub/datasets/{entry_id}/labels?dataset_mode=T3`.

- [ ] **Step 1: Write the failing tests** (append)

```python
# ── Dataset → new project ─────────────────────────────────────────────────────

def _registered_dataset(hub_client, location=COLUMN):
    response = hub_client.post(f"{P}/hub/datasets", json={"ref": DATASET, "kind": "dataset", "location": location})
    assert response.status_code == 201, response.text
    return response.json()


def _drain(operation_id):
    import threading

    from curation import importer

    importer.run_import(operation_id, threading.Event())


def test_dataset_becomes_a_patch_only_project(hub_client):
    entry = _registered_dataset(hub_client)
    response = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                               json={"name": "T3 from HF", "dataset_mode": "T3", "splits": ["test", "val"]})
    assert response.status_code == 202, response.text
    project, operation = response.json()["project"], response.json()["operation"]
    assert project["source_kind"] == "huggingface"
    assert project["source_config"]["hub"] == {"entry_id": entry["id"], "repo_id": DATASET, "revision": DATASET_SHA}
    _drain(operation["id"])

    done = hub_client.get(f"{P}/projects/{project['id']}/imports/{operation['id']}").json()
    assert done["status"] == "completed", done
    assert (done["imported_rows"], done["skipped_rows"], done["rejected_rows"]) == (6, 0, 0)
    items = hub_client.get(f"{P}/projects/{project['id']}/items?limit=50").json()["items"]
    assert {item["split"] for item in items} == {"test", "val"}
    first = next(item for item in items if item["file_name"] == "ACAS_IMG-test-0_1.png")
    assert first["crop_uri"] == "gs://nmfs/t3/test/ACAS/ACAS_IMG-test-0_1.png"
    assert first["source_image_uri"] == first["crop_uri"]
    assert first["row_px"] is None and first["column_px"] is None
    assert first["source_label"] == "ACAS"


def test_reimporting_a_dataset_skips_what_is_there(hub_client):
    entry = _registered_dataset(hub_client)
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": "Twice", "dataset_mode": "T3", "splits": ["test"]}).json()
    _drain(created["operation"]["id"])
    from appdb import session_scope
    from curation.models import CurationImportOperation

    with session_scope() as db:
        again = CurationImportOperation(project_id=created["project"]["id"],
                                        sources=created["operation"]["sources"], batch_size=2000)
        db.add(again)
        db.flush()
        again_id = again.id
    _drain(again_id)
    second = hub_client.get(f"{P}/projects/{created['project']['id']}/imports/{again_id}").json()
    assert (second["imported_rows"], second["skipped_rows"]) == (0, 3)


def test_hub_failure_mid_import_keeps_what_landed(hub_client, monkeypatch):
    from fastapi import HTTPException as Hx

    from curation import huggingface

    entry = _registered_dataset(hub_client)
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": "Flaky", "dataset_mode": "T3", "splits": ["train", "test"]}).json()
    real = huggingface.download

    def flaky(kind, repo_id, sha, path):
        if path.endswith("test.parquet"):
            raise Hx(502, "Could not download data/test.parquet from Hugging Face: timed out")
        return real(kind, repo_id, sha, path)

    monkeypatch.setattr(huggingface, "download", flaky)
    _drain(created["operation"]["id"])
    failed = hub_client.get(f"{P}/projects/{created['project']['id']}/imports/{created['operation']['id']}").json()
    assert failed["status"] == "failed"
    assert "timed out" in failed["error_message"]
    # Splits are read in the card's order (train, val, test): train landed before test failed.
    assert failed["imported_rows"] == 3


def test_label_report_flags_labels_outside_the_tier(hub_client, monkeypatch):
    from curation import taxonomy

    monkeypatch.setattr(taxonomy, "available", lambda: True)
    monkeypatch.setattr(taxonomy, "project_code", lambda code, mode: code if code == "ACAS" else None)
    entry = _registered_dataset(hub_client)
    report = hub_client.get(f"{P}/hub/datasets/{entry['id']}/labels?dataset_mode=T3").json()
    assert report == [{"label": "ACAS", "count": 6, "effective": "ACAS", "known": True},
                      {"label": "POCS", "count": 3, "effective": "POCS", "known": False}]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `... python -m pytest -q tests/test_registry.py -k "project or reimporting or mid_import or label_report"`
Expected: FAIL with 404 on `/hub/datasets/{id}/projects`.

- [ ] **Step 3: Implement `curation/hub_import.py`**

```python
"""
Background bodies for Hugging Face dataset operations.

They run on the existing curation import worker (curation/importer.py), so
they get its queue, progress, cancel and restart handling. ``import_rows``
creates patch-only items; ``link_rows`` (Task 7) attaches a dataset to items
that already exist.
"""
from __future__ import annotations

import threading

from sqlalchemy.orm import Session

from appdb.base import utcnow
from curation import hub_rows, taxonomy
from curation.models import URI_LEN, CurationHubEntry, CurationImportOperation


def _entry(db: Session, source: dict) -> CurationHubEntry:
    entry = db.get(CurationHubEntry, source["entry_id"])
    if entry is None:
        raise ValueError("The registry entry for this import was deleted")
    return entry


def record_for(row: dict, split: str, *, project_id: str, entry: CurationHubEntry, dataset_mode: str,
               names: list[str]) -> dict:
    column_map, location = entry.options["column_map"], entry.options["location"]
    split, label, file_name = hub_rows.row_fields(row, split, column_map, names)
    uri = hub_rows.crop_uri(row, split, label, file_name, column_map, location)
    if len(uri) > URI_LEN:
        raise ValueError("Image path too long")
    now = utcnow()
    return {
        "project_id": project_id, "source_key": f"hf:{split}:{label}:{file_name}"[:400],
        "source_image_uri": uri, "crop_uri": uri, "file_name": file_name[:300],
        "source_label": label, "current_label": taxonomy.effective_label(label, dataset_mode),
        "included": True, "review_state": "unreviewed", "row_px": None, "column_px": None, "crop_size": 224,
        "region": None, "island": None, "site": None, "survey_year": None, "split": split, "annotator": None,
        "model_label": None, "model_confidence": None, "top_predictions": [], "quality_flags": [],
        "metadata_json": {
            "split_origin": "hub", "patch_only": True,
            "source_image_stem": hub_rows.source_image_stem(label, file_name),
            "hub": {"repo_id": entry.repo_id, "revision": entry.revision, "split": split},
        },
        "revision": 1, "created_at": now, "updated_at": now,
    }


def import_rows(db: Session, operation: CurationImportOperation, source: dict,
                cancel_event: threading.Event, dataset_mode: str) -> None:
    from curation.importer import _insert_batch  # noqa: PLC0415 — importer imports this module

    entry = _entry(db, source)
    column_map = entry.options["column_map"]
    names = hub_rows.class_names(entry.card, column_map["label"])
    batch: list[dict] = []

    def flush() -> None:
        imported, skipped = _insert_batch(db, batch)
        operation.imported_rows += imported
        operation.skipped_rows += skipped
        batch.clear()
        db.add(operation)
        db.commit()

    columns = [name for name in column_map.values() if name]
    for split, row in hub_rows.iter_rows(entry, source.get("splits") or None, columns):
        if cancel_event.is_set():
            break
        operation.processed_rows += 1
        try:
            batch.append(record_for(row, split, project_id=operation.project_id, entry=entry,
                                    dataset_mode=dataset_mode, names=names))
        except (TypeError, ValueError):
            operation.rejected_rows += 1
        if len(batch) >= operation.batch_size:
            flush()
    flush()
```

`hub_rows.crop_uri` raises HTTPException for a bad value. Inside the worker that must count as a rejected row, not a crash. Wrap it: in `record_for`, catch `fastapi.HTTPException` and re-raise as `ValueError(exc.detail)`. Add `from fastapi import HTTPException` and replace the `uri = ...` line with:

```python
    try:
        uri = hub_rows.crop_uri(row, split, label, file_name, column_map, location)
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc
```

A download failure (HTTPException from `huggingface.download`, raised inside `iter_rows`) is **not** caught. It propagates to `run_import`, which marks the operation failed with `f"{type(exc).__name__}: {exc}"`. `str(HTTPException)` includes the detail in Starlette ≥ 0.36 (`"502: Could not download …"`), so the test's `"timed out"` check holds. If the installed Starlette prints differently, add this to `run_import`'s `except` block: `detail = getattr(exc, "detail", None); message = f"{type(exc).__name__}: {detail or exc}"`.

- [ ] **Step 4: Dispatch in `curation/importer.py`**

In `run_import`, replace the body of the `for source in operation.sources:` loop with:

```python
        for source in operation.sources:
            if cancel_event.is_set():
                break
            operation.current_source = source["name"][:NAME_LEN]
            db.commit()
            kind = source.get("kind", "regional_csv")
            if kind in {"hub_dataset", "hub_link"}:
                from curation import hub_import  # noqa: PLC0415 — hub_import imports this module

                if kind == "hub_dataset":
                    hub_import.import_rows(db, operation, source, cancel_event, dataset_mode)
                else:
                    hub_import.link_rows(db, operation, source, cancel_event)
                continue
            with storage.open_text(source["csv_uri"]) as stream:
                import_csv_stream(db, operation, source, stream, cancel_event, dataset_mode)
```

`link_rows` comes in Task 7. Until then, add a stub to `hub_import.py` so the module imports:

```python
def link_rows(db, operation, source, cancel_event) -> None:  # replaced in Task 7
    raise NotImplementedError("Linking a dataset to a project is not available yet")
```

Update `importer.py`'s module docstring with one sentence: "Hugging Face datasets ride the same worker (`kind: hub_dataset | hub_link`, see `curation/hub_import.py`)."

- [ ] **Step 5: Service and routes**

Append to `curation/registry.py`:

```python
def create_project_from_dataset(db: Session, user: CurrentUser, entry: CurationHubEntry, *, name: str,
                                description: str | None, dataset_mode: str, splits: list[str]):
    from curation import service  # noqa: PLC0415
    from curation.models import CurationImportOperation  # noqa: PLC0415
    from curation.schemas import CurationProjectCreate  # noqa: PLC0415

    if not entry.runnable:
        raise HTTPException(422, "This dataset cannot be imported")
    available = set(hub_rows.parquet_by_split(entry.card, entry.files))
    chosen = [split for split in splits if split in available] if splits else sorted(available)
    if not chosen:
        raise HTTPException(422, f"Choose at least one of: {', '.join(sorted(available))}")
    hub = {"entry_id": entry.id, "repo_id": entry.repo_id, "revision": entry.revision}
    project = service.create_project(db, CurationProjectCreate(
        name=name, description=description or f"Patches from {entry.repo_id}@{entry.revision[:8]}",
        dataset_mode=dataset_mode, source_config={"hub": hub}), user)
    project.source_kind = "huggingface"
    operation = CurationImportOperation(
        project_id=project.id, batch_size=2000, created_by_user_id=user.id, created_by_name=user.label,
        sources=[{"kind": "hub_dataset", "name": f"{entry.repo_id}@{entry.revision[:8]}"[:200],
                  "entry_id": entry.id, "splits": chosen}])
    db.add(operation)
    db.commit()
    db.refresh(project)
    db.refresh(operation)
    return project, operation


def label_report(entry: CurationHubEntry, dataset_mode: str) -> list[dict]:
    from collections import Counter  # noqa: PLC0415

    from curation import taxonomy  # noqa: PLC0415

    column_map = entry.options["column_map"]
    names = hub_rows.class_names(entry.card, column_map["label"])
    counts: Counter = Counter()
    for split, row in hub_rows.iter_rows(entry, None, [name for name in column_map.values() if name]):
        try:
            counts[hub_rows.row_fields(row, split, column_map, names)[1]] += 1
        except ValueError:
            continue
    check = taxonomy.available()
    return [{"label": label, "count": count, "effective": taxonomy.effective_label(label, dataset_mode),
             "known": (not check) or taxonomy.project_code(label, dataset_mode) is not None}
            for label, count in sorted(counts.items())]
```

Append to `routers/registry.py`:

```python
from curation.schemas import CurationImportResponse, CurationProjectResponse  # top of file
from curation.schemas import DatasetMode  # top of file (the T1/T3 enum used by CurationProjectCreate)


class DatasetProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    dataset_mode: DatasetMode = DatasetMode.T3
    splits: list[str] = Field(default_factory=list, max_length=10)


@router.post("/datasets/{entry_id}/projects", status_code=202)
def dataset_project(entry_id: int, body: DatasetProjectIn, user: CurrentUser = Depends(require_lead),
                    db: Session = Depends(get_db)):
    entry = registry.get_entry_or_404(db, entry_id, "dataset")
    project, operation = registry.create_project_from_dataset(
        db, user, entry, name=body.name, description=body.description,
        dataset_mode=body.dataset_mode.value, splits=body.splits)
    return {"project": CurationProjectResponse.model_validate(project),
            "operation": CurationImportResponse.model_validate(operation)}


@router.get("/datasets/{entry_id}/labels")
def dataset_labels(entry_id: int, dataset_mode: DatasetMode = DatasetMode.T3, db: Session = Depends(get_db)):
    return registry.label_report(registry.get_entry_or_404(db, entry_id, "dataset"), dataset_mode.value)
```

(Put the two schema imports with the other imports at the top. `DatasetMode` lives in `curation/schemas.py`; confirm the name with `grep -n "class DatasetMode" curation/schemas.py`.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `... python -m pytest -q tests/test_registry.py tests/test_curation.py`
Expected: all PASS. The CSV import tests still pass through the unchanged CSV branch.

- [ ] **Step 7: Commit**

```bash
git add curation/hub_import.py curation/importer.py curation/registry.py routers/registry.py tests/test_registry.py
git commit -m "A registered Hugging Face dataset becomes a patch-only project

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Link a registered dataset to an existing project (dry run, then apply)

**Files:**
- Modify: `curation/service.py:580-605` (extract `match_items`; `attach_curation_links` uses it)
- Modify: `curation/hub_import.py` (replace the `link_rows` stub)
- Modify: `curation/registry.py` (add `queue_link`)
- Modify: `routers/registry.py` (one route)
- Test: `tests/test_registry.py` (append section)

**Interfaces:**
- Consumes: `service._state(item)`, `service._record_change(db, item, user, event_type, previous, *, operation_id, extra_new)`; Task 6's worker dispatch.
- Produces:
  - `service.match_items(db, project_id, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], list[CurationItem]]`, keyed by `(source_label, file_name)`.
  - `hub_import.Actor(id: int | None, label: str)`, a dataclass that `_record_change` accepts as `user`.
  - `hub_import.link_rows(db, operation, source, cancel_event)`. Link source shape: `{"kind": "hub_link", "name", "entry_id", "adopt_splits": bool, "dry_run": bool}`. On completion it sets `operation.sources[0]["result"] = {matched, unmatched, ambiguous, split_changes, kept_csv_split, already_linked}`.
  - `registry.queue_link(db, user, entry, project, *, adopt_splits, dry_run) -> CurationImportOperation`.
  - Route `POST /api/curation/hub/datasets/{entry_id}/links` with `{project_id, adopt_splits, dry_run}` → 202 operation.

- [ ] **Step 1: Write the failing tests** (append)

```python
# ── Dataset → existing project ────────────────────────────────────────────────

def _csv_project(hub_client):
    """A project whose items carry the crop names the HF rows use (as a CSV import makes them)."""
    from test_curation import _add, _item_payload, _project

    project = _project(hub_client, "CSV project")
    made = {}
    for split, n, label, origin_split in (("test", 0, "ACAS", None), ("test", 2, "POCS", "train"),
                                          ("val", 1, "ACAS", None)):
        file_name = f"{label}_IMG-{split}-{n}_{n + 1}.png"
        payload = {**_item_payload(f"{split}-{n}", label), "file_name": file_name}
        if origin_split:
            payload["split"] = origin_split  # a split that came from the CSV
        made[file_name] = _add(hub_client, project["id"], payload)
    return project, made


def _link(hub_client, entry_id, project_id, *, dry_run, adopt=True):
    response = hub_client.post(f"{P}/hub/datasets/{entry_id}/links",
                               json={"project_id": project_id, "adopt_splits": adopt, "dry_run": dry_run})
    assert response.status_code == 202, response.text
    _drain(response.json()["id"])
    return hub_client.get(f"{P}/projects/{project_id}/imports/{response.json()['id']}").json()


def test_link_dry_run_counts_without_changing_anything(hub_client):
    from appdb import session_scope
    from curation.models import CurationEvent, CurationItem

    entry = _registered_dataset(hub_client)
    project, made = _csv_project(hub_client)
    with session_scope() as db:
        before = {item.id: (item.split, item.revision) for item in db.query(CurationItem)}
        events_before = db.query(CurationEvent).count()
    done = _link(hub_client, entry["id"], project["id"], dry_run=True)
    assert done["status"] == "completed", done
    result = done["sources"][0]["result"]
    assert result["matched"] == 3 and result["unmatched"] == 6 and result["ambiguous"] == 0
    assert result["kept_csv_split"] == 1   # the POCS item's CSV said train; HF says test
    with session_scope() as db:
        assert {item.id: (item.split, item.revision) for item in db.query(CurationItem)} == before
        assert db.query(CurationEvent).count() == events_before


def test_link_apply_records_provenance_and_events(hub_client):
    from appdb import session_scope
    from curation.models import CurationEvent, CurationItem, CurationProject

    entry = _registered_dataset(hub_client)
    project, made = _csv_project(hub_client)
    done = _link(hub_client, entry["id"], project["id"], dry_run=False)
    assert done["status"] == "completed", done
    with session_scope() as db:
        items = {item.file_name: item for item in db.query(CurationItem)}
        acas = items["ACAS_IMG-test-0_1.png"]
        assert acas.metadata_json["hub"] == {"repo_id": DATASET, "revision": DATASET_SHA, "split": "test"}
        assert acas.revision == made["ACAS_IMG-test-0_1.png"]["revision"] + 1
        assert items["POCS_IMG-test-2_3.png"].split == "train"   # CSV split wins
        events = db.query(CurationEvent).filter(CurationEvent.operation_id == done["id"]).all()
        assert {event.event_type for event in events} <= {"hub_link", "split_adopted"}
        assert len(events) == 3
        links = db.get(CurationProject, project["id"]).source_config["hub_links"]
        assert links[0]["entry_id"] == entry["id"] and links[0]["revision"] == DATASET_SHA
    again = _link(hub_client, entry["id"], project["id"], dry_run=False)
    assert again["sources"][0]["result"]["already_linked"] == 3


def test_published_versions_keep_their_frozen_splits(hub_client):
    from test_versions import _publish

    entry = _registered_dataset(hub_client)
    project, made = _csv_project(hub_client)
    version = _publish(hub_client, project["id"])
    _link(hub_client, entry["id"], project["id"], dry_run=False)
    frozen = hub_client.get(f"{P}/projects/{project['id']}/versions/{version['version']}").json()
    assert frozen["statistics"] == version["statistics"]


def test_match_items_is_shared_with_matrix_links(db_session):
    from curation import service

    assert service.match_items(db_session, "no-such-project", []) == {}
```

`db_session` comes from `test_curation`; import it at the top next to `team`:
`from test_curation import _test_buckets, db_session, team  # noqa: F401 — fixtures`.

- [ ] **Step 2: Run them to verify they fail**

Run: `... python -m pytest -q tests/test_registry.py -k "link or frozen or match_items"`
Expected: FAIL with 404 on `/links`.

- [ ] **Step 3: Extract the matcher in `curation/service.py`**

Replace the query-and-group part of `attach_curation_links` with a call to a new function above it:

```python
def match_items(db: Session, project_id: str, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], list[CurationItem]]:
    """Items keyed by (source label, crop file name): how exported crops find their way home."""
    pairs = list(pairs)
    if not pairs:
        return {}
    file_names = list({file_name for _, file_name in pairs})
    labels = list({label for label, _ in pairs})
    rows = db.scalars(select(CurationItem).where(
        CurationItem.project_id == project_id,
        in_chunks(CurationItem.file_name, file_names),
        in_chunks(CurationItem.source_label, labels),
    )).all()
    wanted = set(pairs)
    matches: dict[tuple[str, str], list[CurationItem]] = {}
    for row in rows:
        key = (row.source_label, row.file_name)
        if key in wanted:
            matches.setdefault(key, []).append(row)
    return matches


def attach_curation_links(db: Session, project_id: str, assets: list[dict], mutable: bool) -> list[dict]:
    """Match evaluation/split crops back to curation items by (label, file name)."""
    if not assets:
        return []
    matches = match_items(db, project_id, [(asset["actual_label"], asset["file_name"]) for asset in assets])
    linked = []
    for asset in assets:
        # ... unchanged from here ...
```

Add `Iterable` to the `typing` import if it is missing.

- [ ] **Step 4: Implement `link_rows` in `curation/hub_import.py`** (replace the stub)

```python
from dataclasses import dataclass

from curation import service
from curation.models import CurationProject


@dataclass
class Actor:
    """Who a background change is attributed to: the lead who started it."""
    id: int | None
    label: str


def link_rows(db: Session, operation: CurationImportOperation, source: dict,
              cancel_event: threading.Event) -> None:
    entry = _entry(db, source)
    column_map = entry.options["column_map"]
    names = hub_rows.class_names(entry.card, column_map["label"])
    adopt, dry_run = bool(source.get("adopt_splits")), bool(source.get("dry_run"))
    actor = Actor(operation.created_by_user_id, operation.created_by_name or "import")
    hub = {"repo_id": entry.repo_id, "revision": entry.revision}
    result = {"matched": 0, "unmatched": 0, "ambiguous": 0, "split_changes": 0,
              "kept_csv_split": 0, "already_linked": 0}
    pending: list[tuple[str, str, str]] = []  # (split, label, file name)

    def flush() -> None:
        matches = service.match_items(db, operation.project_id, [(label, name) for _, label, name in pending])
        for split, label, file_name in pending:
            candidates = matches.get((label, file_name), [])
            if not candidates:
                result["unmatched"] += 1
                continue
            if len(candidates) > 1:
                result["ambiguous"] += 1
                continue
            item = candidates[0]
            result["matched"] += 1
            metadata = dict(item.metadata_json or {})
            from_csv = metadata.get("split_origin") == "imported"
            change_split = adopt and item.split != split and not from_csv
            if adopt and item.split != split and from_csv:
                result["kept_csv_split"] += 1
            if change_split:
                result["split_changes"] += 1
            link = {**hub, "split": split}
            if metadata.get("hub") == link and not change_split:
                result["already_linked"] += 1
                continue
            if dry_run:
                continue
            previous = {**service._state(item), "split": item.split}  # noqa: SLF001 — same package
            metadata["hub"] = link
            if change_split:
                item.split = split
                metadata["split_origin"] = "hub"
            item.metadata_json = metadata
            service._record_change(  # noqa: SLF001
                db, item, actor, "split_adopted" if change_split else "hub_link", previous,
                operation_id=operation.id, extra_new={"split": item.split, "hub": link})
        operation.imported_rows = result["matched"]
        operation.skipped_rows = result["unmatched"]
        operation.rejected_rows = result["ambiguous"]
        pending.clear()
        db.add(operation)
        db.commit()

    for split, row in hub_rows.iter_rows(entry, None, [name for name in column_map.values() if name]):
        if cancel_event.is_set():
            break
        operation.processed_rows += 1
        try:
            pending.append(hub_rows.row_fields(row, split, column_map, names))
        except ValueError:
            continue
        if len(pending) >= operation.batch_size:
            flush()
    flush()

    operation.sources = [{**source, "result": result}]
    if not dry_run and not cancel_event.is_set():
        project = db.get(CurationProject, operation.project_id)
        config = dict(project.source_config or {})
        links = [link for link in config.get("hub_links", []) if link.get("entry_id") != entry.id]
        links.append({"entry_id": entry.id, **hub, "linked_at": utcnow().isoformat() + "Z",
                      "adopted_splits": adopt})
        config["hub_links"] = links
        project.source_config = config
    db.commit()
```

`split_origin` values:
- `"imported"`: the split came from a CSV row or an item payload (`curation/service.py:166`, `curation/importer.py`). The link never overrides it.
- `"generated"`: hashed from the source image. The link may replace it.
- `"hub"`: the split came from a Hugging Face dataset, set by Task 6's `record_for` or by an adopted link.

Linking twice is therefore stable: the second run finds `metadata["hub"] == link` and reports `already_linked`.

Check that `_record_change` only reads `user.id` and `user.label` (it does, `curation/service.py:118-133`), so `Actor` fits.

- [ ] **Step 5: Service and route**

Append to `curation/registry.py`:

```python
def queue_link(db: Session, user: CurrentUser, entry: CurationHubEntry, project: CurationProject, *,
               adopt_splits: bool, dry_run: bool):
    from curation.models import CurationImportOperation  # noqa: PLC0415

    if not entry.runnable:
        raise HTTPException(422, "This dataset cannot be linked")
    verb = "Preview link" if dry_run else "Link"
    operation = CurationImportOperation(
        project_id=project.id, batch_size=2000, created_by_user_id=user.id, created_by_name=user.label,
        sources=[{"kind": "hub_link", "name": f"{verb} {entry.repo_id}@{entry.revision[:8]}"[:200],
                  "entry_id": entry.id, "adopt_splits": adopt_splits, "dry_run": dry_run}])
    db.add(operation)
    db.commit()
    db.refresh(operation)
    return operation
```

Append to `routers/registry.py`:

```python
class LinkIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)
    adopt_splits: bool = False
    dry_run: bool = True


@router.post("/datasets/{entry_id}/links", status_code=202, response_model=CurationImportResponse)
def link_dataset(entry_id: int, body: LinkIn, user: CurrentUser = Depends(require_lead), db: Session = Depends(get_db)):
    from curation import service  # noqa: PLC0415

    entry = registry.get_entry_or_404(db, entry_id, "dataset")
    project = service.get_project_or_404(db, body.project_id)
    return registry.queue_link(db, user, entry, project, adopt_splits=body.adopt_splits, dry_run=body.dry_run)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `... python -m pytest -q tests/test_registry.py tests/test_curation.py tests/test_evaluations.py`
Expected: all PASS. `attach_curation_links` callers in `test_curation`/`test_evaluations` stay green.

- [ ] **Step 7: Commit**

```bash
git add curation/service.py curation/hub_import.py curation/registry.py routers/registry.py tests/test_registry.py
git commit -m "Link a Hugging Face dataset to an existing project: preview, then apply with an audit trail

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Hugging Face provenance in published versions

**Files:**
- Modify: `curation/versions.py` (`_write_files` summary, `changelog_markdown`)
- Modify: `static/curation/curation.js:1614` (patch-only items show "—" rather than "null, null")
- Test: `tests/test_registry.py` (append)

**Interfaces:**
- Consumes: `project.source_config["hub"]` (Task 6) and `project.source_config["hub_links"]` (Task 7).
- Produces: `versions.hub_provenance(project) -> list[dict]`: `[{"repo_id", "revision", "relation": "source" | "linked"}]`.

- [ ] **Step 1: Write the failing test** (append)

```python
# ── Versions remember where they came from ────────────────────────────────────

def test_versions_record_hugging_face_provenance(hub_client):
    from test_versions import _publish

    from curation import settings

    entry = _registered_dataset(hub_client)
    project, _ = _csv_project(hub_client)
    _link(hub_client, entry["id"], project["id"], dry_run=False)
    version = _publish(hub_client, project["id"])
    manifest = hub_client.get(f"{P}/projects/{project['id']}/versions/{version['version']}/files/manifest.json").json()
    assert manifest["derived_from"] == [{"repo_id": DATASET, "revision": DATASET_SHA, "relation": "linked"}]
    changelog = hub_client.get(f"{P}/projects/{project['id']}/versions/{version['version']}/files/CHANGELOG.md").text
    assert f"Derived from: {DATASET}@{DATASET_SHA[:8]} (linked)" in changelog
```

- [ ] **Step 2: Run it to verify it fails**

Run: `... python -m pytest -q tests/test_registry.py -k provenance`
Expected: FAIL with `KeyError: 'derived_from'`.

- [ ] **Step 3: Implement**

In `curation/versions.py`, add above `_write_files`:

```python
def hub_provenance(project: CurationProject) -> list[dict]:
    """Hugging Face datasets this project was built from or linked to (curation/registry.py)."""
    config = project.source_config or {}
    found = []
    if config.get("hub"):
        found.append({"repo_id": config["hub"]["repo_id"], "revision": config["hub"]["revision"], "relation": "source"})
    for link in config.get("hub_links", []):
        found.append({"repo_id": link["repo_id"], "revision": link["revision"], "relation": "linked"})
    return found
```

In `_write_files`'s `summary` dict, add `"derived_from": hub_provenance(project),`.

In `changelog_markdown`, after the `lines = [...]` opening block (before `if version.notes:`), add:

```python
    for source in hub_provenance(project):
        lines.append(f"Derived from: {source['repo_id']}@{source['revision'][:8]} ({source['relation']}).")
```

The test checks the substring without the trailing period, so either form passes.

In `static/curation/curation.js` line 1614, change `<small>${item.column_px}, ${item.row_px}</small>` to `<small>${item.row_px == null ? 'patch' : `${item.column_px}, ${item.row_px}`}</small>`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `... python -m pytest -q tests/test_registry.py tests/test_versions.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add curation/versions.py static/curation/curation.js tests/test_registry.py
git commit -m "Published versions name the Hugging Face datasets they came from

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Evaluate a registered model on the val and test splits, at full scale

**Why this task exists:**
- The worker already scores per split, and the run form already lets you pick only val and test. But nothing tests that chain for an HF model on an HF-built project.
- The current staging step cannot handle 113,634 patches (val 56,770 + test 56,864). It fetches one patch at a time from GCS in the web process, decodes and re-encodes each PNG, and writes it twice.

**Measured on 2026-10-11 from the developer's machine:**
- 0.38–0.5 s per sequential patch fetch, with a fresh connection each time.
- Link speed about 0.93 MB/s; patches about 100 KB each.
- So val+test is about 11 GB: at least 3½ h from that machine, whatever the code does. On the deployed GCP workstation, parallel fetches bring it to minutes.

**What this task does:**
- Fetches patches 32 at a time.
- Keeps a content cache, so every patch is downloaded **once** and later runs hard-link it.
- Skips re-encoding when the patch is already an RGB PNG.
- Missing patches no longer abort the run. **They are left unscored and reported** (the user's decision), with a guard: more than `CURATION_EVAL_MAX_MISSING` (default 5%) missing still fails the run, so a wrong root cannot "succeed" on a handful of images.

**Files:**
- Modify: `curation/settings.py` (append `evaluation_fetch_workers`, `evaluation_max_missing`, `patch_cache_dir`)
- Modify: `curation/images.py` (add pure `is_allowed_in(buckets, uri)`; `is_allowed` uses it)
- Modify: `curation/evaluation_runner.py` (`stage_run`: parallel, cached, linked, tolerant of missing patches)
- Modify: `curation/evaluation_runs.py` (`summary` and `matrix` report unscored patches)
- Modify: `routers/curation.py` (`GET /projects/{pid}/evaluations/{run_id}/missing.csv`)
- Modify: `curation/registry.py`, `routers/registry.py` (`label_report` takes an optional `classifier_id`)
- Modify: `static/curation/curation.js:1021` (the runs list shows missing patches and links the CSV)
- Test: `tests/test_hub_evaluation.py` (new)

**Interfaces:**
- Consumes:
  - Tasks 5–6: the `hub_client` and `hub_files` fixtures, `_registered_dataset` and `_drain` in `tests/test_registry.py`.
  - `_publish` (`tests/test_versions.py`) and `_jpeg` (`tests/test_curation.py`).
- Produces:
  - `settings.evaluation_fetch_workers() -> int` (1–64, default 32).
  - `settings.evaluation_max_missing() -> float` (0–1, default 0.05).
  - `settings.patch_cache_dir() -> Path | None` (`None` when `CURATION_PATCH_CACHE=off`).
  - `images.is_allowed_in(buckets: set[str], uri: str) -> bool`.
  - `evaluation_runner.load_patch(item, project, buckets) -> bytes | None`, returning RGB PNG bytes.
  - After staging:
    - `run.specification["requested_split_counts"]`
    - `run.specification["split_counts"]`, now the **staged** counts
    - `run.expected_count`, now the staged total
    - `run.provenance["unscored"] = {"count", "by_split", "examples"}`
    - `evaluation_runs.directory(run.id) / "missing.json"`
  - `evaluation_runs.summary(run)["unscored_count"]`.
  - `matrix(...)["unscored_count"]` and `["coverage"]["expected"]` use the requested counts.
  - `registry.label_report(entry, dataset_mode, class_names: list[str] | None = None)` adds `"in_model": bool | None` to each row.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hub_evaluation.py
"""A Hugging Face model scored on the val and test splits of a Hugging Face project."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from test_curation import _jpeg, _test_buckets, team  # noqa: F401 — fixtures
from test_registry import (  # noqa: F401 — fixtures and helpers
    DATASET, MODEL, P, _drain, _registered_dataset, hub_client, hub_files,
)
from test_versions import _publish


def _hf_project(hub_client, mode="T3"):
    entry = _registered_dataset(hub_client)
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": f"HF {mode}", "dataset_mode": mode}).json()
    _drain(created["operation"]["id"])
    _publish(hub_client, created["project"]["id"])
    return created["project"]["id"], entry


def _hf_model(hub_client, monkeypatch, class_names=("ACAS", "POCS")):
    from appdb import session_scope
    from curation import model_registry
    from curation.models import CurationClassifier

    entry = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                    "weight_file": "weights/best.pt", "trusted": True}).json()
    with session_scope() as db:
        model = db.get(CurationClassifier, entry["classifier_id"])
        model.status = "ready"
        model.class_names = list(class_names)
    monkeypatch.setattr(model_registry, "worker_available", lambda: True)
    return entry["classifier_id"]


def _start(hub_client, pid, classifier_id, splits=("val", "test")):
    response = hub_client.post(f"{P}/projects/{pid}/evaluations",
                               json={"version": 1, "classifier_id": classifier_id, "splits": list(splits)})
    return response


def _stage(run_id):
    from appdb import session_scope
    from curation import evaluation_runner
    from curation.models import CurationEvaluationRun

    with session_scope() as db:
        evaluation_runner.stage_run(db, db.get(CurationEvaluationRun, run_id))


def _finish(run_id, label="ACAS"):
    """Write what the worker would, for every staged image, and ingest it."""
    from appdb import session_scope
    from curation import evaluation_runner, evaluation_runs, settings
    from curation.models import CurationEvaluationRun

    with session_scope() as db:
        run = db.get(CurationEvaluationRun, run_id)
        manifest = json.loads((evaluation_runs.directory(run_id) / "manifest.json").read_text(encoding="utf-8"))
        output = settings.evaluation_spool() / "outputs" / run_id
        output.mkdir(parents=True, exist_ok=True)
        lines = [{"item_id": entry["item_id"], "top_predictions": [
            {"rank": 1, "label": label, "confidence": 0.9}, {"rank": 2, "label": "POCS", "confidence": 0.1}]}
            for entry in manifest]
        path = output / "predictions.jsonl"
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        counts = {}
        for entry in manifest:
            counts[entry["split"]] = counts.get(entry["split"], 0) + 1
        results = {split: {"images": count} for split, count in counts.items()} | {"all": {"images": len(manifest)}}
        evaluation_runner.ingest_result(db, run, output, {
            "model_sha256": run.specification["model_sha256"], "manifest_sha256": run.provenance["manifest_sha256"],
            "predictions_sha256": evaluation_runner.file_hash(path), "results": results,
            "runtime": {"evaluator_version": "test"}})


def test_hf_model_scores_val_and_test_of_an_hf_project(hub_client, monkeypatch, tmp_path):
    from curation import evaluation_runs, settings, storage

    monkeypatch.setenv("CURATION_PATCH_CACHE", str(tmp_path / "patches"))
    fetched = []
    monkeypatch.setattr(storage, "read_bytes", lambda uri: fetched.append(uri) or _jpeg((224, 224)))
    pid, _ = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch)
    response = _start(hub_client, pid, classifier_id)
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["expected_count"] == 6 and run["splits"] == ["val", "test"]
    _stage(run["id"])
    manifest = json.loads((evaluation_runs.directory(run["id"]) / "manifest.json").read_text(encoding="utf-8"))
    assert {entry["split"] for entry in manifest} == {"val", "test"}
    assert [entry["item_id"] for entry in manifest] == sorted(entry["item_id"] for entry in manifest)
    private = evaluation_runs.directory(run["id"]) / "images" / f"{manifest[0]['item_id']}.png"
    spooled = settings.evaluation_spool() / "inputs" / run["id"] / "images" / f"{manifest[0]['item_id']}.png"
    assert os.path.samefile(private, spooled)  # linked, not written twice
    _finish(run["id"])
    base = f"{P}/projects/{pid}/confusion-matrix"
    for split in ("val", "test"):
        matrix = hub_client.get(base, params={"matrix_source": "evaluation", "evaluation_id": run["id"],
                                              "split": split, "normalization": "count"}).json()
        assert matrix["scored_count"] == 3 and matrix["unscored_count"] == 0
        assert matrix["accuracy"] == pytest.approx(2 / 3)  # two ACAS, one POCS, all predicted ACAS

    # A second evaluation of the same version downloads nothing: every patch is in the cache.
    fetched.clear()
    _stage(_start(hub_client, pid, classifier_id).json()["id"])
    assert fetched == []


def test_a_t1_project_cannot_be_scored_by_a_t3_model(hub_client, monkeypatch):
    from curation import storage

    monkeypatch.setattr(storage, "read_bytes", lambda uri: _jpeg((224, 224)))
    pid, _ = _hf_project(hub_client, mode="T1")
    response = _start(hub_client, pid, _hf_model(hub_client, monkeypatch))
    assert response.status_code == 422
    assert "Classifier does not contain dataset labels" in response.json()["detail"]


def test_missing_patches_are_left_unscored_and_reported(hub_client, monkeypatch, tmp_path):
    from curation import storage

    monkeypatch.setenv("CURATION_PATCH_CACHE", "off")
    monkeypatch.setenv("CURATION_EVAL_MAX_MISSING", "0.5")
    pid, _ = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch)

    def read(uri):
        if uri.endswith("ACAS_IMG-test-0_1.png"):
            raise FileNotFoundError(uri)
        return _jpeg((224, 224))

    monkeypatch.setattr(storage, "read_bytes", read)
    run_id = _start(hub_client, pid, classifier_id).json()["id"]
    _stage(run_id)
    summary = hub_client.get(f"{P}/projects/{pid}/evaluations/{run_id}").json()
    assert summary["expected_count"] == 5 and summary["unscored_count"] == 1
    assert summary["specification"]["requested_split_counts"] == {"val": 3, "test": 3}
    missing = hub_client.get(f"{P}/projects/{pid}/evaluations/{run_id}/missing.csv")
    assert missing.status_code == 200 and "ACAS_IMG-test-0_1.png" in missing.text
    _finish(run_id)
    matrix = hub_client.get(f"{P}/projects/{pid}/confusion-matrix", params={
        "matrix_source": "evaluation", "evaluation_id": run_id, "split": "test", "normalization": "count"}).json()
    assert matrix["scored_count"] == 2 and matrix["unscored_count"] == 1
    assert matrix["coverage"]["expected"] == 3


def test_too_many_missing_patches_still_fail(hub_client, monkeypatch):
    from curation import storage

    monkeypatch.setenv("CURATION_PATCH_CACHE", "off")
    pid, _ = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch)

    def gone(uri):
        raise FileNotFoundError(uri)

    monkeypatch.setattr(storage, "read_bytes", gone)
    run_id = _start(hub_client, pid, classifier_id).json()["id"]
    with pytest.raises(ValueError, match=r"6 of 6 patches are missing"):
        _stage(run_id)


def test_parallel_staging_keeps_order_and_honours_cancel(hub_client, monkeypatch):
    from appdb import session_scope
    from curation import storage
    from curation.models import CurationEvaluationRun

    monkeypatch.setenv("CURATION_PATCH_CACHE", "off")
    monkeypatch.setenv("CURATION_EVAL_FETCH_WORKERS", "4")
    pid, _ = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch)
    monkeypatch.setattr(storage, "read_bytes",
                        lambda uri: time.sleep(0.01 * (hash(uri) % 5)) or _jpeg((224, 224)))
    run_id = _start(hub_client, pid, classifier_id, splits=("train", "val", "test")).json()["id"]
    with session_scope() as db:
        db.get(CurationEvaluationRun, run_id).cancel_requested = True
    _stage(run_id)
    with session_scope() as db:
        assert db.get(CurationEvaluationRun, run_id).status == "cancelled"


def test_label_report_says_which_labels_the_model_knows(hub_client, monkeypatch):
    pid, entry = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch, class_names=("ACAS",))
    report = hub_client.get(f"{P}/hub/datasets/{entry['id']}/labels",
                            params={"dataset_mode": "T3", "classifier_id": classifier_id}).json()
    assert {row["label"]: row["in_model"] for row in report} == {"ACAS": True, "POCS": False}
```

`_hf_project` publishes before the storage stub in some tests. Publishing does not read images, so that is fine. `_finish` predicts every image as ACAS. The fixture has two ACAS and one POCS per split, hence the 2/3.

- [ ] **Step 2: Run them to verify they fail**

Run: `... python -m pytest -q tests/test_hub_evaluation.py`
Expected: FAIL. Staging writes copies (`samefile` is False), and there is no `unscored_count` and no `missing.csv` route.

- [ ] **Step 3: Settings and the pure bucket check**

Append to `curation/settings.py`:

```python
def evaluation_fetch_workers() -> int:
    """Patches fetched at once while staging an evaluation (CURATION_EVAL_FETCH_WORKERS, default 32).

    Patches are small (~100 KB); a sequential fetch spends most of its time
    waiting on each request, so parallel reads are what makes 100k-image
    splits practical.
    """
    try:
        return max(1, min(64, int(os.getenv("CURATION_EVAL_FETCH_WORKERS", "32"))))
    except ValueError:
        return 32


def evaluation_max_missing() -> float:
    """Largest share of patches an evaluation may leave unscored before it fails (default 5%)."""
    try:
        return max(0.0, min(1.0, float(os.getenv("CURATION_EVAL_MAX_MISSING", "0.05"))))
    except ValueError:
        return 0.05


def patch_cache_dir() -> Path | None:
    """Patches downloaded for evaluations, kept so the next run reuses them. 'off' disables."""
    raw = os.getenv("CURATION_PATCH_CACHE", "").strip()
    if raw.lower() in {"off", "0", "no", "false"}:
        return None
    return Path(raw) if raw else eval_dir() / "patch_cache"
```

In `curation/images.py`, split `is_allowed` into a pure check plus the database lookup:

```python
def is_allowed_in(buckets: set[str], uri: str) -> bool:
    """is_allowed against a bucket set fetched once, for loops and worker threads."""
    bucket = _bucket(uri)
    if bucket is None:
        return settings.allow_local_sources() and bool(uri.strip())
    return bucket in buckets


def is_allowed(db: Session, uri: str) -> bool:
    # docstring unchanged
    return is_allowed_in(allowed_buckets(db), uri)
```

- [ ] **Step 4: Rewrite `stage_run` in `curation/evaluation_runner.py`**

Add imports: `import os`, `from collections import Counter` (if not already there), `from concurrent.futures import ThreadPoolExecutor`. Then add these helpers above `stage_run`:

```python
CHUNK = 256


def _png_rgb(payload: bytes) -> bytes:
    """The patch as an RGB PNG; already-RGB PNGs (the HF patches) pass through untouched."""
    with Image.open(BytesIO(payload)) as image:
        if image.format == "PNG" and image.mode == "RGB":
            return payload
        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        return buffer.getvalue()


def _cache_path(uri: str) -> Path | None:
    root = settings.patch_cache_dir()
    if root is None:
        return None
    digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()
    return root / digest[:2] / f"{digest}.png"


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:  # another filesystem, or links not allowed
        shutil.copyfile(source, target)


def load_patch(item, project, buckets: set[str]) -> tuple[bytes, Path | None] | None:
    """(RGB PNG bytes, cache file) for one frozen item, or None when no copy can be found.

    Runs on worker threads: no database session here, only the bucket set
    fetched once by stage_run.
    """
    for uri in images.crop_candidates(item, project):
        if not images.is_allowed_in(buckets, uri):
            continue
        cached = _cache_path(uri)
        if cached is not None and cached.is_file():
            return cached.read_bytes(), cached
        try:
            payload = _png_rgb(storage.read_bytes(uri))
        except FileNotFoundError:
            continue
        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            partial = cached.with_suffix(f".{threading.get_ident()}.partial")
            partial.write_bytes(payload)
            partial.replace(cached)
        return payload, cached
    if item.row_px is not None and item.column_px is not None and images.is_allowed_in(buckets, item.source_image_uri):
        try:
            return _png_rgb(images.crop_from_source(item)), None
        except FileNotFoundError:
            return None
    return None
```

Add `from curation import storage` and `import threading` at module top if missing. Remove the function-local `from curation import storage` inside the old loop.

Replace the body of `stage_run` from `frozen = ...` through the `if len(manifest) != run.expected_count:` check with:

```python
    frozen = db.scalars(select(CurationVersionInput).where(
        CurationVersionInput.version_id == run.version_id,
        CurationVersionInput.split.in_(run.specification["splits"])).order_by(CurationVersionInput.item_id)).all()
    project = SimpleNamespace(dataset_mode=run.specification["dataset_mode"])
    buckets = images.allowed_buckets(db)
    private_root = evaluation_runs.directory(run.id) / "images"
    private_root.mkdir(parents=True, exist_ok=True)
    manifest, missing = [], []
    with ThreadPoolExecutor(max_workers=settings.evaluation_fetch_workers(),
                            thread_name_prefix="eval-fetch") as pool:
        for start in range(0, len(frozen), CHUNK):
            db.refresh(run)
            if run.cancel_requested:
                run.status = "cancelled"
                run.completed_at = utcnow()
                db.commit()
                return
            chunk = frozen[start:start + CHUNK]
            # map keeps input order, so the manifest stays in item_id order.
            for item, loaded in zip(chunk, pool.map(lambda frozen_item: load_patch(frozen_item, project, buckets), chunk)):
                if loaded is None:
                    missing.append({"item_id": item.item_id, "split": item.split, "crop_uri": item.crop_uri})
                    continue
                payload, cached = loaded
                private = private_root / f"{item.item_id}.png"
                if cached is not None:
                    _link_or_copy(cached, private)
                else:
                    private.write_bytes(payload)
                _link_or_copy(private, folder / "images" / f"{item.item_id}.png")
                manifest.append({"item_id": item.item_id, "source_key": item.source_key, "label": item.label,
                                 "split": item.split, "file_name": item.file_name,
                                 "image_sha256": hashlib.sha256(payload).hexdigest()})
    requested = len(frozen)
    if requested != run.expected_count:
        raise ValueError("Frozen input count does not match the requested evaluation")
    if missing and len(missing) / requested > settings.evaluation_max_missing():
        examples = ", ".join(entry["crop_uri"] for entry in missing[:3])
        raise ValueError(f"{len(missing):,} of {requested:,} patches are missing (more than "
                         f"{settings.evaluation_max_missing():.0%} allowed by CURATION_EVAL_MAX_MISSING); first: {examples}")
    if not manifest:
        raise ValueError("No patches could be read for this evaluation")
    (evaluation_runs.directory(run.id) / "missing.json").write_text(json.dumps(missing), encoding="utf-8")
    staged = Counter(entry["split"] for entry in manifest)
    run.specification = {**run.specification, "requested_split_counts": run.specification["split_counts"],
                         "split_counts": {split: staged[split] for split in run.specification["splits"] if staged[split]}}
    run.expected_count = len(manifest)
    unscored = {"count": len(missing), "by_split": dict(Counter(entry["split"] for entry in missing)),
                "examples": missing[:20]}
```

Leave the rest of `stage_run` (manifest writing, provenance, `task.json`) as it is, with one change: add `"unscored": unscored` to the `run.provenance = {...}` dict. Reassigning `run.specification` and `run.provenance` as new dicts is deliberate: SQLAlchemy does not see changes made inside a JSON value.

`ingest_result` needs no change. It already compares against `run.expected_count` and `run.specification["split_counts"]`, which now hold the staged counts.

- [ ] **Step 5: Report the gaps**

In `curation/evaluation_runs.py`:
- `summary` adds `"unscored_count": (run.provenance or {}).get("unscored", {}).get("count", 0),`.
- In `matrix`, compute before the `return`:

```python
    unscored_by_split = (run.provenance or {}).get("unscored", {}).get("by_split", {})
    unscored = sum(unscored_by_split.values()) if split == "all" else unscored_by_split.get(split, 0)
    requested = run.specification.get("requested_split_counts", run.specification["split_counts"])
    expected = sum(requested.values()) if split == "all" else requested.get(split, scored)
```

  Then use `"unscored_count": unscored` and `"coverage": {"expected": expected, "scored": scored, "mixed_splits": split == "all"}`.

In `routers/curation.py`, after the evaluation routes, add:

```python
@router.get("/projects/{project_id}/evaluations/{run_id}/missing.csv")
def evaluation_missing(project_id: str, run_id: str, db: Session = Depends(get_db)):
    run = db.get(CurationEvaluationRun, run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(404, "Evaluation not found")
    path = evaluation_runs.directory(run.id) / "missing.json"
    rows = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["item_id", "split", "crop_uri"])
    writer.writerows([row["item_id"], row["split"], row["crop_uri"]] for row in rows)
    return Response(buffer.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="missing-{run.id}.csv"'})
```

In `static/curation/curation.js:1021`, after the `${run.scored_count…}/${run.expected_count…}` text, add:

```javascript
${run.unscored_count ? ` · <a href="/api/curation/projects/${state.projectId}/evaluations/${run.id}/missing.csv">${run.unscored_count.toLocaleString()} missing patches</a>` : ''}
```

- [ ] **Step 6: Which labels the model knows**

Change `registry.label_report`'s signature to `label_report(entry, dataset_mode, class_names: list[str] | None = None)`. Add this to each row:

```python
"in_model": (taxonomy.effective_label(label, dataset_mode) in class_names) if class_names is not None else None,
```

In `routers/registry.py`, `dataset_labels` gains `classifier_id: Optional[str] = None`:

```python
    names = None
    if classifier_id:
        model = db.get(CurationClassifier, classifier_id)
        names = model.class_names if model is not None and model.status == "ready" else None
    return registry.label_report(entry, dataset_mode.value, names)
```

(import `CurationClassifier` from `curation.models`).

- [ ] **Step 7: Run the tests to verify they pass**

Run: `... python -m pytest -q tests/test_hub_evaluation.py tests/test_evaluations.py tests/test_registry.py`
Expected: all PASS. The existing evaluation tests still pass: their crops are JPEGs, so they still go through `_png_rgb`'s re-encode path.

- [ ] **Step 8: Commit**

```bash
git add curation/settings.py curation/images.py curation/evaluation_runner.py curation/evaluation_runs.py routers/curation.py curation/registry.py routers/registry.py static/curation/curation.js tests/test_hub_evaluation.py
git commit -m "Evaluate a model on val and test at full scale: parallel, cached patches; gaps reported, not fatal

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: The registry in the curation page

**Files:**
- Create: `static/curation/hub.js` (classic script, loaded after `curation.js`)
- Modify: `templates/curation.html` (Registry header button, "From Hugging Face…" buttons, two dialogs, script tag)
- Modify: `static/curation.css` (dialog layout)

**Interfaces:**
- Consumes these `curation.js` globals: `byId`, `request(url, options)` (returns parsed JSON and throws `Error(message)`), `escapeHtml`, `canDo(role)`, `state.projectId`, `loadProjects()`, `loadEvaluationCatalog()`. Also the Task 5–7 routes.
- Produces DOM ids: `hub-registry-button`, `hub-registry-dialog`, `hub-registry-list`, `hub-dialog`, `hub-form`, `hub-ref`, `hub-lookup`, `hub-preview`, `hub-dataset-fields`, `hub-model-fields`, `hub-message`, `project-from-hub`, `model-from-hub`.

- [ ] **Step 1: Markup**

In `templates/curation.html`'s header controls (after `new-project-button`), add:

```html
      <button id="hub-registry-button" class="btn btn-outline-light" type="button" title="Hugging Face datasets and models registered here">
        <i class="bi bi-collection"></i><span>Registry</span>
      </button>
```

In `project-dialog`, before the `<p class="text-muted small mt-3">`, add:

```html
    <button id="project-from-hub" class="btn btn-sm btn-outline-primary mt-3" type="button" data-min-role="lead"><i class="bi bi-box-arrow-in-down"></i> From Hugging Face…</button>
```

In `evaluation-model-form`, before the register button, add:

```html
        <button id="model-from-hub" class="btn btn-sm btn-outline-secondary" type="button" data-min-role="lead"><i class="bi bi-box-arrow-in-down"></i> From Hugging Face…</button>
```

Before `<script src="/static/curation/curation.js">`, add both dialogs:

```html
<dialog id="hub-dialog" class="curation-dialog hub-dialog">
  <form id="hub-form" method="dialog">
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h2 id="hub-title">From Hugging Face</h2>
      <button class="btn btn-sm btn-outline-secondary icon-button" type="button" data-close-dialog="hub-dialog" title="Close"><i class="bi bi-x-lg"></i></button>
    </div>
    <label class="form-label" for="hub-ref">Repository link or owner/name</label>
    <div class="d-flex gap-2">
      <input id="hub-ref" class="form-control" required maxlength="500" placeholder="https://huggingface.co/datasets/NMFS-OSI/noaa-pacific-benthic-cover-t3">
      <button id="hub-lookup" class="btn btn-outline-primary" type="button">Look up</button>
    </div>
    <p class="text-muted small mt-1">Public repositories only. The commit you register is pinned: later changes to the repository do not change what is here.</p>
    <div id="hub-preview" class="hub-preview" aria-live="polite"></div>
    <div id="hub-dataset-fields" class="d-none">
      <fieldset class="mt-2"><legend class="form-label">Where the patches are</legend>
        <label class="d-block"><input type="radio" name="hub-location" value="column" checked> The dataset's own image column</label>
        <label class="d-block"><input type="radio" name="hub-location" value="env_root"> This server's configured split root (<code id="hub-env-root"></code>)</label>
        <label class="d-block"><input type="radio" name="hub-location" value="custom"> Another gs:// root:
          <input id="hub-root" class="form-control form-control-sm d-inline-block w-auto" placeholder="gs://bucket/prefix"></label>
        <small class="text-muted">Roots are read as <code>root/split/label/file</code>, and must be a bucket this server curates.</small>
      </fieldset>
      <fieldset class="mt-2"><legend class="form-label">Splits</legend><div id="hub-splits"></div></fieldset>
      <div class="hub-actions mt-3">
        <div>
          <label class="form-label" for="hub-project-name">New patch-only project</label>
          <input id="hub-project-name" class="form-control form-control-sm" maxlength="200">
          <select id="hub-project-mode" class="form-select form-select-sm mt-1"><option value="T3">T3</option><option value="T1">T1</option></select>
          <button id="hub-create-project" class="btn btn-sm btn-primary mt-2" type="button">Register and create project</button>
        </div>
        <div>
          <span class="form-label d-block">Or link to the current project</span>
          <label class="d-block small"><input id="hub-adopt-splits" type="checkbox"> Adopt the dataset's splits (never overrides a split from a CSV)</label>
          <button id="hub-link-preview" class="btn btn-sm btn-outline-primary mt-2" type="button">Register and preview link</button>
          <button id="hub-link-apply" class="btn btn-sm btn-outline-danger mt-2 d-none" type="button">Apply link</button>
        </div>
      </div>
    </div>
    <div id="hub-model-fields" class="d-none">
      <label class="form-label mt-2" for="hub-weight-file">Checkpoint</label>
      <select id="hub-weight-file" class="form-select form-select-sm"></select>
      <label class="form-label mt-2" for="hub-model-name">Name</label>
      <input id="hub-model-name" class="form-control form-control-sm" maxlength="200">
      <label class="d-block small mt-2"><input id="hub-model-trusted" type="checkbox"> I trust the source of these executable model weights</label>
      <button id="hub-register-model" class="btn btn-sm btn-primary mt-2" type="button">Register model</button>
    </div>
    <div id="hub-message" class="small mt-2" role="status"></div>
  </form>
</dialog>

<dialog id="hub-registry-dialog" class="curation-dialog hub-dialog">
  <div class="d-flex justify-content-between align-items-center mb-3">
    <h2>Registry</h2>
    <button class="btn btn-sm btn-outline-secondary icon-button" type="button" data-close-dialog="hub-registry-dialog" title="Close"><i class="bi bi-x-lg"></i></button>
  </div>
  <div id="hub-registry-list" class="hub-registry-list"></div>
</dialog>
```

After the `curation.js` script tag, add `<script src="/static/curation/hub.js"></script>`.

Expose the env split root to the page: in `main.py` `curation_page`'s context, add `"split_root": curation_settings.split_root()` (import `from curation import settings as curation_settings`). In the template, set `<code id="hub-env-root">{{ split_root }}</code>`.

- [ ] **Step 2: `static/curation/hub.js`**

```javascript
'use strict'
// Hugging Face registry: look up, register, create or link a project, register a model.
// A classic script after curation.js, so it shares that file's request/byId/escapeHtml/
// canDo/state/loadProjects/loadEvaluationCatalog.

const hub = { kind: 'dataset', preview: null, entry: null }

function hubMessage(text, isError = false) {
  const element = byId('hub-message')
  element.textContent = text
  element.classList.toggle('text-danger', isError)
}

function openHub(kind) {
  hub.kind = kind
  hub.preview = null
  hub.entry = null
  byId('hub-form').reset()
  byId('hub-title').textContent = kind === 'dataset' ? 'Dataset from Hugging Face' : 'Model from Hugging Face'
  byId('hub-preview').innerHTML = ''
  byId('hub-dataset-fields').classList.add('d-none')
  byId('hub-model-fields').classList.add('d-none')
  byId('hub-link-apply').classList.add('d-none')
  hubMessage('')
  byId('project-dialog').close()
  byId('hub-dialog').showModal()
}

async function hubLookup() {
  hubMessage('Looking up…')
  try {
    const preview = await request('/api/curation/hub/lookup', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: byId('hub-ref').value.trim(), kind: hub.kind }) })
    hub.preview = preview
    renderHubPreview(preview)
    hubMessage('')
  } catch (error) { hubMessage(error.message, true) }
}

function renderHubPreview(preview) {
  const facts = [
    `<a href="${escapeHtml(preview.url)}" target="_blank" rel="noreferrer"><code>${escapeHtml(preview.repo_id)}@${preview.revision.slice(0, 8)}</code></a>`,
    preview.task ? escapeHtml(preview.task) : null, preview.license ? `license ${escapeHtml(preview.license)}` : null,
    preview.matches_t3_baseline ? '<span class="badge text-bg-info">T3 v0 baseline commit</span>' : null,
    preview.existing_entry_id ? '<span class="badge text-bg-secondary">already registered</span>' : null,
  ].filter(Boolean)
  const warning = preview.runnable ? '' : `<p class="text-warning small mb-0"><i class="bi bi-exclamation-triangle"></i> ${escapeHtml(preview.not_runnable_reason)}</p>`
  byId('hub-preview').innerHTML = `<p class="mb-1">${facts.join(' · ')}</p>${warning}`
  if (preview.kind === 'dataset') {
    byId('hub-dataset-fields').classList.toggle('d-none', !preview.runnable)
    byId('hub-splits').innerHTML = Object.entries(preview.splits).map(([split, count]) =>
      `<label class="me-3"><input type="checkbox" name="hub-split" value="${escapeHtml(split)}" checked> ${escapeHtml(split)}${count == null ? '' : ` (${Number(count).toLocaleString()})`}</label>`).join('')
    byId('hub-project-name').value = preview.repo_id.split('/')[1]
    byId('hub-preview').insertAdjacentHTML('beforeend', `<p class="small text-muted mb-0">Columns: image <code>${escapeHtml(preview.column_map.image || '?')}</code>, label <code>${escapeHtml(preview.column_map.label || '?')}</code>, file <code>${escapeHtml(preview.column_map.file_name || '—')}</code>, split <code>${escapeHtml(preview.column_map.split || '—')}</code></p>`)
  } else {
    byId('hub-model-fields').classList.remove('d-none')
    byId('hub-weight-file').innerHTML = preview.weight_files.map(file => `<option>${escapeHtml(file)}</option>`).join('')
    byId('hub-weight-file').disabled = !preview.runnable
    byId('hub-model-trusted').disabled = !preview.runnable
    byId('hub-model-name').value = preview.repo_id.split('/')[1]
    byId('hub-register-model').textContent = preview.runnable ? 'Register model' : 'Add to catalog only'
  }
}

function hubLocation() {
  const mode = document.querySelector('input[name="hub-location"]:checked').value
  return mode === 'custom' ? { mode, root: byId('hub-root').value.trim() } : { mode }
}

async function hubRegisterDataset() {
  if (hub.entry) return hub.entry
  hubMessage('Registering and checking 5 sample patches…')
  hub.entry = await request('/api/curation/hub/datasets', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ref: byId('hub-ref').value.trim(), kind: 'dataset', location: hubLocation() }) })
  hubMessage(`Registered. ${hub.entry.sample?.found ?? '?'} of ${hub.entry.sample?.checked ?? '?'} sample patches found.`)
  return hub.entry
}

async function hubCreateProject() {
  try {
    const entry = await hubRegisterDataset()
    const splits = [...document.querySelectorAll('input[name="hub-split"]:checked')].map(input => input.value)
    const created = await request(`/api/curation/hub/datasets/${entry.id}/projects`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: byId('hub-project-name').value.trim(), dataset_mode: byId('hub-project-mode').value, splits }) })
    byId('hub-dialog').close()
    state.projectId = created.project.id
    await loadProjects()
  } catch (error) { hubMessage(error.message, true) }
}

async function hubWaitFor(projectId, operationId) {
  for (;;) {
    const operation = await request(`/api/curation/projects/${projectId}/imports/${operationId}`)
    if (!['queued', 'starting', 'running'].includes(operation.status)) return operation
    hubMessage(`Reading the dataset… ${operation.processed_rows.toLocaleString()} rows`)
    await new Promise(resolve => setTimeout(resolve, 1500))
  }
}

async function hubLink(dryRun) {
  if (!state.projectId) { hubMessage('Choose a project first', true); return }
  try {
    const entry = await hubRegisterDataset()
    const operation = await request(`/api/curation/hub/datasets/${entry.id}/links`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: state.projectId, adopt_splits: byId('hub-adopt-splits').checked, dry_run: dryRun }) })
    const done = await hubWaitFor(state.projectId, operation.id)
    if (done.status !== 'completed') { hubMessage(done.error_message || `Link ${done.status}`, true); return }
    const r = done.sources[0].result
    const summary = `${r.matched.toLocaleString()} matched · ${r.unmatched.toLocaleString()} not in this project · ${r.ambiguous} ambiguous · ${r.split_changes.toLocaleString()} split changes · ${r.kept_csv_split.toLocaleString()} CSV splits kept · ${r.already_linked.toLocaleString()} already linked`
    if (dryRun) {
      hubMessage(`Preview: ${summary}. Published versions keep their frozen splits; only the next version changes.`)
      byId('hub-link-apply').classList.remove('d-none')
    } else {
      hubMessage(`Linked: ${summary}.`)
      byId('hub-link-apply').classList.add('d-none')
      await loadProjects()
    }
  } catch (error) { hubMessage(error.message, true) }
}

async function hubRegisterModel() {
  try {
    const entry = await request('/api/curation/hub/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: byId('hub-ref').value.trim(), kind: 'model', weight_file: hub.preview?.runnable ? byId('hub-weight-file').value : null,
        name: byId('hub-model-name').value.trim() || null, trusted: byId('hub-model-trusted').checked }) })
    if (entry.classifier_id && byId('evaluation-worker-state')?.textContent === 'Worker online') {
      await request(`/api/curation/evaluation-models/${entry.classifier_id}/validate`, { method: 'POST' })
    }
    byId('hub-dialog').close()
    if (typeof loadEvaluationCatalog === 'function' && state.projectId) await loadEvaluationCatalog()
  } catch (error) { hubMessage(error.message, true) }
}

async function openRegistry() {
  byId('hub-registry-dialog').showModal()
  const list = byId('hub-registry-list')
  list.textContent = 'Loading…'
  try {
    const entries = await request('/api/curation/hub/entries')
    list.innerHTML = entries.length ? `<table class="table table-sm"><thead><tr><th>Repository</th><th>Kind</th><th>Task</th><th>Use</th><th>Registered</th></tr></thead><tbody>${entries.map(entry => `
      <tr><td><a href="${escapeHtml(entry.url)}" target="_blank" rel="noreferrer">${escapeHtml(entry.repo_id)}</a> <code>${entry.short_revision}</code>${entry.matches_t3_baseline ? ' <span class="badge text-bg-info">T3 v0</span>' : ''}</td>
      <td>${entry.kind}</td><td>${escapeHtml(entry.task || '—')}${entry.runnable ? '' : ` <small class="text-muted">(${escapeHtml(entry.not_runnable_reason || 'catalog only')})</small>`}</td>
      <td>${entry.kind === 'model' ? (entry.classifier_id ? 'classifier registered' : '—') : (entry.projects.map(p => escapeHtml(p.name)).join(', ') || '—')}</td>
      <td>${escapeHtml(entry.created_by_name || '')}<br><small>${new Date(entry.created_at).toLocaleString()}</small></td></tr>`).join('')}</tbody></table>`
      : '<p class="text-muted">Nothing registered yet. Leads add datasets from "New project" and models from the confusion matrix view.</p>'
  } catch (error) { list.textContent = error.message }
}

byId('project-from-hub').addEventListener('click', () => openHub('dataset'))
byId('model-from-hub').addEventListener('click', () => openHub('model'))
byId('hub-registry-button').addEventListener('click', openRegistry)
byId('hub-lookup').addEventListener('click', hubLookup)
byId('hub-ref').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); hubLookup() } })
byId('hub-create-project').addEventListener('click', hubCreateProject)
byId('hub-link-preview').addEventListener('click', () => hubLink(true))
byId('hub-link-apply').addEventListener('click', () => hubLink(false))
byId('hub-register-model').addEventListener('click', hubRegisterModel)
document.querySelectorAll('[data-close-dialog]').forEach(button =>
  button.addEventListener('click', () => byId(button.dataset.closeDialog).close()))
```

- [ ] **Step 3: CSS** (append to `static/curation.css`)

```css
/* Hugging Face registry dialogs */
.hub-dialog { width: min(760px, calc(100vw - 32px)); }
.hub-preview { margin-top: 10px; }
.hub-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.hub-registry-list { max-height: 70vh; overflow: auto; }
@media (max-width: 640px) { .hub-actions { grid-template-columns: 1fr; } }
```

- [ ] **Step 4: Check it in the browser**

Start the dev server (`docker compose --profile dev up --build dev`, which now uses Oracle). Sign in as a lead and open `/curation`. Use the `run` skill or the Chrome tools to check:
1. **Registry** opens and says "Nothing registered yet".
2. **New project → From Hugging Face…**: paste the dataset link and click **Look up**. The preview shows `@b47bff4e`, image-classification, the "T3 v0 baseline commit" badge, three splits with counts, and the detected columns.
3. Untick train and val, then **Register and create project**. The new project is selected, and the import progress bar runs to 56,864 items.
4. Switch to "NOAA Pacific T3" and open the dialog again. **Register and preview link** shows the counts, then **Apply link** shows "Linked".
5. **Confusion matrix → From Hugging Face…** with the model link: pick `weights/best.pt`, tick trust, register. The classifier appears in the model list, validating if the worker is online.
6. **Registry** now lists both entries with their linked project and classifier.
7. With the worker running (`docker compose -f docker-compose.yml -f docker-compose.evaluation.yml up -d`), wait until the HF classifier's status is **ready**. Validation must finish before **Evaluate** accepts it.
8. Choose the classifier in the Evaluate form. The splits change to val and test with train unticked, and a note says the model was trained on this dataset (see the `hub.js` addition below).
9. Run the evaluation on v1 of the HF test-split project. Progress counts up, and the run finishes with "0 missing patches".
10. Switch the matrix source between the new run and **T3 v0 test** and compare the two.
11. No console errors (`read_console_messages`, pattern `Error`).

**`hub.js` addition for step 8.** Append this so a model trained on the project's dataset defaults to val+test and shows a warning about labels it doesn't know:

```javascript
let hubEntriesCache = null
byId('evaluation-model').addEventListener('change', async event => {
  const classifierId = event.target.value
  const note = byId('evaluation-hub-note') || byId('evaluation-run-form').insertAdjacentElement('afterend',
    Object.assign(document.createElement('div'), { id: 'evaluation-hub-note', className: 'small text-muted' }))
  note.textContent = ''
  if (!classifierId || !state.projectId) return
  try {
    hubEntriesCache = hubEntriesCache || await request('/api/curation/hub/entries')
    const model = hubEntriesCache.find(entry => entry.kind === 'model' && entry.classifier_id === classifierId)
    const project = await request(`/api/curation/projects/${state.projectId}`)
    const hubRepos = [project.source_config?.hub, ...(project.source_config?.hub_links || [])].filter(Boolean)
    const trainedOn = new Set(model?.trained_on || [])
    const overlap = hubRepos.find(link => trainedOn.has(link.repo_id))
    if (overlap) {
      document.querySelectorAll('input[name="evaluation-splits"]').forEach(box => { box.checked = box.value !== 'train' })
      note.textContent = `This model was trained on ${overlap.repo_id}; scoring its training split only measures memorisation, so val and test are selected.`
      const entryId = overlap.entry_id
      const labels = await request(`/api/curation/hub/datasets/${entryId}/labels?dataset_mode=${encodeURIComponent(project.dataset_mode)}&classifier_id=${classifierId}`)
      const unknown = labels.filter(row => row.in_model === false)
      if (unknown.length) note.textContent += ` ${unknown.length} label(s) are not classes of this model (${unknown.slice(0, 5).map(row => row.effective).join(', ')}…); the run will be refused until they are mapped or excluded.`
    }
  } catch (error) { note.textContent = error.message }
})
```

This needs one more summary field. In Task 5's `registry.summary`, add `"trained_on": list(entry.card.get("datasets") or []) if entry.kind == "model" else [],`. Also add a test: assert that `trained_on` equals `[DATASET]` for the registered model in `test_register_model_streams_the_chosen_weights`, by reading `/hub/entries?kind=model`.

- [ ] **Step 5: Commit**

```bash
git add static/curation/hub.js templates/curation.html static/curation.css main.py
git commit -m "Registry, dataset and model dialogs in the curation page

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Docs, live test, and the full suites on SQLite and Oracle

**Files:**
- Modify: `docs/CURATION.md` (new "Hugging Face registry" section)
- Modify: `.env.example` (the four `CURATION_HF_*` / `CURATION_HUB_*` variables)
- Modify: `tests/test_noaa_live.py` (one opt-in live test)

- [ ] **Step 1: Live test** (append to `tests/test_noaa_live.py`, which is already skipped unless `OCEANFRAME_LIVE_TESTS=1`)

```python
def test_registry_resolves_the_real_noaa_repositories():
    from curation import evaluation, huggingface, hub_rows

    dataset = huggingface.repo_info(huggingface.parse_ref(
        "https://huggingface.co/datasets/NMFS-OSI/noaa-pacific-benthic-cover-t3"))
    model = huggingface.repo_info(huggingface.parse_ref(
        "https://huggingface.co/NMFS-OSI/yolo11m-cls-noaa-pacific-benthic-cover-t3"))
    # main may move on; the pinned baseline commits must still resolve
    pinned_dataset = huggingface.repo_info(huggingface.HubRef(
        "dataset", dataset.repo_id, evaluation.EXPECTED_DATASET_REVISION))
    pinned_model = huggingface.repo_info(huggingface.HubRef(
        "model", model.repo_id, evaluation.EXPECTED_MODEL_REVISION))
    assert pinned_dataset.sha == evaluation.EXPECTED_DATASET_REVISION
    assert pinned_model.sha == evaluation.EXPECTED_MODEL_REVISION
    assert hub_rows.detect_columns(hub_rows.features(pinned_dataset.card))["image"] == "gcs_uri"
    assert "weights/best.pt" in {entry["path"] for entry in pinned_model.files}
```

(Put the module's existing skip marker or `pytestmark` in front of it. Check how the file gates its tests before adding.)

- [ ] **Step 2: Docs**

Add a "Hugging Face registry" section to `docs/CURATION.md`:
- What it is: datasets and models pinned to a commit, public only, leads register.
- Datasets:
  - The column map is detected automatically.
  - The three image locations, and that roots must be curated buckets.
  - New patch-only project vs. link to an existing project. Linking previews first, writes audit events, never overrides CSV splits, and leaves published versions untouched.
- Models:
  - Pick a `.pt`; weights are registered through the same trusted-classifier path and validated in the isolated worker.
  - Other tasks are catalogue-only.
- Versions record `derived_from`.
- Environment variables, also added to `.env.example`:
  - `CURATION_HF_ALLOWED_ORGS`, `CURATION_HF_ENDPOINT`, `CURATION_HUB_CACHE`, `CURATION_HUB_MAX_BYTES`
  - The web container needs outbound HTTPS to huggingface.co; the evaluation worker stays offline.

- [ ] **Step 3: Run everything**

- `docker compose --profile test run --rm --build test`. Expected: the whole offline suite passes.
- `docker compose -f docker-compose.stack.yml --profile test run --rm --build test-oracle`. Expected: passes on Oracle Free, including `test_registry.py` and `test_appdb_backend.py`, where the strict-mode test skips.
- `docker compose --profile live run --rm --build test-live sh -c "pip install -q -r requirements-dev.txt && OCEANFRAME_LIVE_TESTS=1 python -m pytest -q tests/test_noaa_live.py -k registry"`. Expected: PASS (needs network).

If any Oracle-only failure appears, use the superpowers:systematic-debugging skill before changing code. Likely causes are `''` stored as NULL, JSON CLOB comparisons, and identity columns.

- [ ] **Step 4: Reproduce the published baseline (the acceptance check that matters most)**

The registered commits are exactly the pinned T3 v0 baseline: dataset `b47bff4e`, model `77e10ec1`, and the same 56,864-image test split. So, on the deployed stack (worker on, GPU if available):
1. Register both repos. Create a project from the HF dataset's **test split only**, publish v1, and do not edit anything.
2. Evaluate the HF model on v1 with split `test` and image size 224.
3. Compare with `docs/CURATION.md`'s baseline: **top-1 59.84%, top-5 85.91%, macro F1 53.85%**. Allow ±0.3 points for torch/ultralytics version drift.
4. If the numbers are off, preprocessing or labels differ. Check `imgsz`, the PNG pass-through, and whether `current_label` was remapped by the taxonomy. **Don't trust val numbers until test reproduces.**
5. Write the measured numbers, staging time and wall time into `docs/CURATION.md` next to the baseline.

- [ ] **Step 4: Commit**

```bash
git add docs/CURATION.md .env.example tests/test_noaa_live.py
git commit -m "Document the Hugging Face registry; live check of the NOAA repositories

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
