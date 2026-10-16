# Curation P3a: Patch Editing, Navigation and Missing Images, Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the source image, curators can:
- move, resize, remove and restore patches;
- bulk-add patches around a patch, then fine-tune and save them.

Source images and patches are served through a disk cache. Missing source images are detected, hidden by default and reported. Navigation between the views gains back links and deep links. The P2 legacy-version gap gets a clear error.

**Architecture:**
- **New server modules:**
  - `curation/image_cache.py` caches every cloud read;
  - `curation/editing.py` applies batch edits and suggestions;
  - `curation/availability.py` runs a background operation through the existing import pipeline.
- **Item model:** gains two boolean columns, `removed` and `source_missing`, added by migration.
- **UI:** the editor lives in a new classic script, `static/curation/editor.js`, loaded after `collab.js`. `curation.js` gains navigation and filter changes.

**Tech Stack:** FastAPI, SQLAlchemy 2 (Oracle in deployment, SQLite in tests), Pillow, vanilla JS, pytest in Docker.

**Spec:** `docs/superpowers/specs/2026-10-12-curation-p3a-editing-design.md`

## Global Constraints

- **Schema:**
  - Changes to existing tables only through new append-only entries in `MIGRATIONS` (`appdb/bootstrap.py`).
  - `Str(n)`/`JSONText` columns; every identifier at most 30 characters.
  - Never put the same bound literal in both SELECT and GROUP BY (Oracle).
- **Crops are never written to the source buckets.** Patches are cut from source images and cached on local disk only.
- **Permissions (P2):**
  - Geometry edits, batch edits, suggestions and the availability check need `members.require_project_curator`.
  - Reads stay open to signed-in users.
  - The UI gates on `canDo('project-curator')`.
- **Revisions (P1/P2):**
  - Every edit to an existing item goes through `service._record_change`, which bumps the revision and writes an event that a change set can include.
  - Edits are rejected or skipped on a stale `expected_revision`.
- **Event types:**
  - Geometry, remove and restore edits write `item_updated`.
  - Added points write `manual_point_created`.
  - Both are already committable in P2.
- **Limits:**
  - `crop_size` 32–1024.
  - A point's centre must be inside the image (`0 <= row_px < height`, `0 <= column_px < width`).
  - At most 200 edits plus 200 added points per batch.
  - Grid size 3 or 5.
  - Spacing 32–512 px.
- **Tests:**
  - Docker only, with `--build`. **TEST `<target>`** means:

    ```bash
    MSYS_NO_PATHCONV=1 docker compose --env-file "$TMP/of-dummy.env" run --build --rm test sh -c "pip install -q --no-cache-dir -r requirements-dev.txt && python -m pytest -q <target>"
    ```

  - Each new DB-backed test file is added to the `test-oracle` list in `docker-compose.stack.yml` in the task that creates it, and run once on Oracle:

    ```bash
    docker compose -f docker-compose.stack.yml --profile test run --build --rm test-oracle sh -c "pip install --quiet --no-cache-dir --user -r requirements-dev.txt && python -m pytest -q <files>"
    ```

- **JS syntax check** after any JS change. Drop `editor.js` from the line until it exists:

  ```bash
  MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD/static:/s:ro" node:20-alpine sh -c 'for f in curation hub collab editor; do node --check /s/curation/$f.js || exit 1; done; echo SYNTAX-OK'
  ```

- **Page integrity:**
  - Every `byId('…')` in `curation.js`, `collab.js` and `editor.js` must exist in `templates/curation.html`. Extend the integrity test's file list to include `editor.js` in Task 8.
  - Data text inserted into HTML goes through `escapeHtml()`.
- **Docker safety:** never run `docker compose … down`, never pass `-v`, never touch `.env`, `.env copy` or `docs/*.png`.
- **Commits:** end each with a `Co-Authored-By: <the model> <noreply@anthropic.com>` trailer.

## Review Focus

1. A point dragged to the image edge, or past it. The centre is clamped to the image in the browser, and the server refuses a centre outside the image with 422. Test in Task 3.
2. Two curators editing points on the same image at once. Each stale item is skipped and reported, and the rest apply. Test in Task 3.
3. A moved patch whose old `crop_uri` still exists. The preview and the export show the newly cut patch, never the old file. Test in Task 4.
4. A source image missing from the bucket while editing. Edits and suggestions return 422 "Source image not in the bucket", not a 500. Test in Task 3.
5. A cache size cap smaller than a single image. The image is still served, and the cache evicts down to the cap without deleting a file mid-write. Test in Task 1.

---

### Task 1: Image cache

**Files:**
- Create: `curation/image_cache.py`
- Modify: `curation/settings.py` (`image_cache_dir()`, `image_cache_mb()`)
- Modify: `curation/images.py` (`crop_from_source` uses the cache)
- Modify: `curation/scoring.py` (reads the source through the cache)
- Modify: `routers/curation.py` (the image proxy `GET /image` reads through the cache)
- Modify: `.env.example` (`CURATION_IMAGE_CACHE`, `CURATION_IMAGE_CACHE_MB`)
- Test: `tests/test_image_cache.py` (no DB, so it is not on the Oracle list)

**Interfaces — Produces:**
- `image_cache.read_source(uri: str) -> bytes`
- `image_cache.dimensions(uri: str) -> tuple[int, int]` (width, height)
- `image_cache.patch(uri: str, row_px: int, column_px: int, crop_size: int) -> bytes` (a 224×224 PNG)
- `image_cache.clear()` (tests only)

All three raise `FileNotFoundError` for a missing object.

- [ ] **Step 1: Write the failing tests** in `tests/test_image_cache.py`

```python
"""The disk cache in front of cloud image reads."""
from __future__ import annotations

import io
import threading

import pytest
from PIL import Image


def _jpeg(size=(640, 480), color=(40, 120, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    from curation import image_cache, storage

    monkeypatch.setenv("CURATION_IMAGE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("CURATION_IMAGE_CACHE_MB", "50")
    calls = []
    payload = _jpeg()

    def read(uri):
        calls.append(uri)
        if uri.endswith("missing.jpg"):
            raise FileNotFoundError(uri)
        return payload

    monkeypatch.setattr(storage, "read_bytes", read)
    image_cache.clear()
    return image_cache, calls


def test_a_second_read_comes_from_disk(cache):
    image_cache, calls = cache
    first = image_cache.read_source("gs://bucket/a.jpg")
    assert image_cache.read_source("gs://bucket/a.jpg") == first
    assert calls == ["gs://bucket/a.jpg"]


def test_dimensions_and_patches(cache):
    image_cache, calls = cache
    assert image_cache.dimensions("gs://bucket/a.jpg") == (640, 480)
    patch = image_cache.patch("gs://bucket/a.jpg", 100, 200, 160)
    with Image.open(io.BytesIO(patch)) as image:
        assert image.size == (224, 224) and image.format == "PNG"
    assert image_cache.patch("gs://bucket/a.jpg", 100, 200, 160) == patch
    assert image_cache.patch("gs://bucket/a.jpg", 101, 200, 160) != b""  # a moved point is a new key
    assert calls == ["gs://bucket/a.jpg"]


def test_a_missing_image_is_not_cached(cache):
    image_cache, calls = cache
    for _ in range(2):
        with pytest.raises(FileNotFoundError):
            image_cache.read_source("gs://bucket/missing.jpg")
    assert calls == ["gs://bucket/missing.jpg"] * 2


def test_concurrent_first_reads_download_once(cache, monkeypatch):
    image_cache, calls = cache
    from curation import storage

    gate = threading.Event()
    original = storage.read_bytes

    def slow(uri):
        gate.wait(2)
        return original(uri)

    monkeypatch.setattr(storage, "read_bytes", slow)
    threads = [threading.Thread(target=image_cache.read_source, args=("gs://bucket/b.jpg",)) for _ in range(5)]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join()
    assert calls == ["gs://bucket/b.jpg"]


def test_the_cap_evicts_least_recently_used(cache, monkeypatch, tmp_path):
    image_cache, _ = cache
    monkeypatch.setenv("CURATION_IMAGE_CACHE_MB", "0")  # smaller than one image
    data = image_cache.read_source("gs://bucket/c.jpg")
    assert data  # still served even though it cannot stay
    image_cache.read_source("gs://bucket/d.jpg")
    leftovers = [p for p in (tmp_path / "cache").rglob("*") if p.is_file()]
    assert not any(p.name.endswith(".part") for p in leftovers)
    assert sum(p.stat().st_size for p in leftovers) <= len(data)  # evicted down to (at most) the newest file
```

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_image_cache.py`
Expected: FAIL with `ModuleNotFoundError: curation.image_cache`.

- [ ] **Step 3: Settings.** Add to `curation/settings.py`, after `patch_cache_dir()`:

```python
def image_cache_dir() -> Path:
    """CURATION_IMAGE_CACHE: local disk cache of source images and cut patches."""
    return Path(os.getenv("CURATION_IMAGE_CACHE", str(library_settings.DATA_DIR / "curation_cache")))


def image_cache_mb() -> int:
    """CURATION_IMAGE_CACHE_MB: total size cap for that cache (default 2048)."""
    try:
        return max(0, int(os.getenv("CURATION_IMAGE_CACHE_MB", "2048")))
    except ValueError:
        return 2048
```

- [ ] **Step 4: Create `curation/image_cache.py`**

```python
"""
Disk cache in front of cloud image reads: source images and cut patches.

Every curation read of a source image goes through here: the image proxy, crop
previews, neighbourhood classification, suggestions and image-folder export.
Dragging a patch around or paging through points therefore re-reads nothing
from GCS. Patches are keyed by (image, row, column, size), so a moved patch is
a new key and never shows its old crop. Nothing is ever written back to a bucket.
Bounded by CURATION_IMAGE_CACHE_MB, oldest-used first; a miss is never cached.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import threading
from pathlib import Path

from PIL import Image

from curation import settings, storage

_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_dimensions: dict[str, tuple[int, int]] = {}
_writes = 0
EVICT_EVERY = 20  # check the cap every N writes (and whenever the cap is 0)


def _root() -> Path:
    return settings.image_cache_dir()


def _key(uri: str) -> str:
    return hashlib.sha1(uri.encode("utf-8")).hexdigest()


def _lock_for(name: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(name, threading.Lock())


def _hit(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        os.utime(path, None)  # mark as recently used
    except OSError:
        pass
    return data


def _write(path: Path, payload: bytes) -> None:
    global _writes
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    temporary.write_bytes(payload)
    os.replace(temporary, path)
    with _guard:
        _writes += 1
        due = _writes % EVICT_EVERY == 0 or settings.image_cache_mb() == 0
    if due:
        _evict(keep=path)


def _evict(keep: Path | None = None) -> None:
    cap = settings.image_cache_mb() * 1024 * 1024
    files = [p for p in _root().rglob("*") if p.is_file() and not p.name.endswith(".part")]
    total = sum(p.stat().st_size for p in files)
    for path in sorted(files, key=lambda p: p.stat().st_mtime):
        if total <= cap:
            break
        if keep is not None and path == keep and total - path.stat().st_size <= cap:
            continue
        size = path.stat().st_size
        try:
            path.unlink()
            total -= size
        except FileNotFoundError:
            pass


def read_source(uri: str) -> bytes:
    path = _root() / "sources" / _key(uri)
    data = _hit(path)
    if data is not None:
        return data
    with _lock_for(path.name):
        data = _hit(path)
        if data is not None:
            return data
        data = storage.read_bytes(uri)  # FileNotFoundError propagates; nothing cached for a miss
        _write(path, data)
        return data


def dimensions(uri: str) -> tuple[int, int]:
    size = _dimensions.get(uri)
    if size is None:
        with Image.open(io.BytesIO(read_source(uri))) as image:  # reads the header only
            size = _dimensions[uri] = image.size
    return size


def patch(uri: str, row_px: int, column_px: int, crop_size: int) -> bytes:
    path = _root() / "patches" / f"{_key(uri)}_{row_px}_{column_px}_{crop_size}.png"
    data = _hit(path)
    if data is not None:
        return data
    with Image.open(io.BytesIO(read_source(uri))) as source:
        source = source.convert("RGB")
        half = crop_size // 2
        crop = source.crop((column_px - half, row_px - half, column_px + half, row_px + half))
        if crop.size != (224, 224):
            crop = crop.resize((224, 224), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        crop.save(output, format="PNG")
    data = output.getvalue()
    _write(path, data)
    return data


def clear() -> None:
    """Empty the cache (tests)."""
    with _guard:
        _dimensions.clear()
        _locks.clear()
    shutil.rmtree(_root(), ignore_errors=True)
```

In `_evict`, the `keep` rule means the file just written survives when it alone fits the cap. When even that file exceeds the cap (cap 0), it is still removed after being served, since the caller already has the bytes.

- [ ] **Step 5: Wire the callers.**
- **`curation/images.py` `crop_from_source(item)`:** replace its body with `return image_cache.patch(item.source_image_uri, item.row_px, item.column_px, item.crop_size)`, and add `from curation import image_cache` to the imports.
- **`curation/scoring.py` `classify_neighborhood`:** replace `storage.read_bytes(source_image_uri)` with `image_cache.read_source(source_image_uri)`.
- **`routers/curation.py` image proxy (`GET /image`):** read through `image_cache.read_source(uri)` instead of `storage.read_bytes(uri)`. Keep its allow-list check and EXIF handling.
- **`.env.example`:** add, next to the other `CURATION_` cache entries:

```
# Local disk cache of source images and cut patches (never written to buckets).
# CURATION_IMAGE_CACHE=/data/library/curation_cache
# CURATION_IMAGE_CACHE_MB=2048
```

- [ ] **Step 6: Run the tests**

Run: TEST `tests/test_image_cache.py tests/test_curation.py`
Expected: PASS. Existing tests monkeypatch `storage.read_bytes`, which the cache calls, so they keep working. If a test depended on reads not being cached between tests, add an autouse fixture in `tests/conftest.py` that points `CURATION_IMAGE_CACHE` at `tmp_path` and calls `image_cache.clear()`.

- [ ] **Step 7: Commit**

```bash
git add curation/image_cache.py curation/settings.py curation/images.py curation/scoring.py routers/curation.py .env.example tests/test_image_cache.py tests/conftest.py
git commit -m "Disk cache for source images and cut patches; every curation image read goes through it"
```

---

### Task 2: `removed` and `source_missing` columns; single-item geometry, remove and restore

**Files:**
- Modify: `curation/models.py` (two `CurationItem` columns)
- Modify: `appdb/bootstrap.py` (migration `20261012_01`)
- Modify: `curation/schemas.py`:
  - `CurationItemUpdate` gains geometry and `removed`;
  - `CurationItemResponse` gains `removed` and `source_missing`.
- Modify: `curation/service.py` (`_state`, `update_item`, `item_query`)
- Modify: `routers/curation.py` (`_filters` gains `removed` and `source_missing`)
- Test: `tests/test_editing.py` (new; add it to `test-oracle`)

**Interfaces — Produces:**
- `CurationItem.removed: bool` and `CurationItem.source_missing: bool` (default False)
- `service._state(item)` now includes `row_px`, `column_px`, `crop_size` and `removed`
- `service.check_point_bounds(uri, row_px, column_px) -> None`, raising 422
- Item filters: `removed=hide|only|any` (server default `hide`) and `source_missing=hide|only|any` (server default `any`)

- [ ] **Step 1: Write the failing tests** in `tests/test_editing.py`

```python
"""Editing patches: geometry, remove/restore, batch edits and suggestions."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from test_curation import P, _add, _item_payload, _project, _test_buckets, client, db_session  # noqa: F401


@pytest.fixture(autouse=True)
def _images(monkeypatch, tmp_path):
    """Every source image is a 1000×800 JPEG; anything named *missing* is absent."""
    from curation import image_cache, storage

    buffer = io.BytesIO()
    Image.new("RGB", (1000, 800), (30, 140, 90)).save(buffer, format="JPEG")
    payload = buffer.getvalue()

    def read(uri):
        if "missing" in uri:
            raise FileNotFoundError(uri)
        return payload

    monkeypatch.setenv("CURATION_IMAGE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(storage, "read_bytes", read)
    image_cache.clear()


def _point(client, pid, key="e1", label="ACBR", row=120, col=240, image="img-1"):
    payload = {**_item_payload(key, label), "row_px": row, "column_px": col,
               "source_image_uri": f"gs://nmfs/images/{image}.jpg"}
    return _add(client, pid, payload)


def test_move_and_resize_a_point(client):
    pid = _project(client, "Geometry")["id"]
    item = _point(client, pid)
    moved = client.patch(f"{P}/projects/{pid}/items/{item['id']}", json={
        "expected_revision": item["revision"], "row_px": 300, "column_px": 500, "crop_size": 160})
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert (body["row_px"], body["column_px"], body["crop_size"]) == (300, 500, 160)
    events = client.get(f"{P}/projects/{pid}/activity").json()
    latest = events[0] if isinstance(events, list) else events["events"][0]
    assert latest["previous_value"]["row_px"] == 120 and latest["new_value"]["row_px"] == 300


def test_points_must_stay_inside_the_image(client):
    pid = _project(client, "Bounds")["id"]
    item = _point(client, pid)
    out = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                       json={"expected_revision": item["revision"], "row_px": 800, "column_px": 10})
    assert out.status_code == 422 and "inside the image" in out.json()["detail"]
    assert client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                        json={"expected_revision": item["revision"], "crop_size": 2000}).status_code == 422


def test_a_missing_source_image_refuses_geometry_edits_clearly(client):
    pid = _project(client, "Missing source")["id"]
    item = _point(client, pid, image="missing-1")
    out = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                       json={"expected_revision": item["revision"], "row_px": 10, "column_px": 10})
    assert out.status_code == 422 and "not in the bucket" in out.json()["detail"]


def test_remove_hides_and_restore_brings_back(client):
    pid = _project(client, "Remove")["id"]
    item = _point(client, pid)
    removed = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                           json={"expected_revision": item["revision"], "removed": True}).json()
    assert (removed["removed"], removed["included"]) == (True, False)
    assert client.get(f"{P}/projects/{pid}/items").json()["total"] == 0
    assert client.get(f"{P}/projects/{pid}/items?removed=only").json()["total"] == 1
    restored = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                            json={"expected_revision": removed["revision"], "removed": False}).json()
    assert (restored["removed"], restored["included"]) == (False, True)
    assert client.get(f"{P}/projects/{pid}/items").json()["total"] == 1


def test_patch_only_items_have_no_geometry_to_edit(client, db_session):
    from curation.models import CurationItem

    pid = _project(client, "Patch only")["id"]
    item = _point(client, pid)
    row = db_session.get(CurationItem, item["id"])
    row.row_px = row.column_px = None
    db_session.commit()
    out = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                       json={"expected_revision": item["revision"], "crop_size": 160})
    assert out.status_code == 422 and "patch" in out.json()["detail"].lower()
```

Check the `/activity` response shape (`grep -n '"/projects/{project_id}/activity"' -A12 routers/curation.py`) and the `items` page key (`total`). Then adjust the two lookups. Also confirm `CurationItemCreate` accepts `row_px`/`column_px`; `_item_payload` already sets them.

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_editing.py`
Expected: FAIL. Geometry fields are ignored, and there is no `removed`.

- [ ] **Step 3: Model and migration.**

In `curation/models.py`, on `CurationItem` after `included`:

```python
    # Taken out of the dataset on the source image; restorable, never deleted.
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    # The source image is not in its bucket (curation/availability.py).
    source_missing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
```

Import `false` from `sqlalchemy`.

In `appdb/bootstrap.py`, add before `MIGRATIONS` and append the entry:

```python
def _item_flags(conn: Connection) -> None:
    """curation_items.removed / source_missing on databases created before P3a."""
    from curation.models import CurationItem

    existing = {c["name"] for c in inspect(conn).get_columns("curation_items")}
    for name in ("removed", "source_missing"):
        if name not in existing:
            column_type = CurationItem.__table__.c[name].type.compile(dialect=conn.dialect)
            conn.execute(text(f"ALTER TABLE curation_items ADD {name} {column_type} DEFAULT 0 NOT NULL"))
```

```python
    ("20261012_01", "Curation items: removed and source_missing flags", _item_flags),
```

On Oracle, `ADD … DEFAULT 0 NOT NULL` is a metadata-only change, even on about 1M rows. Add a test in `tests/test_collab_schema.py` style. Put it in `tests/test_editing.py`: drop both columns (SQLite DROP COLUMN), run `_item_flags(conn)`, and assert both are back with value 0. Restore the columns afterwards, as the P2 upgrade test does.

- [ ] **Step 4: Schemas.**
- `CurationItemUpdate`:
  - `expected_revision` stays required;
  - add `row_px: Optional[int] = Field(default=None, ge=0)`, `column_px: Optional[int] = Field(default=None, ge=0)`, `crop_size: Optional[int] = Field(default=None, ge=32, le=1024)` and `removed: Optional[bool] = None`.
- `CurationItemResponse`: add `removed: bool = False` and `source_missing: bool = False`.

- [ ] **Step 5: Service.** In `curation/service.py`:

```python
def _state(item: CurationItem) -> dict:
    return {
        "current_label": item.current_label, "included": bool(item.included),
        "review_state": item.review_state, "revision": item.revision,
        "row_px": item.row_px, "column_px": item.column_px, "crop_size": item.crop_size,
        "removed": bool(item.removed),
    }


def check_point_bounds(uri: str, row_px: int, column_px: int) -> None:
    """A point's centre must lie on its source image."""
    from curation import image_cache  # noqa: PLC0415

    try:
        width, height = image_cache.dimensions(uri)
    except FileNotFoundError as exc:
        raise _http(422, "Source image not in the bucket — its points cannot be moved or added") from exc
    if not (0 <= row_px < height and 0 <= column_px < width):
        raise _http(422, f"The point must be inside the image ({width}×{height})")
```

In `update_item`, after the existing label, included and review blocks:

```python
    geometry = {k: getattr(request, k) for k in ("row_px", "column_px", "crop_size") if getattr(request, k) is not None}
    if geometry:
        if item.row_px is None or item.column_px is None:
            raise _http(422, "This item is a patch with no source coordinates; its geometry cannot be edited")
        row, column = geometry.get("row_px", item.row_px), geometry.get("column_px", item.column_px)
        if "row_px" in geometry or "column_px" in geometry:
            check_point_bounds(item.source_image_uri, row, column)
        item.row_px, item.column_px = row, column
        item.crop_size = geometry.get("crop_size", item.crop_size)
        item.metadata_json = {**(item.metadata_json or {}), "geometry_edited": True}
    if request.removed is not None:
        set_removed(item, request.removed)
```

The new helper, used by Task 3 too:

```python
def set_removed(item: CurationItem, removed: bool) -> None:
    item.removed = removed
    item.included = not removed
    if removed:
        item.review_state = "flagged"
```

Notes:
- `update_item`'s early guard that rejects requests with nothing to change, if it exists, must count the new fields.
- `_record_change` makes `test_informed` depend on label or inclusion; leave that as is.

In `item_query`, add the parameters `removed: Optional[str] = None` and `source_missing: Optional[str] = None`, and before the `return`:

```python
    for column, mode in ((CurationItem.removed, removed), (CurationItem.source_missing, source_missing)):
        if mode == "hide":
            query = query.filter(column == False)  # noqa: E712
        elif mode == "only":
            query = query.filter(column == True)  # noqa: E712
```

- [ ] **Step 6: Router filters.** In `routers/curation.py` `_filters`, add `removed: str = Query("hide", pattern="^(hide|only|any)$")` and `source_missing: str = Query("any", pattern="^(hide|only|any)$")`, and pass both into the returned dict. Check that every route using `Depends(_filters)` forwards `**filters` to `item_query` or to functions that accept the keys. `get_facets` pops `item_ids`; make sure `service.facets` accepts or ignores the two new keys. If a function takes an explicit parameter list, add them.

- [ ] **Step 7: Oracle list and run**

Add `tests/test_editing.py` to `test-oracle`. Run TEST `tests/test_editing.py tests/test_curation.py`, then the full suite, then the Oracle command for `tests/test_editing.py tests/test_curation.py`. Expected: PASS. Any existing test that counted items must still pass, because nothing is removed by default.

- [ ] **Step 8: Commit**

```bash
git add curation/models.py appdb/bootstrap.py curation/schemas.py curation/service.py routers/curation.py tests/test_editing.py docker-compose.stack.yml
git commit -m "Points can be moved, resized, removed and restored; removed and missing-source flags as columns"
```

---

### Task 3: Batch edits and suggestions on a source image

**Files:**
- Create: `curation/editing.py`
- Modify: `curation/service.py` (factor the point-adding logic out of `create_manual_point` into `_add_point`)
- Modify: `curation/scoring.py` (a `classify_points` helper)
- Modify: `curation/schemas.py` (`SourceEditBatch`, `GeometryEdit`, `NewPoint`, `SuggestRequest`)
- Modify: `routers/curation.py` (two routes; `width`/`height` on the source-image detail)
- Test: `tests/test_editing.py` (append)

**Interfaces — Produces:**
- `POST /api/curation/projects/{id}/source-images/{anchor_item_id}/edits` → `{operation_id, applied, added: [item], conflicts: [{id, revision, updated_by_name}]}`
- `POST …/suggest` → `{candidates: [{row_px, column_px, crop_size, hints?}], model_available}`
- `GET …/source-images/{first_item_id}` gains `width` and `height`. Both are null when the image is missing; the detail then also carries `source_missing: true`.
- `service._add_point(db, project, anchor, *, row_px, column_px, crop_size, label, user, reason=None, operation_id=None) -> CurationItem` adds the point without committing.
- `scoring.classify_points(uri, points: list[tuple[int,int]], crop_size, model_name) -> list[list[dict]]` returns the top predictions per point. It raises `HTTPException` 501 or 404 when no model is available.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_editing.py`)

```python
def _image_items(client, pid):
    a = _point(client, pid, key="b1", label="ACBR", row=100, col=100)
    b = _point(client, pid, key="b2", label="POCS", row=400, col=600)
    other = _point(client, pid, key="b3", label="SAND", image="img-2")
    return a, b, other


def test_a_batch_moves_resizes_removes_and_adds(client):
    pid = _project(client, "Batch")["id"]
    a, b, _ = _image_items(client, pid)
    response = client.post(f"{P}/projects/{pid}/source-images/{a['id']}/edits", json={
        "reason": "fine-tune ACBR", "edits": [
            {"op": "move", "item_id": a["id"], "expected_revision": a["revision"], "row_px": 110, "column_px": 105},
            {"op": "resize", "item_id": b["id"], "expected_revision": b["revision"], "crop_size": 320},
        ],
        "added": [{"row_px": 150, "column_px": 150, "crop_size": 224, "label": "ACBR"},
                  {"row_px": 50, "column_px": 50, "crop_size": 224, "label": "ACBR"}]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == 2 and len(body["added"]) == 2 and body["conflicts"] == []
    detail = client.get(f"{P}/projects/{pid}/source-images/{a['id']}").json()
    assert (detail["width"], detail["height"]) == (1000, 800)
    by_id = {item["id"]: item for item in detail["items"]}
    assert (by_id[a["id"]]["row_px"], by_id[b["id"]]["crop_size"]) == (110, 320)
    assert len(detail["items"]) == 4


def test_stale_items_are_skipped_and_the_rest_apply(client):
    pid = _project(client, "Batch conflict")["id"]
    a, b, _ = _image_items(client, pid)
    client.patch(f"{P}/projects/{pid}/items/{b['id']}", json={"expected_revision": b["revision"], "review_state": "reviewed"})
    body = client.post(f"{P}/projects/{pid}/source-images/{a['id']}/edits", json={"edits": [
        {"op": "remove", "item_id": a["id"], "expected_revision": a["revision"]},
        {"op": "remove", "item_id": b["id"], "expected_revision": b["revision"]},
    ]}).json()
    assert body["applied"] == 1 and [c["id"] for c in body["conflicts"]] == [b["id"]]


def test_batch_validation(client):
    pid = _project(client, "Batch rules")["id"]
    a, _, other = _image_items(client, pid)
    url = f"{P}/projects/{pid}/source-images/{a['id']}/edits"
    elsewhere = client.post(url, json={"edits": [{"op": "remove", "item_id": other["id"], "expected_revision": other["revision"]}]})
    assert elsewhere.status_code == 422 and "not on this image" in elsewhere.json()["detail"]
    outside = client.post(url, json={"added": [{"row_px": 900, "column_px": 10, "crop_size": 224, "label": "ACBR"}]})
    assert outside.status_code == 422
    bad_label = client.post(url, json={"added": [{"row_px": 10, "column_px": 10, "crop_size": 224, "label": "NOT-A-CODE"}]})
    assert bad_label.status_code == 422
    too_many = client.post(url, json={"added": [{"row_px": 10, "column_px": 10, "crop_size": 224, "label": "ACBR"}] * 201})
    assert too_many.status_code == 422
    nothing_applied = client.get(f"{P}/projects/{pid}/source-images/{a['id']}").json()
    assert len(nothing_applied["items"]) == 2  # a refused batch changes nothing


def test_suggestions_form_a_clipped_grid(client):
    pid = _project(client, "Suggest")["id"]
    a, _, _ = _image_items(client, pid)
    body = client.post(f"{P}/projects/{pid}/source-images/{a['id']}/suggest",
                       json={"item_id": a["id"], "grid": 3, "spacing_px": 150}).json()
    centres = {(c["row_px"], c["column_px"]) for c in body["candidates"]}
    assert (100, 100) not in centres  # the source patch itself is not a candidate
    assert all(0 <= r < 800 and 0 <= c < 1000 for r, c in centres)
    assert len(centres) == 3  # (100,100) at spacing 150: only +150 offsets stay inside on both axes
    assert body["model_available"] is False and all("hints" not in c for c in body["candidates"])
    five = client.post(f"{P}/projects/{pid}/source-images/{a['id']}/suggest",
                       json={"item_id": a["id"], "grid": 5, "spacing_px": 40}).json()
    assert len(five["candidates"]) == 24


def test_suggestion_hints_come_from_the_classifier(client, monkeypatch):
    from curation import scoring

    pid = _project(client, "Suggest hints")["id"]
    a, _, _ = _image_items(client, pid)
    monkeypatch.setattr(scoring, "classify_points", lambda uri, points, size, model: [
        [{"label": "POCS", "confidence": 0.9, "rank": 1}] for _ in points])
    body = client.post(f"{P}/projects/{pid}/source-images/{a['id']}/suggest",
                       json={"item_id": a["id"], "grid": 3, "spacing_px": 150, "model_name": "m.pt"}).json()
    assert body["model_available"] is True and body["candidates"][0]["hints"][0]["label"] == "POCS"


def test_editing_a_missing_image_is_a_clear_422(client):
    pid = _project(client, "Batch missing")["id"]
    item = _point(client, pid, image="missing-2")
    out = client.post(f"{P}/projects/{pid}/source-images/{item['id']}/edits",
                      json={"added": [{"row_px": 10, "column_px": 10, "crop_size": 224, "label": "ACBR"}]})
    assert out.status_code == 422 and "not in the bucket" in out.json()["detail"]
    detail = client.get(f"{P}/projects/{pid}/source-images/{item['id']}").json()
    assert detail["width"] is None and detail["source_missing"] is True
```

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_editing.py`
Expected: the new tests FAIL with 404 or 405.

- [ ] **Step 3: Schemas** (`curation/schemas.py`)

```python
class GeometryEdit(BaseModel):
    op: Literal["move", "resize", "remove", "restore"]
    item_id: int
    expected_revision: int
    row_px: Optional[int] = Field(default=None, ge=0)
    column_px: Optional[int] = Field(default=None, ge=0)
    crop_size: Optional[int] = Field(default=None, ge=32, le=1024)


class NewPoint(BaseModel):
    row_px: int = Field(ge=0)
    column_px: int = Field(ge=0)
    crop_size: int = Field(default=224, ge=32, le=1024)
    label: str = Field(min_length=1, max_length=100)


class SourceEditBatch(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=4000)
    edits: list[GeometryEdit] = Field(default_factory=list, max_length=200)
    added: list[NewPoint] = Field(default_factory=list, max_length=200)


class SuggestRequest(BaseModel):
    item_id: int
    grid: Literal[3, 5] = 3
    spacing_px: int = Field(default=224, ge=32, le=512)
    crop_size: Optional[int] = Field(default=None, ge=32, le=1024)
    model_name: Optional[str] = Field(default=None, max_length=300)
```

- [ ] **Step 4: Factor `_add_point` out of `service.create_manual_point`.** Move everything from building the `CurationItem` through adding its `manual_point_created` event into `_add_point(db, project, anchor, *, row_px, column_px, crop_size, label, user, reason=None, operation_id=None)`. It:
- validates the label with `_clean_label`;
- calls `check_point_bounds(anchor.source_image_uri, row_px, column_px)`;
- flushes and bumps `project.item_count`;
- adds the event with `operation_id`;
- returns the item without committing.

`create_manual_point` then becomes: get project and anchor, check patch-only (the existing HF guard), call `_add_point(...)`, commit, refresh and return. Existing manual-point tests must pass unchanged.

- [ ] **Step 5: `scoring.classify_points`.** In `curation/scoring.py`, extract the "crop, then predict, then top 5" body of `classify_neighborhood` into the helper below. `classify_neighborhood` then calls it with its grid points and keeps its own response building.

```python
def classify_points(uri: str, points: list[tuple[int, int]], crop_size: int, model_name: str) -> list[list[dict]]:
    """Top predictions for a square patch around each (row, column), cut from the cached source."""
```

- [ ] **Step 6: Create `curation/editing.py`**

```python
"""
Edits to the points on one source image, applied together: move, resize,
remove, restore and new points. One transaction and one operation_id. An item
someone else changed since the editor loaded it is skipped and reported, not
overwritten. Suggestions lay out candidate patches around a patch, with model
hints when a classifier is available.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth.service import CurrentUser
from curation import image_cache, scoring, service
from curation.models import CurationItem
from curation.schemas import SourceEditBatch, SuggestRequest


def _image_items(db: Session, project_id: str, anchor: CurationItem) -> dict[int, CurationItem]:
    rows = db.scalars(select(CurationItem).where(
        CurationItem.project_id == project_id, CurationItem.source_image_uri == anchor.source_image_uri)).all()
    return {item.id: item for item in rows}


def _dimensions(uri: str) -> tuple[int, int]:
    try:
        return image_cache.dimensions(uri)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=422, detail="Source image not in the bucket — its points cannot be edited") from exc


def apply_batch(db: Session, project_id: str, anchor_item_id: int, batch: SourceEditBatch, user: CurrentUser) -> dict:
    project = service.get_project_or_404(db, project_id)
    anchor = service.get_item_or_404(db, project_id, anchor_item_id, "Source image not found")
    if anchor.row_px is None or project.source_kind == "huggingface":
        raise HTTPException(status_code=422, detail="This project holds patches only; there is no source image to edit")
    width, height = _dimensions(anchor.source_image_uri)
    items = _image_items(db, project_id, anchor)

    # Validate everything before changing anything: a refused batch changes nothing.
    for edit in batch.edits:
        if edit.item_id not in items:
            raise HTTPException(status_code=422, detail=f"Item {edit.item_id} is not on this image")
        if edit.op == "move":
            if edit.row_px is None or edit.column_px is None:
                raise HTTPException(status_code=422, detail="A move needs row_px and column_px")
            if not (0 <= edit.row_px < height and 0 <= edit.column_px < width):
                raise HTTPException(status_code=422, detail=f"The point must be inside the image ({width}×{height})")
        if edit.op == "resize" and edit.crop_size is None:
            raise HTTPException(status_code=422, detail="A resize needs crop_size")
    for point in batch.added:
        if not (0 <= point.row_px < height and 0 <= point.column_px < width):
            raise HTTPException(status_code=422, detail=f"The point must be inside the image ({width}×{height})")
        service._clean_label(project, point.label)  # noqa: SLF001 — raises 422 for an unknown code

    operation_id = str(uuid.uuid4())
    applied, conflicts = 0, []
    for edit in batch.edits:
        item = items[edit.item_id]
        if item.revision != edit.expected_revision:
            conflicts.append({"id": item.id, "revision": item.revision, "updated_by_name": item.updated_by_name})
            continue
        previous = service._state(item)  # noqa: SLF001
        if edit.op == "move":
            item.row_px, item.column_px = edit.row_px, edit.column_px
        elif edit.op == "resize":
            item.crop_size = edit.crop_size
        if edit.op in {"move", "resize"}:
            item.metadata_json = {**(item.metadata_json or {}), "geometry_edited": True}
        else:
            service.set_removed(item, edit.op == "remove")
        service._record_change(db, item, user, "item_updated", previous,  # noqa: SLF001
                               reason=batch.reason, operation_id=operation_id)
        applied += 1
    added = [service._add_point(db, project, anchor, row_px=p.row_px, column_px=p.column_px,  # noqa: SLF001
                                crop_size=p.crop_size, label=p.label, user=user, reason=batch.reason,
                                operation_id=operation_id) for p in batch.added]
    db.commit()
    for item in added:
        db.refresh(item)
    return {"operation_id": operation_id, "applied": applied, "added": added, "conflicts": conflicts}


def suggest(db: Session, project_id: str, anchor_item_id: int, request: SuggestRequest) -> dict:
    anchor = service.get_item_or_404(db, project_id, anchor_item_id, "Source image not found")
    items = _image_items(db, project_id, anchor)
    source = items.get(request.item_id)
    if source is None or source.row_px is None:
        raise HTTPException(status_code=422, detail="Choose a point on this image to suggest around")
    width, height = _dimensions(anchor.source_image_uri)
    size = request.crop_size or source.crop_size
    reach = request.grid // 2
    centres = []
    for dr in range(-reach, reach + 1):
        for dc in range(-reach, reach + 1):
            if dr == 0 and dc == 0:
                continue
            row, column = source.row_px + dr * request.spacing_px, source.column_px + dc * request.spacing_px
            if 0 <= row < height and 0 <= column < width:
                centres.append((row, column))
    candidates = [{"row_px": r, "column_px": c, "crop_size": size} for r, c in centres]
    model_available = False
    if request.model_name and centres:
        try:
            hints = scoring.classify_points(anchor.source_image_uri, centres, size, request.model_name)
            model_available = True
            for candidate, top in zip(candidates, hints):
                candidate["hints"] = top[:3]
        except HTTPException:
            model_available = False
    return {"candidates": candidates, "model_available": model_available}
```

- [ ] **Step 7: Routes.** In `routers/curation.py`, with the other source-image routes (import `editing`, and the three schemas from `curation.schemas`):

```python
@router.post("/projects/{project_id}/source-images/{anchor_item_id}/edits")
def edit_source_image(project_id: str, anchor_item_id: int, batch: SourceEditBatch,
                      user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    result = editing.apply_batch(db, project_id, anchor_item_id, batch, user)
    return {**result, "added": [CurationItemResponse.model_validate(i).model_dump() for i in result["added"]]}


@router.post("/projects/{project_id}/source-images/{anchor_item_id}/suggest")
def suggest_points(project_id: str, anchor_item_id: int, request: SuggestRequest,
                   user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    return editing.suggest(db, project_id, anchor_item_id, request)
```

In `get_source_image`, add `width`, `height` and `source_missing` to the returned dict:

```python
    try:
        width, height = image_cache.dimensions(anchor.source_image_uri)
        missing = False
    except FileNotFoundError:
        width = height = None
        missing = True
```

Add the three fields to `CurationSourceImageDetail` in `schemas.py`: `width: Optional[int] = None`, `height: Optional[int] = None` and `source_missing: bool = False`. The route lists every point on the image; include `removed` items there too. The editor shows them greyed out with Restore, so use no `removed` filter in that route's own query.

- [ ] **Step 8: Run**

Run: TEST `tests/test_editing.py tests/test_curation.py`, then the full suite, then the Oracle command for `tests/test_editing.py`. Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add curation/editing.py curation/service.py curation/scoring.py curation/schemas.py routers/curation.py tests/test_editing.py
git commit -m "Batch edits on a source image (move, resize, remove, restore, add) and grid suggestions with model hints"
```

---

### Task 4: Fresh crops for edited patches, export, and change-set kinds

**Files:**
- Modify: `curation/images.py` (`needs_fresh_crop`)
- Modify: `routers/curation.py` (`crop_preview`)
- Modify: `curation/versions.py` (`_export_images`)
- Modify: `curation/change_sets.py` (`kinds_of`)
- Test: `tests/test_editing.py` (append)

**Interfaces — Produces:** `images.needs_fresh_crop(item) -> bool`, which is True when `metadata_json` has `geometry_edited` or `manual_point`. It works on `CurationItem` and `CurationVersionInput` alike.

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_a_moved_patch_never_shows_its_old_crop(client, monkeypatch):
    from curation import image_cache, storage

    pid = _project(client, "Fresh crop")["id"]
    item = _point(client, pid)
    old_crop = b"old-crop-bytes"
    real = storage.read_bytes
    monkeypatch.setattr(storage, "read_bytes", lambda uri: old_crop if uri == item["crop_uri"] else real(uri))
    image_cache.clear()
    assert client.get(f"{P}/projects/{pid}/items/{item['id']}/crop-preview").content == old_crop
    client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                 json={"expected_revision": item["revision"], "row_px": 300, "column_px": 300})
    fresh = client.get(f"{P}/projects/{pid}/items/{item['id']}/crop-preview")
    assert fresh.status_code == 200 and fresh.content != old_crop and fresh.content.startswith(b"\x89PNG")


def test_commit_summaries_name_geometry_edits(client):
    pid = _project(client, "Kinds")["id"]
    a, b, _ = _image_items(client, pid)
    client.post(f"{P}/projects/{pid}/source-images/{a['id']}/edits", json={"edits": [
        {"op": "move", "item_id": a["id"], "expected_revision": a["revision"], "row_px": 111, "column_px": 111},
        {"op": "resize", "item_id": b["id"], "expected_revision": b["revision"], "crop_size": 300},
    ], "added": [{"row_px": 20, "column_px": 20, "crop_size": 224, "label": "ACBR"}]})
    kinds = client.get(f"{P}/projects/{pid}/change-sets/pending?user=me").json()["summary"]["kinds"]
    assert kinds["move"] == 1 and kinds["resize"] == 1 and kinds["point"] == 1
```

Also add an export test, following the existing image-export test in `tests/test_versions.py`:
1. publish a version containing a moved item whose old `crop_uri` returns stale bytes;
2. run "Write image folders";
3. assert the written file is the freshly cut PNG, not the stale bytes.

Read the existing export test (`grep -n "images" tests/test_versions.py`) and mirror how it reads the output folder.

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_editing.py tests/test_versions.py -k "fresh or kinds or export"`
Expected: FAIL.

- [ ] **Step 3: Implement.**
- `curation/images.py`:

```python
def needs_fresh_crop(item) -> bool:
    """Edited or added points have no stored crop that matches them: cut from the source."""
    metadata = getattr(item, "metadata_json", None) or {}
    return bool(metadata.get("geometry_edited") or metadata.get("manual_point"))
```

- `routers/curation.py` `crop_preview`: at the top, after loading `item`:

```python
    if images.needs_fresh_crop(item) and item.row_px is not None:
        images.require_allowed(db, item.source_image_uri)
        try:
            return Response(images.crop_from_source(item), media_type="image/png", headers=PNG_CACHE)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Source image not in the bucket") from exc
```

  Keep the existing candidate loop for other items. Change its final fallback's 404 detail to "Source image not in the bucket". `PNG_CACHE` caches privately for an hour, so a moved patch's URL must change. Add the item's `revision` as a query string in the client (Task 8 does this in `cropPreviewUrl`).

- `curation/versions.py` `_export_images`: before trying `crop_candidates`, use `images.crop_from_source(item)` when `images.needs_fresh_crop(item) and item.row_px is not None`.
- `curation/change_sets.py` `kinds_of`, after the label, inclusion and review checks:

```python
    if any(field in before and before.get(field) != after.get(field) for field in ("row_px", "column_px")):
        kinds.add("move")
    if "crop_size" in before and before.get("crop_size") != after.get("crop_size"):
        kinds.add("resize")
    if "removed" in before and before.get("removed") != after.get("removed"):
        kinds.add("remove" if after.get("removed") else "restore")
```

  The `included` change that a remove also makes would add `inclusion` too. When `remove` or `restore` is present, discard `inclusion` so the summary reads "1 removed", not both.

- [ ] **Step 4: Run**

Run: TEST `tests/test_editing.py tests/test_versions.py tests/test_change_sets.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add curation/images.py routers/curation.py curation/versions.py curation/change_sets.py tests/test_editing.py tests/test_versions.py
git commit -m "Edited and added patches are cut fresh for previews and exports; commit summaries name moves, resizes, removals"
```

---

### Task 5: The source-image availability check

**Files:**
- Create: `curation/availability.py`
- Modify: `curation/storage.py` (`list_names`)
- Modify: `curation/importer.py`:
  - dispatch the operation kind `availability_check`;
  - queue a check after a source import completes.
- Modify: `routers/curation.py`:
  - `POST /projects/{id}/availability` (project curator, 202);
  - `GET /projects/{id}/availability` (counts);
  - `GET /projects/{id}/availability.csv`.
- Test: `tests/test_availability.py` (new; add it to `test-oracle`)

**Interfaces — Produces:**
- `storage.list_names(prefix_uri: str) -> set[str]`: the image file names directly under a `gs://…/images/` prefix, or a local folder.
- `availability.check(db, project_id, cancel=None) -> dict` with `{checked, missing, prefixes}`.
- `availability.summary(db, project_id) -> {missing_images, missing_items, by_region_year: [{region, year, images}]}`.

- [ ] **Step 1: Write the failing tests** in `tests/test_availability.py`

```python
"""Which source images are missing from their bucket."""
from __future__ import annotations

import time

from test_curation import P, _add, _item_payload, _project, _test_buckets, client  # noqa: F401


def _seed(client):
    pid = _project(client, "Availability")["id"]
    for key, image, region, year in (("a1", "present-1", "Hawaii", 2023), ("a2", "present-1", "Hawaii", 2023),
                                     ("a3", "gone-1", "Hawaii", 2024), ("a4", "gone-2", "Mariana", 2024)):
        _add(client, pid, {**_item_payload(key, "ACBR", region=region), "survey_year": year,
                           "source_image_uri": f"gs://nmfs/images/{image}.jpg"})
    return pid


def _wait(client, pid, operation_id):
    for _ in range(100):
        op = client.get(f"{P}/projects/{pid}/imports/{operation_id}").json()
        if op["status"] not in {"queued", "running"}:
            return op
        time.sleep(0.1)
    raise AssertionError("check did not finish")


def test_missing_images_are_flagged_counted_and_filterable(client, monkeypatch):
    from curation import availability, storage

    monkeypatch.setattr(storage, "list_names", lambda prefix: {"present-1.jpg"})
    pid = _seed(client)
    from appdb import session_scope
    with session_scope() as db:
        result = availability.check(db, pid)
    assert (result["checked"], result["missing"]) == (3, 2)
    summary = client.get(f"{P}/projects/{pid}/availability").json()
    assert summary["missing_images"] == 2 and summary["missing_items"] == 2
    assert {(r["region"], r["year"], r["images"]) for r in summary["by_region_year"]} == {("Hawaii", 2024, 1), ("Mariana", 2024, 1)}
    assert client.get(f"{P}/projects/{pid}/items?source_missing=hide").json()["total"] == 2
    assert client.get(f"{P}/projects/{pid}/items?source_missing=only").json()["total"] == 2
    csv_text = client.get(f"{P}/projects/{pid}/availability.csv").text
    assert "gone-1.jpg" in csv_text and "present-1.jpg" not in csv_text


def test_a_second_check_clears_images_that_came_back(client, monkeypatch):
    from appdb import session_scope
    from curation import availability, storage

    pid = _seed(client)
    monkeypatch.setattr(storage, "list_names", lambda prefix: {"present-1.jpg"})
    with session_scope() as db:
        availability.check(db, pid)
    monkeypatch.setattr(storage, "list_names", lambda prefix: {"present-1.jpg", "gone-1.jpg", "gone-2.jpg"})
    with session_scope() as db:
        assert availability.check(db, pid)["missing"] == 0
    assert client.get(f"{P}/projects/{pid}/items?source_missing=only").json()["total"] == 0


def test_the_check_runs_as_a_background_operation(client, monkeypatch):
    from curation import storage

    monkeypatch.setattr(storage, "list_names", lambda prefix: set())
    pid = _seed(client)
    started = client.post(f"{P}/projects/{pid}/availability")
    assert started.status_code == 202
    op = _wait(client, pid, started.json()["id"])
    assert op["status"] == "completed", op
    assert client.get(f"{P}/projects/{pid}/availability").json()["missing_images"] == 3


def test_missing_images_sort_last_in_the_source_queue(client, monkeypatch):
    from appdb import session_scope
    from curation import availability, storage

    monkeypatch.setattr(storage, "list_names", lambda prefix: {"present-1.jpg"})
    pid = _seed(client)
    with session_scope() as db:
        availability.check(db, pid)
    images = client.get(f"{P}/projects/{pid}/source-images?source_missing=any").json()["images"]
    assert images[-1]["source_image_uri"].endswith(("gone-1.jpg", "gone-2.jpg"))
    assert images[0]["source_image_uri"].endswith("present-1.jpg")
```

The background test depends on how import operations run in tests. Read `tests/test_curation.py::test_import_worker_completes_from_stream_and_updates_project`. If operations need the worker started or a manual `run_import(operation_id, event)`, follow that pattern instead of polling. Adjust `_wait` accordingly.

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_availability.py`
Expected: FAIL.

- [ ] **Step 3: `storage.list_names`.** Add to `curation/storage.py`:

```python
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def list_names(prefix_uri: str) -> set[str]:
    """File names (not paths) of the images directly under a prefix, e.g. gs://bucket/…/images/."""
    if not prefix_uri.startswith("gs://"):
        _local_allowed(prefix_uri)
        folder = _local_path(prefix_uri)
        return {p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS} if folder.is_dir() else set()
    backend = GCSBackend(prefix_uri.rstrip("/"))
    return {ref.key.rsplit("/", 1)[-1] for ref in backend.list_objects(IMAGE_EXTENSIONS) if "/" not in ref.key}
```

`GCSBackend` with a prefix lists recursively. The `"/" not in ref.key` check keeps only direct children; `key` is relative to the prefix.

- [ ] **Step 4: Create `curation/availability.py`**

```python
"""
Which source images are missing from their bucket.

One listing per images/ prefix the project uses (about a minute for 93k images),
then a column flag per item. That makes the source queue able to hide missing
images and sort them last, and lets Dataset status say which surveys are gone.
"""
from __future__ import annotations

import csv
import io
import threading
from collections import defaultdict
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from curation import storage
from curation.models import CurationItem
from curation.service import in_chunks


def _prefix(uri: str) -> str:
    return uri.rsplit("/", 1)[0] + "/"


def check(db: Session, project_id: str, cancel: Optional[threading.Event] = None) -> dict:
    uris = [u for u in db.scalars(select(CurationItem.source_image_uri).where(
        CurationItem.project_id == project_id).distinct())]
    by_prefix: dict[str, list[str]] = defaultdict(list)
    for uri in uris:
        by_prefix[_prefix(uri)].append(uri)
    missing: list[str] = []
    for prefix, members in by_prefix.items():
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Availability check canceled")
        try:
            present = storage.list_names(prefix)
        except PermissionError:
            continue  # local paths not allowed here: leave those images unflagged
        missing.extend(uri for uri in members if uri.rsplit("/", 1)[-1] not in present)
    db.execute(update(CurationItem).where(CurationItem.project_id == project_id, CurationItem.source_missing == True)  # noqa: E712
               .values(source_missing=False).execution_options(synchronize_session=False))
    for start in range(0, len(missing), 1000):
        db.execute(update(CurationItem).where(CurationItem.project_id == project_id,
                                              in_chunks(CurationItem.source_image_uri, missing[start:start + 1000]))
                   .values(source_missing=True).execution_options(synchronize_session=False))
    db.commit()
    return {"checked": len(uris), "missing": len(missing), "prefixes": len(by_prefix)}


def summary(db: Session, project_id: str) -> dict:
    where = [CurationItem.project_id == project_id, CurationItem.source_missing == True]  # noqa: E712
    images = db.scalar(select(func.count(func.distinct(CurationItem.source_image_uri))).where(*where)) or 0
    items = db.scalar(select(func.count()).select_from(CurationItem).where(*where)) or 0
    rows = db.execute(select(CurationItem.region, CurationItem.survey_year,
                             func.count(func.distinct(CurationItem.source_image_uri)))
                      .where(*where).group_by(CurationItem.region, CurationItem.survey_year)).all()
    return {"missing_images": images, "missing_items": items,
            "by_region_year": [{"region": r, "year": y, "images": n} for r, y, n in
                               sorted(rows, key=lambda row: (-row[2], str(row[0]), row[1] or 0))]}


def missing_csv(db: Session, project_id: str) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["source_image_uri", "region", "survey_year", "points"])
    for uri, region, year, count in db.execute(
            select(CurationItem.source_image_uri, CurationItem.region, CurationItem.survey_year, func.count())
            .where(CurationItem.project_id == project_id, CurationItem.source_missing == True)  # noqa: E712
            .group_by(CurationItem.source_image_uri, CurationItem.region, CurationItem.survey_year)
            .order_by(CurationItem.region, CurationItem.survey_year, CurationItem.source_image_uri)):
        writer.writerow([uri, region, year, count])
    return output.getvalue()
```

- [ ] **Step 5: Background operation and routes.**
- **`curation/importer.py`:** the operation dispatch already branches on source `kind` for HF imports and links; read `run_import`. Add `kind == "availability_check"`: call `availability.check(db, project_id, cancel_event)` and store the counts on the operation: `processed_rows = checked`, `rejected_rows = missing`. Mark it `completed` as the other kinds do.
- **After an import:** after a `regional_csv` or `hub_dataset` import completes successfully, queue an `availability_check` operation for the same project.
- **`routers/curation.py`:**

```python
@router.post("/projects/{project_id}/availability", status_code=202, response_model=CurationImportResponse)
def check_availability(project_id: str, user: CurrentUser = Depends(members.require_project_curator),
                       db: Session = Depends(get_db)):
    return service.queue_operation(db, project_id, [{"kind": "availability_check", "name": "Check source images"}], user)


@router.get("/projects/{project_id}/availability")
def availability_summary(project_id: str, db: Session = Depends(get_db)):
    service.get_project_or_404(db, project_id)
    return availability.summary(db, project_id)


@router.get("/projects/{project_id}/availability.csv")
def availability_csv(project_id: str, db: Session = Depends(get_db)):
    project = service.get_project_or_404(db, project_id)
    return Response(availability.missing_csv(db, project_id), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{versions._slug(project.name)}_missing_images.csv"'})  # noqa: SLF001
```

  `service.queue_operation` stands for however the code already creates an import operation; P1's HF imports and `create_import` do it. Use that existing function (`grep -n "CurationImportOperation(" curation/*.py`), or add the one-line helper there.

- **Source queue order:** in `service.source_image_page`, order by `source_missing` ascending first (False before True), then the existing order. That makes missing images sort last.

- [ ] **Step 6: Oracle list and run**

Add `tests/test_availability.py` to `test-oracle`. Run TEST `tests/test_availability.py tests/test_curation.py`, the full suite, and the Oracle command for `tests/test_availability.py`. Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add curation/availability.py curation/storage.py curation/importer.py curation/service.py routers/curation.py tests/test_availability.py docker-compose.stack.yml
git commit -m "Source-image availability check: flags missing images, counts them by region and year, sorts them last"
```

---

### Task 6: Refuse legacy versions in releases and project compare

**Files:**
- Modify: `curation/releases.py` (`start`)
- Modify: `curation/project_compare.py` (`_side`)
- Test: `tests/test_releases.py` and `tests/test_project_compare.py` (append)

**Interfaces — Produces:** releases and project compare return 422 with the detail "v{n} of {project} was published before releases were supported — publish a new version first" for a version with `item_count > 0` and no `curation_version_inputs` rows.

- [ ] **Step 1: Write the failing tests.** In each file, publish a version, then delete its `CurationVersionInput` rows through `db_session` or `session_scope` to simulate a legacy version. Assert the 422 and the phrase "publish a new version first".
  - `test_releases.py`: `POST /releases` with that source returns 422.
  - `test_project_compare.py`: `compare-projects` with `a_version` = that version returns 422.

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_releases.py tests/test_project_compare.py -k legacy`
Expected: FAIL. Today the release builds empty and the compare counts 0.

- [ ] **Step 3: Implement.** In both places, after the published-version check:

```python
        has_inputs = db.scalar(select(CurationVersionInput.item_id)
                               .where(CurationVersionInput.version_id == version.id).limit(1)) is not None
        if version.item_count and not has_inputs:
            raise HTTPException(status_code=422, detail=f"v{version.version} of {project.name} was published before "
                                                        "releases were supported — publish a new version first")
```

- [ ] **Step 4: Run** the two test files, plus the Oracle command for them. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add curation/releases.py curation/project_compare.py tests/test_releases.py tests/test_project_compare.py
git commit -m "Releases and project compare refuse versions published before frozen inputs existed"
```

---

### Task 7: Navigation — back links, open source image, image in the URL, missing-image notice

**Files:**
- Modify: `templates/curation.html`:
  - a `#back-bar` above the paired view;
  - a `#source-missing-notice` inside the image wrap.
- Modify: `static/curation/curation.js`:
  - `readHash`/`writeHash`, `selectImage`, `selectItem`;
  - crop tiles, quality cards and evaluation cards;
  - `setView`, `cropPreviewUrl`.
- Modify: `static/curation.css`
- Test: `tests/test_curation.py` (string and element checks)

**Interfaces — Produces:**
- `state.backTo = {view, label, scrollTop, filtersHash} | null`
- `openSource(itemId, from)`: records where you came from, switches to the source view and selects the image and point
- the hash gains `image` and `item`

- [ ] **Step 1: Write the failing check**

```python
def test_navigation_back_links_and_deep_links(client):
    page = client.get("/curation").text
    for element in ('id="back-bar"', 'id="back-button"', 'id="source-missing-notice"'):
        assert element in page
    script = client.get("/static/curation/curation.js").text
    for needle in ("function openSource(", "state.backTo", "params.set('image'", "data-open-source"):
        assert needle in script
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_curation.py::test_navigation_back_links_and_deep_links`
Expected: FAIL.

- [ ] **Step 3: Template.**
- Directly before `<div id="paired-view" …>`:

```html
      <div id="back-bar" class="back-bar d-none"><button id="back-button" class="btn btn-sm btn-outline-secondary" type="button"><i class="bi bi-arrow-left"></i> <span id="back-label">Back</span></button></div>
```

- Inside the source image wrap, next to `#source-image`:

```html
              <div id="source-missing-notice" class="source-missing-notice d-none" role="status"></div>
```

- [ ] **Step 4: Script.**

```js
// ── Navigation: where the source view was opened from, and how to get back ──
const BACK_LABELS = { crop: 'crop grid', quality: 'quality review', matrix: 'confusion matrix', versions: 'history', status: 'dataset status' }

async function openSource(itemId, from = state.view) {
  if (from && from !== 'source') {
    const panel = document.querySelector('.curation-main') || document.scrollingElement
    state.backTo = { view: from, label: BACK_LABELS[from] || from, scrollTop: panel ? panel.scrollTop : 0 }
  }
  setView('source')
  await selectImage(itemId, itemId)
}

function renderBackBar() {
  const show = state.view === 'source' && Boolean(state.backTo)
  byId('back-bar').classList.toggle('d-none', !show)
  if (show) byId('back-label').textContent = `Back to ${state.backTo.label}`
}

byId('back-button').addEventListener('click', () => {
  const back = state.backTo
  if (!back) return
  state.backTo = null
  setView(back.view)
  requestAnimationFrame(() => {
    const panel = document.querySelector('.curation-main') || document.scrollingElement
    if (panel) panel.scrollTop = back.scrollTop
  })
})
```

Wiring:
- **Crop tile click handler** (~line 518): replace `setView('source'); await selectImage(itemId, itemId)` with `await openSource(itemId, 'crop')`.
- **Quality cards:** add a button `<button type="button" class="btn btn-sm btn-outline-secondary" data-open-source="${item.reviewId}"><i class="bi bi-image"></i> Source image</button>` to each card's actions row. After rendering, bind it with `document.querySelectorAll('[data-open-source]').forEach(b => b.addEventListener('click', () => openSource(Number(b.dataset.openSource), 'quality')))`.
- **Evaluation cards:** their existing "Full image" (`data-open-matrix-item`) handler sets `editContext` and goes to the source view. Also set `state.backTo` for `matrix` before switching.
- **Change-set "Show these items" and compare class links** go to the crop grid, so they need no back link here.
- **`setView`:** call `renderBackBar()` at the end. When leaving the source view for any view other than through Back, clear `state.backTo` (`if (view !== 'source') state.backTo = null`), so the bar never points somewhere stale.
- **URL hash:** in `writeHash`, when `state.view === 'source'` and `state.currentImage`, add `params.set('image', state.currentImage.first_item_id)`, and `params.set('item', state.selected.id)` when there's a selection. In `readHash`, return `image` and `item` (numbers or null). In `init` and `switchProject`, after the project loads, if the linked view is `source` and `image` is set, `await selectImage(image, item || image)`. In `selectItem`, call `writeHash()` at the end, so the URL follows the selected point.
- **Missing image:** in `selectImage`, after fetching the detail:

```js
  const missing = Boolean(detail.source_missing)
  byId('source-missing-notice').classList.toggle('d-none', !missing)
  byId('source-missing-notice').textContent = missing ? `Source image not in the bucket: ${detail.source_image_uri}` : ''
```

  Also hook the image's `error` event to show the same notice, for images that disappear after the check: `byId('source-image').addEventListener('error', …)`.
- **Crop preview URL:** in `cropPreviewUrl(item)`, append `&r=${item.revision}`, or `?r=` if there is no query yet. An edited patch then gets a new URL instead of the browser's hour-long cached copy. Do the same in the patch `<img>` `onerror` for crop tiles: show a small "missing" placeholder class instead of a broken image.

- [ ] **Step 5: Styles.** Append to `static/curation.css`:

```css
.back-bar { margin-bottom: 8px; }
.source-missing-notice { position: absolute; inset: 12px; display: flex; align-items: center; justify-content: center; text-align: center; padding: 16px; background: rgba(255,255,255,.92); color: #8a2d2d; font-weight: 600; border: 1px dashed #c94d4d; border-radius: 8px; z-index: 5; }
```

- [ ] **Step 6: Run** TEST `tests/test_curation.py` and the JS syntax check (`curation`, `hub`, `collab`). Expected: PASS, SYNTAX-OK.

- [ ] **Step 7: Commit**

```bash
git add templates/curation.html static/curation/curation.js static/curation.css tests/test_curation.py
git commit -m "Back to where you came from; Source image from quality and evaluation cards; image and point in the URL; missing-image notice"
```

---

### Task 8: The editor — move, resize, remove, restore, pending tray, save

**Files:**
- Create: `static/curation/editor.js`
- Modify: `templates/curation.html`:
  - the script tag after `collab.js`;
  - the point panel gets `#point-remove`, `#point-restore`, `#point-size` and size presets;
  - a `#pending-tray` under the image, holding `#pending-list`, `#pending-reason`, `#pending-save`, `#pending-discard` and `#pending-count`.
- Modify: `static/curation/curation.js`:
  - `renderMarkers` gets handles for the selected point when editable;
  - `selectImage` and the view and project switches warn about unsaved edits;
  - removed points are shown greyed.
- Modify: `static/curation.css`
- Modify: `tests/test_curation.py` (the integrity test also scans `editor.js`; a string check)

**Interfaces:**
- **Consumes:** `POST …/source-images/{anchor}/edits` (Task 3); `detail.width` and `detail.height`.
- **Produces:**
  - `state.pending = {moves: Map(id→{row_px,column_px}), resizes: Map(id→crop_size), removes: Set, restores: Set, added: []}`;
  - `pendingCount()`, `confirmDiscardPending() -> boolean`;
  - `markerEditable()`, true when `canDo('project-curator')`, the image isn't missing and the point has coordinates.

- [ ] **Step 1: Write the failing check.** Extend the integrity test's file list to include `static/curation/editor.js`, and append this test:

```python
def test_the_source_editor_is_on_the_page(client):
    page = client.get("/curation").text
    assert "/static/curation/editor.js" in page
    for element in ('id="pending-tray"', 'id="pending-list"', 'id="pending-save"', 'id="pending-discard"',
                    'id="point-remove"', 'id="point-restore"', 'id="point-size"'):
        assert element in page
    script = client.get("/static/curation/editor.js").text
    for needle in ("function pendingCount(", "function savePending(", "/edits", "function confirmDiscardPending("):
        assert needle in script
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_curation.py -k "editor or page_renders"`
Expected: FAIL.

- [ ] **Step 3: Template.**
- In the item form's point panel, gated by `data-min-role="project-curator"`:

```html
          <div class="point-edit-tools">
            <label class="form-label small mb-0" for="point-size">Patch size</label>
            <div class="d-flex gap-1 align-items-center">
              <input id="point-size" class="form-control form-control-sm" type="number" min="32" max="1024" step="8" style="width:6rem" data-min-role="project-curator">
              <button type="button" class="btn btn-sm btn-outline-secondary" data-point-preset="160" data-min-role="project-curator">160</button>
              <button type="button" class="btn btn-sm btn-outline-secondary" data-point-preset="224" data-min-role="project-curator">224</button>
              <button type="button" class="btn btn-sm btn-outline-secondary" data-point-preset="320" data-min-role="project-curator">320</button>
            </div>
            <div class="d-flex gap-1 mt-2">
              <button id="point-remove" type="button" class="btn btn-sm btn-outline-danger" data-min-role="project-curator"><i class="bi bi-trash"></i> Remove</button>
              <button id="point-restore" type="button" class="btn btn-sm btn-outline-success d-none" data-min-role="project-curator"><i class="bi bi-arrow-counterclockwise"></i> Restore</button>
            </div>
          </div>
```

- Under the source image panel:

```html
      <section id="pending-tray" class="pending-tray d-none" aria-label="Unsaved edits">
        <div class="curation-panel-title"><div><strong><span id="pending-count">0</span> unsaved edits</strong><small>Moves, resizes, removals and new patches on this image</small></div></div>
        <div id="pending-list" class="pending-list"></div>
        <div class="d-flex gap-2 mt-2"><input id="pending-reason" class="form-control form-control-sm" maxlength="4000" placeholder="Reason (optional)">
          <button id="pending-save" class="btn btn-sm btn-primary" type="button">Save</button>
          <button id="pending-discard" class="btn btn-sm btn-outline-secondary" type="button">Discard</button></div>
        <p id="pending-message" class="small mt-1 mb-0" role="status"></p>
      </section>
```

- Script tag after `collab.js`: `<script src="/static/curation/editor.js"></script>`.

- [ ] **Step 4: `static/curation/editor.js`** (a classic script using `curation.js` globals)

```js
// Editing points on the source image: drag to move, corner handle to resize,
// Remove/Restore, all collected as pending edits and saved together (one
// request, one revision check per item, one batch in the uncommitted changes).

state.pending = { moves: new Map(), resizes: new Map(), removes: new Set(), restores: new Set(), added: [] }

function pendingCount() {
  const p = state.pending
  return p.moves.size + p.resizes.size + p.removes.size + p.restores.size + p.added.length
}

function resetPending() {
  state.pending = { moves: new Map(), resizes: new Map(), removes: new Set(), restores: new Set(), added: [] }
  renderPending()
}

function confirmDiscardPending() {
  const count = pendingCount()
  if (!count) return true
  if (!window.confirm(`You have ${count} unsaved edit${count === 1 ? '' : 's'} on this image — discard them?`)) return false
  resetPending()
  return true
}

function markerEditable(item) {
  return canDo('project-curator') && item && item.row_px != null && state.currentImage && !state.currentImage.source_missing
}

// The geometry to draw for an item, including unsaved edits.
function effectiveGeometry(item) {
  const moved = state.pending.moves.get(item.id)
  return {
    row_px: moved ? moved.row_px : item.row_px,
    column_px: moved ? moved.column_px : item.column_px,
    crop_size: state.pending.resizes.get(item.id) ?? item.crop_size,
    removed: state.pending.removes.has(item.id) || (item.removed && !state.pending.restores.has(item.id)),
  }
}

function clampToImage(row, column) {
  const w = state.currentImage?.width || byId('source-image').naturalWidth
  const h = state.currentImage?.height || byId('source-image').naturalHeight
  return [Math.max(0, Math.min(h - 1, Math.round(row))), Math.max(0, Math.min(w - 1, Math.round(column)))]
}

function queueMove(item, row, column) {
  const [r, c] = clampToImage(row, column)
  if (r === item.row_px && c === item.column_px) state.pending.moves.delete(item.id)
  else state.pending.moves.set(item.id, { row_px: r, column_px: c })
  renderPending()
}

function queueResize(item, size) {
  const value = Math.max(32, Math.min(1024, Math.round(size)))
  if (value === item.crop_size) state.pending.resizes.delete(item.id)
  else state.pending.resizes.set(item.id, value)
  renderPending()
}

function queueRemove(item, removed) {
  state.pending.removes.delete(item.id)
  state.pending.restores.delete(item.id)
  if (removed && !item.removed) state.pending.removes.add(item.id)
  if (!removed && item.removed) state.pending.restores.add(item.id)
  renderPending()
}

// A patch cut in the browser from the already-loaded image, for the tray.
function previewDataUrl(row, column, size) {
  const image = byId('source-image')
  if (!image.naturalWidth) return ''
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = 96
  canvas.getContext('2d').drawImage(image, column - size / 2, row - size / 2, size, size, 0, 0, 96, 96)
  try { return canvas.toDataURL('image/png') } catch (_) { return '' }
}

function renderPending() {
  const count = pendingCount()
  byId('pending-tray').classList.toggle('d-none', !count)
  byId('pending-count').textContent = count
  const rows = []
  const byId_ = id => state.items.find(item => item.id === id)
  for (const [id, to] of state.pending.moves) {
    const item = byId_(id)
    if (item) rows.push({ kind: 'Move', item, text: `(${item.column_px}, ${item.row_px}) → (${to.column_px}, ${to.row_px})`, geo: { ...effectiveGeometry(item) } })
  }
  for (const [id, size] of state.pending.resizes) {
    const item = byId_(id)
    if (item) rows.push({ kind: 'Resize', item, text: `${item.crop_size} → ${size} px`, geo: effectiveGeometry(item) })
  }
  for (const id of state.pending.removes) { const item = byId_(id); if (item) rows.push({ kind: 'Remove', item, text: '', geo: effectiveGeometry(item) }) }
  for (const id of state.pending.restores) { const item = byId_(id); if (item) rows.push({ kind: 'Restore', item, text: '', geo: effectiveGeometry(item) }) }
  state.pending.added.forEach((point, index) => rows.push({ kind: 'Add', item: { current_label: point.label }, text: `(${point.column_px}, ${point.row_px}) · ${point.crop_size} px`, geo: point, addedIndex: index }))
  byId('pending-list').innerHTML = rows.map(row => `<div class="pending-row">
      <img alt="" src="${previewDataUrl(row.geo.row_px, row.geo.column_px, row.geo.crop_size)}">
      <span><b>${escapeHtml(row.kind)}</b> ${escapeHtml(row.item.current_label || '')} <small>${escapeHtml(row.text)}</small></span>
      ${row.addedIndex != null ? `<button type="button" class="btn btn-sm btn-link" data-pending-drop="${row.addedIndex}">drop</button>` : ''}
    </div>`).join('')
  byId('pending-list').querySelectorAll('[data-pending-drop]').forEach(b => b.addEventListener('click', () => {
    state.pending.added.splice(Number(b.dataset.pendingDrop), 1)
    renderPending()
    renderMarkers()
  }))
}

async function savePending() {
  if (!pendingCount() || !state.currentImage) return
  const p = state.pending
  const revisionOf = id => state.items.find(item => item.id === id)?.revision
  const edits = [
    ...[...p.moves].map(([id, to]) => ({ op: 'move', item_id: id, expected_revision: revisionOf(id), ...to })),
    ...[...p.resizes].map(([id, size]) => ({ op: 'resize', item_id: id, expected_revision: revisionOf(id), crop_size: size })),
    ...[...p.removes].map(id => ({ op: 'remove', item_id: id, expected_revision: revisionOf(id) })),
    ...[...p.restores].map(id => ({ op: 'restore', item_id: id, expected_revision: revisionOf(id) })),
  ]
  const message = byId('pending-message')
  byId('pending-save').disabled = true
  try {
    const result = await request(`/api/curation/projects/${state.projectId}/source-images/${state.currentImage.first_item_id}/edits`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: byId('pending-reason').value.trim() || null, edits, added: p.added.map(({ row_px, column_px, crop_size, label }) => ({ row_px, column_px, crop_size, label })) }),
    })
    resetPending()
    byId('pending-reason').value = ''
    reportConflicts({ updated: result.applied, conflicts: result.conflicts })
    message.textContent = `Saved ${result.applied + result.added.length} edits.`
    message.className = 'small mt-1 mb-0 text-success'
    const keep = state.selected?.id
    await selectImage(state.currentImage.first_item_id, keep)
    if (typeof refreshCommitPill === 'function') refreshCommitPill()
  } catch (error) {
    message.textContent = error.message
    message.className = 'small mt-1 mb-0 text-danger'
  } finally { byId('pending-save').disabled = false }
}

byId('pending-save').addEventListener('click', savePending)
byId('pending-discard').addEventListener('click', () => { resetPending(); renderMarkers() })
byId('point-remove').addEventListener('click', () => { if (state.selected) { queueRemove(state.selected, true); renderMarkers(); syncPointTools() } })
byId('point-restore').addEventListener('click', () => { if (state.selected) { queueRemove(state.selected, false); renderMarkers(); syncPointTools() } })
byId('point-size').addEventListener('change', event => { if (state.selected) { queueResize(state.selected, Number(event.target.value)); renderMarkers() } })
document.querySelectorAll('[data-point-preset]').forEach(b => b.addEventListener('click', () => {
  if (!state.selected) return
  queueResize(state.selected, Number(b.dataset.pointPreset))
  syncPointTools()
  renderMarkers()
}))

function syncPointTools() {
  const item = state.selected
  const editable = markerEditable(item)
  const geo = item ? effectiveGeometry(item) : null
  byId('point-size').value = geo ? geo.crop_size : ''
  byId('point-remove').classList.toggle('d-none', !editable || !geo || geo.removed)
  byId('point-restore').classList.toggle('d-none', !editable || !geo || !geo.removed)
}

window.addEventListener('beforeunload', event => { if (pendingCount()) { event.preventDefault(); event.returnValue = '' } })
```

- [ ] **Step 5: Markers with handles.** In `curation.js`'s `renderMarkers`:
- **Positions:** use `effectiveGeometry(item)` (guard with `typeof effectiveGeometry === 'function'`) for each item's position, size and removed state. Removed points get a `removed` class (greyed, dashed).
- **Pending new points:** draw `state.pending.added` points as dashed `source-marker pending` buttons too.
- **The selected point, when `markerEditable(item)`:** add a `<span class="resize-handle" data-resize="${item.id}"></span>` inside its marker. Always render it as a patch square (as in patch overlay mode), so the size is visible while editing.
- **Drag to move:** on `pointerdown` on the selected marker, start a drag. Convert pointer positions to image pixels with the existing `eventToImagePoint(event)`. Call `queueMove(item, point.row, point.column)` on `pointerup`, and update the marker's left and top live on `pointermove`. Stop propagation, so the image doesn't pan. Escape cancels the drag.
- **Drag to resize:** on `pointerdown` on `.resize-handle`, set the new size to `2 × max(|Δrow|, |Δcolumn|)` from the point's centre, in image pixels. Call `queueResize` on `pointerup`.
- **Pending tray:** after drawing, call `renderPending()` if it exists.

In `selectItem`, call `syncPointTools()` when it exists. In `selectImage` and the view and project switches (`setView`, `switchProject`), call `if (typeof confirmDiscardPending === 'function' && !confirmDiscardPending()) return` first, so unsaved edits are never silently lost. For `setView`, guard only when leaving `source`.

- [ ] **Step 6: Styles.** Append to `static/curation.css`:

```css
.source-marker.removed { opacity: .45; border-style: dashed !important; }
.source-marker.pending { border-style: dashed !important; background: transparent !important; }
.source-marker .resize-handle { position: absolute; right: -6px; bottom: -6px; width: 12px; height: 12px; background: #fff; border: 2px solid currentColor; border-radius: 2px; cursor: nwse-resize; }
.source-marker.selected { cursor: move; }
.pending-tray { margin-top: 10px; }
.pending-list { display: grid; gap: 6px; max-height: 260px; overflow: auto; }
.pending-row { display: flex; gap: 8px; align-items: center; }
.pending-row img { width: 48px; height: 48px; object-fit: cover; border-radius: 4px; background: #eef0f2; }
```

- [ ] **Step 7: Run** TEST `tests/test_curation.py` and the JS syntax check including `editor`. Expected: PASS, SYNTAX-OK.

- [ ] **Step 8: Commit**

```bash
git add static/curation/editor.js static/curation/curation.js templates/curation.html static/curation.css tests/test_curation.py
git commit -m "Source-image editor: drag to move, handle to resize, remove/restore, pending tray saved in one request"
```

---

### Task 9: Bulk add around a patch, and keyboard shortcuts

**Files:**
- Modify: `templates/curation.html` (a "Bulk add around…" button in the point panel, and a `#bulk-add-form` popover with `#bulk-grid`, `#bulk-spacing`, `#bulk-model` and `#bulk-run`)
- Modify: `static/curation/editor.js`
- Modify: `static/curation/curation.js` (key handlers; `renderMarkers` draws candidates)
- Modify: `static/curation.css`
- Test: `tests/test_curation.py`

**Interfaces — Consumes:**
- `POST …/suggest` (Task 3);
- the model list the classify feature already loads: find where `#classify-model` or similar is filled (`grep -n "classify" static/curation/curation.js templates/curation.html`), and reuse that list for `#bulk-model`.

- [ ] **Step 1: Write the failing check**

```python
def test_bulk_add_and_shortcuts_are_wired(client):
    page = client.get("/curation").text
    for element in ('id="bulk-add-button"', 'id="bulk-add-form"', 'id="bulk-grid"', 'id="bulk-spacing"', 'id="bulk-model"', 'id="bulk-run"'):
        assert element in page
    script = client.get("/static/curation/editor.js").text
    assert "/suggest" in script and "function addCandidates(" in script and "'keydown'" in script
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_curation.py::test_bulk_add_and_shortcuts_are_wired`
Expected: FAIL.

- [ ] **Step 3: Template.** In the point tools (Task 8):

```html
            <button id="bulk-add-button" type="button" class="btn btn-sm btn-outline-primary mt-2" data-min-role="project-curator"><i class="bi bi-grid-3x3"></i> Bulk add around…</button>
            <div id="bulk-add-form" class="bulk-add-form d-none">
              <div class="d-flex gap-2 align-items-end flex-wrap">
                <label class="small">Grid <select id="bulk-grid" class="form-select form-select-sm"><option value="3">3×3</option><option value="5">5×5</option></select></label>
                <label class="small">Spacing px <input id="bulk-spacing" class="form-control form-control-sm" type="number" min="32" max="512" step="8" value="224" style="width:6rem"></label>
                <label class="small">Model hints <select id="bulk-model" class="form-select form-select-sm"><option value="">None</option></select></label>
                <button id="bulk-run" type="button" class="btn btn-sm btn-primary">Lay out</button>
              </div>
            </div>
```

- [ ] **Step 4: Editor code.** Append to `editor.js`:

```js
// ── Bulk add around a patch: candidates you slide, relabel or drop, then save ──
byId('bulk-add-button').addEventListener('click', () => {
  const form = byId('bulk-add-form')
  form.classList.toggle('d-none')
  if (state.selected) byId('bulk-spacing').value = effectiveGeometry(state.selected).crop_size
  const models = document.querySelectorAll('#classify-model option')  // reuse the classify model list
  byId('bulk-model').innerHTML = '<option value="">None</option>' + [...models].filter(o => o.value).map(o => `<option value="${escapeHtml(o.value)}">${escapeHtml(o.textContent)}</option>`).join('')
})

byId('bulk-run').addEventListener('click', async () => {
  const item = state.selected
  if (!item || !markerEditable(item)) return
  try {
    const result = await request(`/api/curation/projects/${state.projectId}/source-images/${state.currentImage.first_item_id}/suggest`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: item.id, grid: Number(byId('bulk-grid').value), spacing_px: Number(byId('bulk-spacing').value),
        crop_size: effectiveGeometry(item).crop_size, model_name: byId('bulk-model').value || null }),
    })
    addCandidates(result.candidates, item.current_label)
    byId('bulk-add-form').classList.add('d-none')
    if (byId('bulk-model').value && !result.model_available) showNotice('No classifier available here; candidates use the source label only.')
  } catch (error) { showNotice(error.message) }
})

function addCandidates(candidates, label) {
  for (const candidate of candidates) {
    state.pending.added.push({ row_px: candidate.row_px, column_px: candidate.column_px, crop_size: candidate.crop_size,
      label, hints: candidate.hints || [] })
  }
  renderPending()
  renderMarkers()
}
```

**Candidates on the image:** `renderMarkers` already draws `state.pending.added` (Task 8).
- Make those markers draggable, with the same drag code; on `pointerup`, update that entry's `row_px` and `column_px`, clamped with `clampToImage`.
- Clicking one opens a small inline chooser in the tray row: a label input (`list="relabel-options"`, from P1) and, when `hints` exist, chips "POCS 0.81" that set the label on click.

**Tray rows for added points:** show the hint chips. Clicking a chip sets `state.pending.added[i].label`. The label input validates with `validRelabelCode` (P1) and stores the code it returns.

- [ ] **Step 5: Keyboard.** Append to `editor.js`:

```js
document.addEventListener('keydown', event => {
  if (state.view !== 'source' || event.target.closest('input, textarea, select, [contenteditable]')) return
  const item = state.selected
  if (event.key === 'ArrowRight' && (event.altKey || !markerEditable(item))) { event.preventDefault(); stepImage(1); return }
  if (event.key === 'ArrowLeft' && (event.altKey || !markerEditable(item))) { event.preventDefault(); stepImage(-1); return }
  if (!markerEditable(item)) return
  const step = event.shiftKey ? 10 : 1
  const geo = effectiveGeometry(item)
  const moves = { ArrowUp: [-step, 0], ArrowDown: [step, 0], ArrowLeft: [0, -step], ArrowRight: [0, step] }
  if (moves[event.key]) {
    event.preventDefault()
    queueMove(item, geo.row_px + moves[event.key][0], geo.column_px + moves[event.key][1])
    renderMarkers()
  } else if (event.key === 'Delete' || event.key === 'Backspace') {
    event.preventDefault()
    queueRemove(item, !geo.removed)
    renderMarkers()
    syncPointTools()
  }
})
```

**`stepImage(direction)`:** move to the previous or next image in the loaded queue (`state.queue` or whatever `loadQueue` fills; check the name). Call `selectImage(nextFirstItemId)` after `confirmDiscardPending()`.

**Arrow-key rule:**
- **Editable point selected:** arrows nudge it; Alt+← / Alt+→ step images.
- **Otherwise:** ← / → step images.

- [ ] **Step 6: Styles.** Append `.bulk-add-form { margin-top: 8px; padding: 8px; border: 1px solid #e6e9ec; border-radius: 6px; }` and `.hint-chip { font-size: 12px; padding: 0 6px; border-radius: 999px; border: 1px solid #c9ced3; background: #fff; cursor: pointer; }`.

- [ ] **Step 7: Run** TEST `tests/test_curation.py` and the JS syntax check. Expected: PASS, SYNTAX-OK.

- [ ] **Step 8: Commit**

```bash
git add templates/curation.html static/curation/editor.js static/curation/curation.js static/curation.css tests/test_curation.py
git commit -m "Bulk add patches around a patch with model hints; keyboard nudge, remove and image stepping"
```

---

### Task 10: Missing images in the UI — filter, sort and Dataset status card

**Files:**
- Modify: `templates/curation.html`:
  - a "Hide missing images" checkbox `#filter-hide-missing` (checked by default) in the filter bar;
  - a "Show removed" checkbox `#filter-show-removed`;
  - a Dataset status card `#missing-card` with `#missing-summary`, `#missing-check` and `#missing-download`.
- Modify: `static/curation/curation.js` (`currentFilters`, `loadDatasetStatus`)
- Test: `tests/test_curation.py`

**Interfaces — Consumes:** the Task 5 routes, and the `source_missing` / `removed` filter parameters (Task 2).

- [ ] **Step 1: Write the failing check**

```python
def test_missing_images_and_removed_filters_are_on_the_page(client):
    page = client.get("/curation").text
    for element in ('id="filter-hide-missing"', 'id="filter-show-removed"', 'id="missing-card"', 'id="missing-summary"',
                    'id="missing-check"', 'id="missing-download"'):
        assert element in page
    script = client.get("/static/curation/curation.js").text
    assert "source_missing" in script and "/availability" in script
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_curation.py::test_missing_images_and_removed_filters_are_on_the_page`
Expected: FAIL.

- [ ] **Step 3: Implement.**
- **`currentFilters()`:** set `source_missing` to `hide` when `#filter-hide-missing` is checked, otherwise `any`. Set `removed` to `any` when `#filter-show-removed` is checked, otherwise leave the server default of `hide`. Changing either box reloads the current view, the same as other filters.
- **Dataset status card,** in `loadDatasetStatus`: fetch `GET …/availability` and render:
  - "N source images missing from the bucket (M points)";
  - a table of region, year and images;
  - "Check again", which POSTs `…/availability` (project curator) and then polls the operation through the existing import progress rendering, or reloads after it completes;
  - "Download CSV" (`…/availability.csv`).

  When no check has run, nothing is flagged, so say "Not checked yet — Check now". Escape every value.

- [ ] **Step 4: Run** TEST `tests/test_curation.py` and the JS syntax check. Expected: PASS, SYNTAX-OK.

- [ ] **Step 5: Commit**

```bash
git add templates/curation.html static/curation/curation.js tests/test_curation.py
git commit -m "Hide missing images (on by default) and show removed points filters; missing-images card in Dataset status"
```

---

### Task 11: Docs, full verification, deployment

**Files:** `docs/CURATION.md`

- [ ] **Step 1: Docs.** Add an "Editing patches on the source image" section to `docs/CURATION.md` covering:
  - moving, resizing, removing and restoring points;
  - the pending tray and Save;
  - bulk add with model hints;
  - keyboard shortcuts;
  - crops cut from source images and cached (`CURATION_IMAGE_CACHE`, `CURATION_IMAGE_CACHE_MB`), never written to buckets;
  - the missing-image check (what it does, when it runs, the CSV);
  - the back bar and image deep links.

  Add the two settings to the data and settings table. Commit.
- [ ] **Step 2:** Full offline suite. Expected: 0 failures.
- [ ] **Step 3:** Full Oracle suite. Expected: 0 failures.
- [ ] **Step 4: Deploy the app only:**

  ```bash
  docker compose -f docker-compose.stack.yml -f docker-compose.evaluation.yml --profile evaluation up -d --build --no-deps oceanframe-app
  ```

  `/healthz` should return `oracle`, and the log should show migration `20261012_01` applied.
- [ ] **Step 5: Check by hand,** as `tester` and `tester2`, on NOAA Pacific T3:
  1. Run the availability check: about 1,422 images flagged, and Hawaii 2024 shows 1,419.
  2. The source queue opens on an image that loads.
  3. Move, resize and remove a point; bulk-add 3×3 around a patch; drag one candidate; drop one; Save.
  4. History shows the change in the commit pill summary.
  5. The crop grid shows the moved patch freshly cut.
  6. Back from the crop grid; a shared image link; keyboard nudge and image stepping.
  7. Run a Playwright browser check, as in P2.
