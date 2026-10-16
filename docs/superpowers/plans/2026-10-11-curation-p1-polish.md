# Curation P1 Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make curation clearer and better informed:
- Include / Hold / Exclude in quality review;
- bulk relabel in the crop grid;
- split breakdowns, live and per version;
- comparison of any two versions;
- one shared header and links to a view;
- background Hugging Face model registration;
- a model cache that persists.

**Architecture:**
- **Server:** small additions to the existing FastAPI routers and the curation service layer. Two new modules: `curation/breakdown.py` (GROUP BY counts) and `curation/registry_worker.py` (a coordinator thread, following the `CurationImportPool` pattern). No new tables.
- **UI:** stays in the classic scripts `static/curation/curation.js` and `static/curation/hub.js`, plus Jinja templates.

**Tech Stack:** FastAPI, SQLAlchemy 2 (Oracle via python-oracledb thin in deployment, SQLite in unit tests), Jinja2, vanilla JS, pytest run in Docker.

**Spec:** `docs/superpowers/specs/2026-10-11-curation-p1-polish-design.md`

## Global Constraints

- **No new tables or columns.** `CurationHubEntry.status` gains the values `queued` and `downloading`; `Str(20)` already fits.
- **Oracle schema rules:** use `Str(n)` and `JSONText` for any column type, and keep identifiers at 30 characters or fewer.
- **Tests run in Docker; there is no local Python.** The test image bakes in the code, so always pass `--build`:
  `MSYS_NO_PATHCONV=1 docker compose --env-file "$TMP/of-dummy.env" run --build --rm test sh -c "pip install -q --no-cache-dir -r requirements-dev.txt && python -m pytest -q <target>"`
  Below this is written as **TEST `<target>`**. The full suite is **TEST `tests`**.
- **Docker:**
  - Never run `docker compose ... down` against `docker-compose.yml` or `docker-compose.stack.yml`, and never pass `-v`.
  - Never touch `.env`, `.env copy` or `docs/*.png`.
- **Commit trailer:** `Co-Authored-By: <the model that wrote the commit> <noreply@anthropic.com>`.
- **Roles:**
  - viewers read;
  - curators curate (bulk quality and bulk relabel use `require_curator`);
  - creation routes use `require_creator` (`CURATION_CREATE_ROLE`, default curator);
  - publishing stays `require_lead`.
- **New UI elements:** every new element that `curation.js` looks up with `byId('…')` must exist in `templates/curation.html`. `tests/test_curation.py::test_curation_page_renders_and_every_scripted_element_exists` enforces this.
- **Escaping:** all user or data text inserted into HTML goes through `escapeHtml()`, which already escapes quotes.
- **Route order:** fixed-segment routes under `/versions/` (`preview`, `compare`) must be declared before `/versions/{number}` in `routers/curation.py`.

## Review Focus

1. Bulk quality in source scope, where two chosen anchors sit on the same image: no error, and every point ends with the last decision. Test in Task 1.
2. Breakdown when the group-by column is null (no region, no annotator): a `(none)` row, not a crash or a dropped count. Test in Task 4.
3. Compare against a version that is building or failed: 422 with a clear message. Never compare half-written rows. Test in Task 6.
4. A queued model whose registrant's account has since been deleted: the entry fails with a readable message, and the worker keeps polling. Test in Task 10.
5. A bulk relabel to a code that isn't in the project's taxonomy: refused in the browser before sending, and the server's 422 is shown if it slips through. The server side is already covered by `_clean_label`; the browser side is checked manually in Task 3.

---

### Task 1: Hold keeps inclusion; bulk quality decisions

**Files:**
- Modify: `curation/schemas.py` (after `CurationQualityReviewRequest`, ~line 238)
- Modify: `curation/service.py:325-364` (`review_quality`), plus a new `bulk_review_quality`
- Modify: `routers/curation.py` (after `review_item_quality`, ~line 370)
- Test: `tests/test_curation.py`

**Interfaces:**
- Produces: `POST /api/curation/projects/{project_id}/quality/bulk`
  - body: `{scope: "patch"|"source", decisions: [{item_id, decision, score?, flags?, expected_revision?}]}`, with 1–500 decisions;
  - response: `{operation_id: str, applied: int, conflicts: [{id, revision, updated_by_name}]}`.
- Produces: `service.bulk_review_quality(db, project_id, request, user) -> tuple[str, int, list[dict]]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_curation.py`)

```python
def test_hold_keeps_inclusion_and_include_exclude_set_it(client):
    pid = _project(client, "Hold")["id"]
    item = _add(client, pid, _item_payload("hold-1", "TURFH"))
    url = f"{P}/projects/{pid}/items/{item['id']}/quality"
    held = client.post(url, json={"decision": "manual_review", "score": 50}).json()
    assert (held["included"], held["quality_decision"], held["review_state"]) == (True, "manual_review", "flagged")
    assert client.post(url, json={"decision": "reject"}).json()["included"] is False
    # Hold parks an item without changing it: an excluded one stays excluded.
    assert client.post(url, json={"decision": "manual_review"}).json()["included"] is False
    included = client.post(url, json={"decision": "accept"}).json()
    assert (included["included"], included["review_state"]) == (True, "reviewed")


def test_bulk_quality_applies_under_one_operation_and_reports_conflicts(client, db_session):
    from curation.models import CurationEvent, CurationItem

    pid = _project(client, "Bulk quality")["id"]
    a = _add(client, pid, _item_payload("bq-1", "TURFH"))
    b = _add(client, pid, _item_payload("bq-2", "ACBR"))
    c = _add(client, pid, _item_payload("bq-3", "SAND"))
    response = client.post(f"{P}/projects/{pid}/quality/bulk", json={"scope": "patch", "decisions": [
        {"item_id": a["id"], "decision": "accept", "score": 90, "expected_revision": a["revision"]},
        {"item_id": b["id"], "decision": "reject", "score": 20, "flags": ["blurry"], "expected_revision": b["revision"]},
        {"item_id": c["id"], "decision": "accept", "expected_revision": c["revision"] + 5},
    ]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == 2
    assert [conflict["id"] for conflict in body["conflicts"]] == [c["id"]]
    db_session.expire_all()
    assert db_session.get(CurationItem, a["id"]).included is True
    assert db_session.get(CurationItem, b["id"]).included is False
    assert db_session.get(CurationItem, b["id"]).quality_flags == ["blurry"]
    assert db_session.get(CurationItem, c["id"]).quality_decision is None
    operations = {event.operation_id for event in db_session.query(CurationEvent)
                  .filter(CurationEvent.event_type == "quality_reviewed")}
    assert operations == {body["operation_id"]}


def test_bulk_quality_source_scope_covers_every_point_even_with_two_anchors_on_one_image(client, db_session):
    from curation.models import CurationItem

    pid = _project(client, "Bulk source quality")["id"]
    first = _item_payload("bs-1", "TURFH")
    a = _add(client, pid, first)
    second = {**_item_payload("bs-2", "ACBR"), "source_image_uri": first["source_image_uri"]}
    b = _add(client, pid, second)
    response = client.post(f"{P}/projects/{pid}/quality/bulk", json={"scope": "source", "decisions": [
        {"item_id": a["id"], "decision": "accept"}, {"item_id": b["id"], "decision": "reject"},
    ]})
    assert response.status_code == 200, response.text
    db_session.expire_all()
    # Both anchors are on one image: the later decision wins for every point on it.
    assert {db_session.get(CurationItem, i).included for i in (a["id"], b["id"])} == {False}


def test_bulk_quality_validates_its_input(client):
    pid = _project(client, "Bulk quality input")["id"]
    a = _add(client, pid, _item_payload("bv-1", "TURFH"))
    url = f"{P}/projects/{pid}/quality/bulk"
    assert client.post(url, json={"decisions": []}).status_code == 422
    twice = [{"item_id": a["id"], "decision": "accept"}] * 2
    assert client.post(url, json={"decisions": twice}).status_code == 422
    assert client.post(url, json={"decisions": [{"item_id": 999999, "decision": "accept"}]}).status_code == 404


def test_bulk_quality_needs_a_curator(team):
    pid = _project(team["admin"], "Bulk quality roles")["id"]
    item = _add(team["admin"], pid, _item_payload("br-1", "TURFH"))
    body = {"decisions": [{"item_id": item["id"], "decision": "accept"}]}
    assert team["vic"].post(f"{P}/projects/{pid}/quality/bulk", json=body).status_code == 403
    assert team["ana"].post(f"{P}/projects/{pid}/quality/bulk", json=body).status_code == 200
```

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_curation.py -k "hold_keeps or bulk_quality"`
Expected: FAIL. Hold currently sets `included=False`, and `/quality/bulk` returns 404 or 405.

- [ ] **Step 3: Add the schemas** in `curation/schemas.py`, directly after `CurationQualityReviewRequest`:

```python
class CurationQualityDecisionIn(BaseModel):
    item_id: int
    decision: QualityDecision
    score: Optional[int] = Field(default=None, ge=0, le=100)
    flags: list[str] = Field(default_factory=list, max_length=20)
    expected_revision: Optional[int] = None


class CurationQualityBulkRequest(BaseModel):
    """Many quality decisions at once, e.g. "Include recommended" in quality review."""

    scope: Literal["patch", "source"] = "patch"
    decisions: list[CurationQualityDecisionIn] = Field(min_length=1, max_length=500)


class CurationQualityBulkResponse(BaseModel):
    operation_id: str
    applied: int
    conflicts: list[CurationConflict] = Field(default_factory=list)
```

`Literal` is already imported (it's used by `QualityDecision`). `CurationConflict` is defined above this point (~line 217).

- [ ] **Step 4: Change the service.** In `curation/service.py`, replace the body of `review_quality` from `reviewed_at = utcnow()` through `db.commit()`, and add `_apply_quality` and `bulk_review_quality`. Add `CurationQualityBulkRequest` to the existing `from curation.schemas import (...)` list.

```python
def _apply_quality(db: Session, item: CurationItem, user: CurrentUser, *, decision: str, score: Optional[int],
                   flags: list[str], reviewed_at, whole_image: bool, operation_id: Optional[str]) -> None:
    previous = {**_state(item), "quality_decision": item.quality_decision}
    item.quality_score = score if score is not None else item.quality_score
    item.quality_flags = list(flags)
    item.quality_decision = decision
    item.quality_reviewed_by = user.label
    item.quality_reviewed_at = reviewed_at
    metadata_key = "quality_review" if whole_image else "patch_quality_review"
    item.metadata_json = {**(item.metadata_json or {}), metadata_key: {
        "decision": decision, "score": item.quality_score, "flags": list(flags),
        "reviewed_by": user.label, "reviewed_at": reviewed_at.isoformat() + "Z",
    }}
    # Include and Exclude decide inclusion; Hold parks the item for a second
    # look and leaves inclusion as it was.
    if decision == "accept":
        item.included = True
    elif decision == "reject":
        item.included = False
    item.review_state = "reviewed" if decision == "accept" else "flagged"
    _record_change(db, item, user, "quality_reviewed", previous, operation_id=operation_id, extra_new={
        "quality_decision": decision, "quality_score": item.quality_score,
        "scope": "source_image" if whole_image else "patch",
    })


def _quality_targets(db: Session, project_id: str, anchor: CurationItem, whole_image: bool) -> list[CurationItem]:
    if not whole_image:
        return [anchor]
    return list(db.scalars(select(CurationItem).where(
        CurationItem.project_id == project_id, CurationItem.source_image_uri == anchor.source_image_uri,
    ).order_by(CurationItem.id)).all())
```

The new `review_quality` body, keeping its signature and docstring and changing the docstring's first line to "Include, exclude or hold an item — or every point on its source image.":

```python
    get_project_or_404(db, project_id)
    anchor = get_item_or_404(db, project_id, anchor_item_id,
                             "Source image not found" if whole_image else "Curation item not found")
    _check_revision(anchor, request.expected_revision)
    reviewed_at = utcnow()
    operation_id = str(uuid.uuid4()) if whole_image else None
    for item in _quality_targets(db, project_id, anchor, whole_image):
        _apply_quality(db, item, user, decision=request.decision, score=request.score, flags=request.flags,
                       reviewed_at=reviewed_at, whole_image=whole_image, operation_id=operation_id)
    db.commit()
    db.refresh(anchor)
    return anchor
```

New function, placed after `review_quality`:

```python
def bulk_review_quality(db: Session, project_id: str, request: CurationQualityBulkRequest,
                        user: CurrentUser) -> tuple[str, int, list[dict]]:
    """Many quality decisions under one operation; stale revisions are skipped and reported."""
    get_project_or_404(db, project_id)
    ids = [decision.item_id for decision in request.decisions]
    if len(set(ids)) != len(ids):
        raise _http(422, "Each item may appear only once")
    anchors = {item.id: item for item in db.scalars(select(CurationItem).where(
        CurationItem.project_id == project_id, in_chunks(CurationItem.id, ids))).all()}
    missing = [item_id for item_id in ids if item_id not in anchors]
    if missing:
        raise _http(404, f"Curation item not found: {', '.join(map(str, missing[:10]))}")
    whole_image = request.scope == "source"
    operation_id = str(uuid.uuid4())
    reviewed_at = utcnow()
    applied, conflicts = 0, []
    for decision in request.decisions:
        anchor = anchors[decision.item_id]
        if decision.expected_revision is not None and anchor.revision != decision.expected_revision:
            conflicts.append({"id": anchor.id, "revision": anchor.revision, "updated_by_name": anchor.updated_by_name})
            continue
        for item in _quality_targets(db, project_id, anchor, whole_image):
            _apply_quality(db, item, user, decision=decision.decision, score=decision.score, flags=decision.flags,
                           reviewed_at=reviewed_at, whole_image=whole_image, operation_id=operation_id)
        applied += 1
    db.commit()
    return operation_id, applied, conflicts
```

- [ ] **Step 5: Add the route** in `routers/curation.py`, directly after `review_item_quality`. Add `CurationQualityBulkRequest` and `CurationQualityBulkResponse` to the router's `from curation.schemas import (...)` list.

```python
@router.post("/projects/{project_id}/quality/bulk", response_model=CurationQualityBulkResponse)
def bulk_review_quality(project_id: str, request: CurationQualityBulkRequest,
                        user: CurrentUser = Depends(require_curator), db: Session = Depends(get_db)):
    operation_id, applied, conflicts = service.bulk_review_quality(db, project_id, request, user)
    return {"operation_id": operation_id, "applied": applied, "conflicts": conflicts}
```

- [ ] **Step 6: Run the new tests and the existing quality tests**

Run: TEST `tests/test_curation.py -k "quality or hold"`
Expected: all PASS, including the existing `test_quality_review_route_persists_decision_for_source_image` and `test_patch_quality_review_only_updates_selected_item`, where reject still excludes.

- [ ] **Step 7: Commit**

```bash
git add curation/schemas.py curation/service.py routers/curation.py tests/test_curation.py
git commit -m "Quality review: Hold keeps inclusion; bulk decisions under one operation"
```

---

### Task 2: Quality review cards say Include / Hold / Exclude

**Files:**
- Modify: `templates/curation.html:158-164` (decision filter), `:182-183` (bulk buttons)
- Modify: `static/curation/curation.js`: `loadQualityReview` (~779-846), `renderQualityList` (~867-919), `applyQualityRecommendations` (~945-963)
- Modify: `static/curation.css` (badge styles)
- Test: `tests/test_curation.py` (the page-integrity test, plus one string check)

**Interfaces:**
- Consumes: `POST /projects/{id}/quality/bulk` from Task 1, and the per-item routes `/items/{id}/quality` and `/source-images/{id}/quality`, which return `CurationItemResponse` with `included`, `revision` and `quality_decision`.

- [ ] **Step 1: Write the failing check** (append to `tests/test_curation.py`)

```python
def test_quality_review_speaks_in_dataset_terms(client):
    page = client.get("/curation").text
    assert ">In dataset</option>" in page and ">Held</option>" in page and ">Excluded</option>" in page
    script = client.get("/static/curation/curation.js").text
    assert "/quality/bulk" in script
    for word in ("'Include'", "'Hold'", "'Exclude'"):
        assert word in script
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_curation.py::test_quality_review_speaks_in_dataset_terms`
Expected: FAIL.

- [ ] **Step 3: Template.** Replace the decision filter options and the two bulk buttons:

```html
          <option value="">All decisions</option>
          <option value="accept">In dataset</option>
          <option value="manual_review">Held</option>
          <option value="reject">Excluded</option>
          <option value="pending" selected>Not reviewed</option>
```

```html
        <button id="quality-accept-all" class="btn btn-sm btn-outline-success" type="button" data-min-role="curator" title="Include every patch the quality check found usable">Include recommended</button>
        <button id="quality-reject-all" class="btn btn-sm btn-outline-danger" type="button" data-min-role="curator" title="Exclude every patch the quality check recommends rejecting">Exclude recommended</button>
```

- [ ] **Step 4: Keep inclusion and revision on each analysed entry.** In `loadQualityReview`, add to the success-branch `results[index] = {...}` object:

```js
            included: patchScope ? detail.included : detail.items.some(item => item.included),
            revision: patchScope ? detail.revision : entry.revision,
```

In the `catch` branch's object, add `included: entry.included ?? true`.

- [ ] **Step 5: Cards.** In `renderQualityList`:
- Replace the `savedDecision` constant with the badges below.
- Replace the `quality-verdict` div and the `quality-actions-row` div.
- Replace the click handler so it updates in place instead of re-analysing all 200 images.

```js
const QUALITY_ACTIONS = [
  ['accept', 'btn-outline-success', 'bi-check-lg', 'Include'],
  ['manual_review', 'btn-outline-secondary', 'bi-pause-fill', 'Hold'],
  ['reject', 'btn-outline-danger', 'bi-x-lg', 'Exclude'],
]

function qualityBadges(item) {
  const membership = item.included === false
    ? '<span class="quality-badge quality-badge-out">Excluded</span>'
    : '<span class="quality-badge quality-badge-in">In dataset</span>'
  const held = item.savedDecision === 'manual_review' ? '<span class="quality-badge quality-badge-held">Held</span>' : ''
  const by = item.reviewedBy ? `<small class="text-muted">by ${escapeHtml(item.reviewedBy)}</small>` : ''
  return `${membership}${held}${by}`
}
```

Inside the card template:

```js
        <div class="quality-verdict"><span class="quality-recommendation">${recommendation}</span>${qualityBadges(item)}</div>
```

```js
        <div class="quality-actions-row">
          ${QUALITY_ACTIONS.map(([action, style, icon, label]) => `<button type="button" class="btn btn-sm ${style} ${item.savedDecision === action ? 'active' : ''}" data-quality-action="${action}" data-quality-id="${item.reviewId}" ${canDo('curator') ? '' : 'disabled'}><i class="bi ${icon}"></i> ${label}</button>`).join('')}
        </div>
```

The click handler:

```js
  document.querySelectorAll('[data-quality-action]').forEach(button => button.addEventListener('click', async () => {
    const reviewId = Number(button.dataset.qualityId)
    const item = state.qualityItems.find(candidate => candidate.reviewId === reviewId)
    if (!item) return
    const saved = await request(qualityReviewEndpoint(item), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision: button.dataset.qualityAction, score: item.quality.score, flags: item.quality.flags || [], expected_revision: item.revision ?? null }),
    })
    item.savedDecision = saved.quality_decision
    item.included = saved.included
    item.revision = saved.revision
    item.reviewedBy = saved.quality_reviewed_by
    renderQualityList()
    loadStats()
  }))
  updateQualityBulkButtons()
```

Call `updateQualityBulkButtons()` before the early `return` for the empty list as well.

- [ ] **Step 6: Bulk through the new route.** Replace `applyQualityRecommendations` and add `updateQualityBulkButtons`:

```js
function qualityCandidates(decision) {
  return state.qualityItems.filter(item => item.quality.decision === decision &&
    !item.quality.flags.includes('analysis_failed') && item.savedDecision !== decision)
}

function updateQualityBulkButtons() {
  byId('quality-accept-all').textContent = `Include recommended (${qualityCandidates('accept').length})`
  byId('quality-reject-all').textContent = `Exclude recommended (${qualityCandidates('reject').length})`
}

async function applyQualityRecommendations(decision) {
  const candidates = qualityCandidates(decision)
  if (!candidates.length) {
    updateQualityProgress(0, 0, `No ${decision === 'accept' ? 'usable' : 'poor'} recommendations left to apply`)
    return
  }
  const verb = decision === 'accept' ? 'Including' : 'Excluding'
  let applied = 0
  const conflicts = []
  for (let start = 0; start < candidates.length; start += 500) {
    const chunk = candidates.slice(start, start + 500)
    updateQualityProgress(start, candidates.length, `${verb} ${candidates.length} recommended…`)
    const result = await request(`/api/curation/projects/${state.projectId}/quality/bulk`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scope: state.qualityScope === 'patch' ? 'patch' : 'source',
        decisions: chunk.map(item => ({ item_id: item.reviewId, decision, score: item.quality.score, flags: item.quality.flags, expected_revision: item.revision ?? null })),
      }),
    })
    applied += result.applied
    conflicts.push(...result.conflicts)
  }
  updateQualityProgress(candidates.length, candidates.length, `${verb.replace('ing', 'ed')} ${applied}`)
  reportConflicts({ updated: applied, conflicts })
  await Promise.all([loadQualityReview(), loadStats()])
}
```

`reportConflicts` (~line 504) already returns early when there are no conflicts.

- [ ] **Step 7: Styles.** Append to `static/curation.css`:

```css
.quality-badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; margin-left: 6px; }
.quality-badge-in { background: #e3f4ef; color: #146c5f; }
.quality-badge-out { background: #eceef0; color: #5b6570; }
.quality-badge-held { background: #fdf1de; color: #8a5a12; }
.quality-actions-row .btn.active { color: #fff; }
.quality-actions-row .btn-outline-success.active { background: #198754; }
.quality-actions-row .btn-outline-secondary.active { background: #6c757d; }
.quality-actions-row .btn-outline-danger.active { background: #dc3545; }
```

- [ ] **Step 8: Run the page tests**

Run: TEST `tests/test_curation.py -k "page_renders or dataset_terms"`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add templates/curation.html static/curation/curation.js static/curation.css tests/test_curation.py
git commit -m "Quality cards: Include / Hold / Exclude with an In dataset badge; bulk in one request"
```

---

### Task 3: Bulk relabel in the crop grid

**Files:**
- Modify: `templates/curation.html:333-338` (the crop bulk group)
- Modify: `static/curation/curation.js`: `updateBulkControls` (~499), `applyBulkAction` (~510), `updateMatrixBulkControls` (~1414), the matrix relabel validity check, and `renderTaxonomyOptions` (~95)
- Test: `tests/test_curation.py`

**Interfaces:**
- Consumes: `POST /projects/{id}/items/bulk` with `{items:[{id, expected_revision}], current_label, reason}`. It already exists; `current_label` is validated server-side by `_clean_label`.
- Produces: a `relabel-options` datalist holding every valid code for the project's mode, and `validRelabelCode(text) -> string|null`. The grid and the matrix both use them.

- [ ] **Step 1: Write the failing check**

```python
def test_crop_grid_offers_bulk_relabel_with_a_reason(client):
    page = client.get("/curation").text
    for element in ('id="bulk-label"', 'id="bulk-reason"', 'id="select-page-crops"', 'id="relabel-options"', '<option value="relabel">Relabel to…</option>'):
        assert element in page
    script = client.get("/static/curation/curation.js").text
    assert "function validRelabelCode(" in script


def test_bulk_relabel_records_the_reason_on_every_event(client, db_session):
    from curation.models import CurationEvent

    pid = _project(client, "Bulk relabel")["id"]
    items = [_add(client, pid, _item_payload(f"rl-{n}", "ACBR")) for n in range(3)]
    response = client.post(f"{P}/projects/{pid}/items/bulk", json={
        "items": [{"id": i["id"], "expected_revision": i["revision"]} for i in items],
        "current_label": "POCS", "reason": "split ACBR vs POCS per new guide"})
    assert response.status_code == 200 and response.json()["updated"] == 3
    events = db_session.query(CurationEvent).filter(CurationEvent.event_type == "bulk_item_updated").all()
    assert {e.reason for e in events} == {"split ACBR vs POCS per new guide"}
    assert len({e.operation_id for e in events}) == 1
```

- [ ] **Step 2: Run them to verify the first fails**

Run: TEST `tests/test_curation.py -k "bulk_relabel"`
Expected: `test_crop_grid_offers_bulk_relabel_with_a_reason` FAILS. `test_bulk_relabel_records_the_reason_on_every_event` PASSES; the server already supports this, and the test pins it.

- [ ] **Step 3: Template.** Replace the crop bulk group with:

```html
        <div class="crop-tool-group crop-action-group" role="group" aria-label="Crop bulk action">
          <button id="select-page-crops" class="btn btn-sm btn-outline-secondary" type="button" title="Select every crop on this page">Select page</button>
          <select id="bulk-action" class="form-select form-select-sm" aria-label="Bulk action" data-min-role="curator">
            <option value="">Bulk action</option><option value="relabel">Relabel to…</option>
            <option value="include">Include</option><option value="exclude">Exclude</option>
            <option value="reviewed">Mark reviewed</option><option value="flagged">Flag</option><option value="unreviewed">Mark unreviewed</option>
          </select>
          <input id="bulk-label" class="form-control form-control-sm d-none" list="relabel-options" placeholder="New label code" aria-label="New label code">
          <input id="bulk-reason" class="form-control form-control-sm" maxlength="4000" placeholder="Reason (optional)" aria-label="Reason for this change">
          <datalist id="relabel-options"></datalist>
          <button id="apply-bulk" class="btn btn-sm btn-primary" type="button" data-min-role="curator" disabled>Apply</button>
        </div>
```

Then change the matrix label input (~line 490) to use the same list: `list="relabel-options"`.

- [ ] **Step 4: Valid codes.** At the end of `renderTaxonomyOptions()`, add the code below. Then add the helper after the function.

```js
  const relabel = byId('relabel-options')
  const codes = new Set()
  state.taxonomy
    .filter(node => node.codes.length && (mode !== 'T1' || node.tier === 'TIER_1'))
    .forEach(node => node.codes.forEach(code => codes.add(code)))
  state.relabelCodes = codes
  relabel.innerHTML = [...codes].sort().map(code => `<option value="${escapeHtml(code)}"></option>`).join('')
```

```js
// A relabel target must be a code the project's taxonomy knows; the server
// refuses anything else, so say so before sending rather than after.
function validRelabelCode(text) {
  const code = (text || '').trim()
  if (!code) return null
  if (!state.relabelCodes || !state.relabelCodes.size) return code  // taxonomy not loaded yet: let the server decide
  return state.relabelCodes.has(code) ? code : null
}
```

- [ ] **Step 5: Grid controls.** Replace `updateBulkControls` and `applyBulkAction`:

```js
function updateBulkControls() {
  const count = state.selectedCropIds.size
  const action = byId('bulk-action').value
  const relabel = action === 'relabel'
  byId('crop-selection-count').textContent = `${count} selected`
  byId('bulk-label').classList.toggle('d-none', !relabel)
  const label = relabel ? validRelabelCode(byId('bulk-label').value) : null
  byId('bulk-label').classList.toggle('is-invalid', relabel && !!byId('bulk-label').value.trim() && !label)
  byId('apply-bulk').disabled = !count || !action || (relabel && !label)
  byId('apply-bulk').textContent = relabel && label && count ? `Relabel ${count} to ${label}` : 'Apply'
}

async function applyBulkAction() {
  const action = byId('bulk-action').value
  if (!action || !state.selectedCropIds.size) return
  // Send the revision each tile was loaded at, so a tile someone else has
  // changed since is left alone and reported rather than overwritten.
  const revisions = new Map(state.crops.map(item => [item.id, item.revision]))
  const payload = {
    items: [...state.selectedCropIds].map(id => ({ id, expected_revision: revisions.get(id) ?? null })),
    reason: byId('bulk-reason').value.trim() || 'Crop grid bulk action',
  }
  if (action === 'relabel') {
    const label = validRelabelCode(byId('bulk-label').value)
    if (!label) { showNotice('Choose a label code from the list'); return }
    payload.current_label = label
  } else if (action === 'include') payload.included = true
  else if (action === 'exclude') payload.included = false
  else payload.review_state = action
  const result = await request(`/api/curation/projects/${state.projectId}/items/bulk`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  })
  reportConflicts(result)
  state.selectedCropIds.clear()
  byId('bulk-reason').value = ''
  await Promise.all([loadCrops(false), loadStats(), loadFacets()])
}
```

Wire the new controls next to the existing `bulk-action` and `apply-bulk` listeners. Find them with `grep -n "byId('apply-bulk').addEventListener" static/curation/curation.js`.

```js
byId('bulk-label').addEventListener('input', updateBulkControls)
byId('select-page-crops').addEventListener('click', () => {
  state.crops.forEach(item => state.selectedCropIds.add(item.id))
  renderCropGrid()
})
```

`renderCropGrid` is the function that ends with `setupGridLasso('crop-grid', …)` (~line 495). If it has a different name, use that function. It already calls `updateBulkControls()`.

- [ ] **Step 6: Matrix uses the same check.** In `updateMatrixBulkControls`, replace `(action === 'relabel' && !byId('matrix-bulk-label').value.trim())` with `(action === 'relabel' && !validRelabelCode(byId('matrix-bulk-label').value))`. In `applyMatrixBulkAction`, replace `byId('matrix-bulk-label').value.trim()` with `validRelabelCode(byId('matrix-bulk-label').value)`.

- [ ] **Step 7: Run**

Run: TEST `tests/test_curation.py -k "bulk_relabel or page_renders"`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add templates/curation.html static/curation/curation.js tests/test_curation.py
git commit -m "Crop grid: bulk relabel with a reason and select page; matrix shares the label check"
```

---

### Task 4: Split breakdown service and routes

**Files:**
- Create: `curation/breakdown.py`
- Modify: `curation/settings.py` (add `rare_min()`)
- Modify: `routers/curation.py` (two GET routes)
- Modify: `.env.example` (document `CURATION_RARE_MIN`, next to `CURATION_CREATE_ROLE`)
- Test: `tests/test_breakdown.py`

**Interfaces:**
- Produces: `breakdown.breakdown(db, project_id: str, *, by: str, version: CurationDatasetVersion | None = None) -> dict`, returning:
  - `by`, `source` (`"live"` or the version number);
  - `splits`: `["train","val","test","unassigned"]`;
  - `rows`: `[{key, counts:{train,val,test,unassigned}, total, rare}]`;
  - `totals`, `total`, `rare_min`.
- Produces routes, both viewer-readable:
  - `GET /api/curation/projects/{id}/breakdown?by=label|region|island|survey_year|annotator`;
  - `GET /api/curation/projects/{id}/versions/{n}/breakdown?by=…`.
- Produces: `settings.rare_min() -> int`.

- [ ] **Step 1: Write the failing tests** in `tests/test_breakdown.py`

```python
"""Split breakdowns: included items per group and split, live and for a published version."""
from __future__ import annotations

from test_curation import P, _add, _item_payload, _project, client, db_session, team  # noqa: F401
from test_versions import _publish

ROWS = [("b1", "ACBR", "train", "Hawaii"), ("b2", "ACBR", "train", "Hawaii"), ("b3", "ACBR", "val", "Mariana"),
        ("b4", "ACBR", "test", "Hawaii"), ("b5", "POCS", "train", "Mariana"), ("b6", "POCS", None, "Hawaii")]


def _seed(client):
    pid = _project(client, "Splits")["id"]
    for key, label, split, region in ROWS:
        payload = _item_payload(key, label, region=region)
        if split:
            payload["split"] = split
        _add(client, pid, payload)
    excluded = _add(client, pid, {**_item_payload("b7", "ACBR"), "split": "train"})
    client.patch(f"{P}/projects/{pid}/items/{excluded['id']}",
                 json={"expected_revision": excluded["revision"], "included": False})
    return pid


def test_live_breakdown_by_label_counts_included_items_per_split(client, monkeypatch):
    monkeypatch.setenv("CURATION_RARE_MIN", "1")
    pid = _seed(client)
    body = client.get(f"{P}/projects/{pid}/breakdown?by=label").json()
    rows = {row["key"]: row for row in body["rows"]}
    assert rows["ACBR"]["counts"] == {"train": 2, "val": 1, "test": 1, "unassigned": 0}
    assert rows["POCS"]["counts"] == {"train": 1, "val": 0, "test": 0, "unassigned": 1}
    assert (rows["ACBR"]["rare"], rows["POCS"]["rare"]) == (False, True)
    assert body["totals"] == {"train": 3, "val": 1, "test": 1, "unassigned": 1} and body["total"] == 6
    assert (body["source"], body["rare_min"], body["by"]) == ("live", 1, "label")
    assert [row["key"] for row in body["rows"]] == ["ACBR", "POCS"]  # largest first


def test_live_breakdown_by_region(client):
    pid = _seed(client)
    rows = {row["key"]: row["counts"] for row in client.get(f"{P}/projects/{pid}/breakdown?by=region").json()["rows"]}
    assert rows == {"Hawaii": {"train": 2, "val": 0, "test": 1, "unassigned": 1},
                    "Mariana": {"train": 1, "val": 1, "test": 0, "unassigned": 0}}


def test_missing_group_values_become_a_none_row(client):
    pid = _project(client, "No annotator")["id"]
    _add(client, pid, {**_item_payload("n1", "ACBR"), "split": "val"})
    rows = client.get(f"{P}/projects/{pid}/breakdown?by=annotator").json()["rows"]
    assert rows == [{"key": "(none)", "counts": {"train": 0, "val": 1, "test": 0, "unassigned": 0},
                     "total": 1, "rare": True}]


def test_version_breakdown_reads_the_frozen_rows(client):
    pid = _seed(client)
    _publish(client, pid)
    body = client.get(f"{P}/projects/{pid}/versions/1/breakdown?by=label").json()
    rows = {row["key"]: row for row in body["rows"]}
    assert body["source"] == 1 and body["total"] == 6 and body["totals"]["unassigned"] == 0
    assert rows["ACBR"]["counts"] == {"train": 2, "val": 1, "test": 1, "unassigned": 0}
    assert rows["POCS"]["total"] == 2  # b6 got a split when v1 was published
    by_region = client.get(f"{P}/projects/{pid}/versions/1/breakdown?by=region").json()
    assert {row["key"] for row in by_region["rows"]} == {"Hawaii", "Mariana"}


def test_breakdown_rejects_unknown_groups_and_versions(client):
    pid = _seed(client)
    assert client.get(f"{P}/projects/{pid}/breakdown?by=colour").status_code == 422
    assert client.get(f"{P}/projects/{pid}/versions/9/breakdown").status_code == 404


def test_breakdown_is_readable_by_viewers(team):
    pid = _project(team["admin"], "Viewer splits")["id"]
    assert team["vic"].get(f"{P}/projects/{pid}/breakdown").status_code == 200
```

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_breakdown.py`
Expected: FAIL with 404 on `/breakdown`.

- [ ] **Step 3: Add the setting** in `curation/settings.py`, after `split_ratios()`:

```python
def rare_min() -> int:
    """CURATION_RARE_MIN: a class (or region…) is rare when val or test holds fewer items than this."""
    try:
        return max(0, int(os.getenv("CURATION_RARE_MIN", "10")))
    except ValueError:
        return 10
```

- [ ] **Step 4: Create `curation/breakdown.py`**

```python
"""
Included items per group and split, for the live draft or a published version.

One GROUP BY per call, so it stays quick on the 941k-item NOAA project. A
published version is read from its frozen ``curation_version_inputs`` rows, so
a past version's breakdown never drifts as items are edited.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from curation import settings
from curation.models import CurationDatasetVersion, CurationItem, CurationVersionInput

GROUPS = ("label", "region", "island", "survey_year", "annotator")
SPLITS = ("train", "val", "test", "unassigned")
NONE_KEY = "(none)"


def breakdown(db: Session, project_id: str, *, by: str,
              version: Optional[CurationDatasetVersion] = None) -> dict:
    if by not in GROUPS:
        raise ValueError(f"Group by one of {', '.join(GROUPS)}")
    if version is None:
        key = CurationItem.current_label if by == "label" else getattr(CurationItem, by)
        split = func.coalesce(CurationItem.split, "unassigned")
        where = [CurationItem.project_id == project_id, CurationItem.included == True]  # noqa: E712
    else:
        key = getattr(CurationVersionInput, by)
        split = CurationVersionInput.split
        where = [CurationVersionInput.version_id == version.id]
    counted = db.execute(select(key, split, func.count()).where(*where).group_by(key, split)).all()

    by_key: dict[str, dict[str, int]] = {}
    for value, split_name, count in counted:
        counts = by_key.setdefault(NONE_KEY if value is None else str(value), dict.fromkeys(SPLITS, 0))
        counts[split_name if split_name in counts else "unassigned"] += count
    rare_min = settings.rare_min()
    rows = [{"key": name, "counts": counts, "total": sum(counts.values()),
             "rare": counts["val"] < rare_min or counts["test"] < rare_min}
            for name, counts in by_key.items()]
    rows.sort(key=lambda row: (-row["total"], row["key"]))
    totals = {name: sum(row["counts"][name] for row in rows) for name in SPLITS}
    return {"by": by, "source": "live" if version is None else version.version, "splits": list(SPLITS),
            "rows": rows, "totals": totals, "total": sum(totals.values()), "rare_min": rare_min}
```

- [ ] **Step 5: Routes.** In `routers/curation.py`:
- Add `from curation import breakdown as split_breakdown`. The name `breakdown` would clash with the route functions.
- Add `GROUP_BY = Query("label", pattern="^(label|region|island|survey_year|annotator)$")` next to the other module-level `Query` constants (`ORIGIN = Query(...)`).
- Add the live route after `get_status` (~line 470).
- Add the version route after `get_version`.

```python
@router.get("/projects/{project_id}/breakdown")
def get_breakdown(project_id: str, by: str = GROUP_BY, db: Session = Depends(get_db)):
    """Included items per class (or region, island, year, annotator) and split, as of now."""
    service.get_project_or_404(db, project_id)
    return split_breakdown.breakdown(db, project_id, by=by)
```

```python
@router.get("/projects/{project_id}/versions/{number}/breakdown")
def get_version_breakdown(project_id: str, number: int, by: str = GROUP_BY, db: Session = Depends(get_db)):
    version = _version_or_404(db, project_id, number)
    if version.status != "published":
        raise HTTPException(status_code=409, detail=f"v{number} is not published")
    return split_breakdown.breakdown(db, project_id, by=by, version=version)
```

- [ ] **Step 6: `.env.example`.** After the `CURATION_CREATE_ROLE` block:

```
# A class (or region…) is flagged rare in the split breakdown when val or test
# holds fewer items than this.
# CURATION_RARE_MIN=10
```

- [ ] **Step 7: Run**

Run: TEST `tests/test_breakdown.py`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add curation/breakdown.py curation/settings.py routers/curation.py .env.example tests/test_breakdown.py
git commit -m "Split breakdown by class, region, island, year or annotator — live and per version"
```

---

### Task 5: Splits card and per-version breakdown in the UI

**Files:**
- Modify: `templates/curation.html` (the dataset status panel, ~347-356)
- Modify: `static/curation/curation.js`: `loadDatasetStatus` (~1470), `renderVersionList` (~609)
- Modify: `static/curation.css`
- Test: `tests/test_curation.py`

**Interfaces:**
- Consumes: the Task 4 routes. The response shape is listed in Task 4's Interfaces.
- Produces: `renderBreakdownTable(container, data, { rareOnly, sortKey })` and `breakdownCsv(data) -> string`.

- [ ] **Step 1: Write the failing check**

```python
def test_status_view_has_a_splits_card(client):
    page = client.get("/curation").text
    for element in ('id="splits-group"', 'id="splits-rare-only"', 'id="splits-table"', 'id="splits-download"'):
        assert element in page
    script = client.get("/static/curation/curation.js").text
    assert "function renderBreakdownTable(" in script and "data-version-breakdown" in script
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_curation.py::test_status_view_has_a_splits_card`
Expected: FAIL.

- [ ] **Step 3: Template.** In `dataset-status-panel`, directly after `<div id="status-kpis" …></div>`:

```html
    <section class="splits-card">
      <div class="curation-panel-title">
        <div><strong>Splits</strong><small>Included items in train / val / test, as publishing now would see them</small></div>
        <div class="splits-tools">
          <select id="splits-group" class="form-select form-select-sm" aria-label="Group splits by">
            <option value="label">By class</option><option value="region">By region</option>
            <option value="island">By island</option><option value="survey_year">By year</option>
            <option value="annotator">By annotator</option>
          </select>
          <label class="form-check form-check-inline small mb-0"><input id="splits-rare-only" class="form-check-input" type="checkbox"> Rare only</label>
          <button id="splits-download" class="btn btn-sm btn-outline-secondary" type="button"><i class="bi bi-download"></i> CSV</button>
        </div>
      </div>
      <div id="splits-table" class="splits-table"></div>
    </section>
```

- [ ] **Step 4: The shared table.** Add to `curation.js`, before `loadDatasetStatus`:

```js
// ── Split breakdowns ─────────────────────────────────────────────────────────
// One table for the live draft (Dataset status) and for each published version.
const SPLIT_COLORS = { train: '#2f6fb0', val: '#e59d3d', test: '#7a4fb5', unassigned: '#b8bec6' }
const splitsState = { data: null, sortKey: 'total' }

function renderBreakdownTable(container, data, { rareOnly = false, sortKey = 'total' } = {}) {
  const pct = (part, whole) => whole ? `${Math.round(part / whole * 100)}%` : '—'
  const rows = data.rows.filter(row => !rareOnly || row.rare)
    .sort((a, b) => sortKey === 'key' ? a.key.localeCompare(b.key) : (sortKey === 'total' ? b.total - a.total : b.counts[sortKey] - a.counts[sortKey]))
  const header = ['key', ...data.splits, 'total'].map(key => `<th class="${key === 'key' ? '' : 'num'}" data-split-sort="${key}" role="button">${key === 'key' ? escapeHtml(data.by.replace('_', ' ')) : key}${sortKey === key ? ' ▾' : ''}</th>`).join('')
  container.innerHTML = `<p class="small text-muted mb-1">${fmt(data.total)} items · train ${pct(data.totals.train, data.total)} · val ${pct(data.totals.val, data.total)} · test ${pct(data.totals.test, data.total)}${data.totals.unassigned ? ` · ${fmt(data.totals.unassigned)} get a split on publish` : ''} · rare = fewer than ${data.rare_min} in val or test</p>
    <div class="table-responsive"><table class="table table-sm align-middle mb-0"><thead><tr>${header}<th>Mix</th></tr></thead><tbody>
    ${rows.length ? rows.map(row => `<tr class="${row.rare ? 'split-rare' : ''}">
      <td>${data.by === 'label' ? `<i class="legend-dot" style="background:${labelColor(row.key)}"></i> ` : ''}<strong>${escapeHtml(row.key)}</strong>${row.rare ? ' <span class="badge text-bg-warning">rare</span>' : ''}</td>
      ${data.splits.map(split => `<td class="num">${fmt(row.counts[split])}<small class="text-muted"> ${pct(row.counts[split], row.total)}</small></td>`).join('')}
      <td class="num"><b>${fmt(row.total)}</b></td>
      <td><span class="split-bar">${data.splits.map(split => row.counts[split] ? `<i title="${split} ${fmt(row.counts[split])}" style="width:${row.counts[split] / row.total * 100}%;background:${SPLIT_COLORS[split]}"></i>` : '').join('')}</span></td>
    </tr>`).join('') : `<tr><td colspan="${data.splits.length + 3}" class="text-muted">Nothing to show.</td></tr>`}
    </tbody></table></div>`
  container.querySelectorAll('[data-split-sort]').forEach(th => th.addEventListener('click', () => {
    renderBreakdownTable(container, data, { rareOnly, sortKey: th.dataset.splitSort })
    if (container.id === 'splits-table') splitsState.sortKey = th.dataset.splitSort
  }))
}

function breakdownCsv(data) {
  const quote = value => `"${String(value).replaceAll('"', '""')}"`
  const lines = [[data.by, ...data.splits, 'total', 'rare'].map(quote).join(',')]
  data.rows.forEach(row => lines.push([row.key, ...data.splits.map(split => row.counts[split]), row.total, row.rare].map(quote).join(',')))
  return lines.join('\n')
}

async function loadSplits() {
  if (!state.projectId) return
  const projectId = state.projectId
  const data = await request(`/api/curation/projects/${projectId}/breakdown?by=${byId('splits-group').value}`)
  if (projectId !== state.projectId) return
  splitsState.data = data
  renderBreakdownTable(byId('splits-table'), data, { rareOnly: byId('splits-rare-only').checked, sortKey: splitsState.sortKey })
}

byId('splits-group').addEventListener('change', () => { splitsState.sortKey = 'total'; loadSplits() })
byId('splits-rare-only').addEventListener('change', () => splitsState.data && renderBreakdownTable(byId('splits-table'), splitsState.data, { rareOnly: byId('splits-rare-only').checked, sortKey: splitsState.sortKey }))
byId('splits-download').addEventListener('click', () => {
  if (!splitsState.data) return
  const link = document.createElement('a')
  link.href = URL.createObjectURL(new Blob([breakdownCsv(splitsState.data)], { type: 'text/csv' }))
  link.download = `splits_by_${splitsState.data.by}.csv`
  link.click()
  URL.revokeObjectURL(link.href)
})
```

`fmt` is defined at ~line 561. It is a `const`, so the code above must sit **after** it in the file; put this block after the versions section. In `loadDatasetStatus`, add `loadSplits().catch(error => showNotice(error.message))` as its first line.

- [ ] **Step 5: Per-version expander.** In `renderVersionList`, inside the `version-links` div for published versions, add a button. Below the links, add an empty container and the two summary lines.

```js
        <button type="button" class="btn btn-sm btn-outline-secondary" data-version-breakdown="${v.version}"><i class="bi bi-table"></i> Breakdown</button>
```

```js
      ${v.status === 'published' ? `<div class="version-meta">Leakage: ${fmt(v.statistics?.cross_split_source_images)} source images span more than one split · split origins: ${Object.entries(v.statistics?.split_origins || {}).map(([origin, count]) => `${escapeHtml(origin)} ${fmt(count)}`).join(', ') || '—'}</div>
      <div class="version-breakdown d-none" data-version-breakdown-body="${v.version}"></div>` : ''}
```

After the existing `data-export-images` listener loop:

```js
  document.querySelectorAll('[data-version-breakdown]').forEach(button => button.addEventListener('click', async () => {
    const number = button.dataset.versionBreakdown
    const body = document.querySelector(`[data-version-breakdown-body="${number}"]`)
    const opening = body.classList.contains('d-none')
    body.classList.toggle('d-none', !opening)
    if (!opening || body.dataset.loaded) return
    body.innerHTML = '<span class="small text-muted">Loading…</span>'
    try {
      renderBreakdownTable(body, await request(`${base}/${number}/breakdown?by=label`))
      body.dataset.loaded = '1'
    } catch (error) { body.innerHTML = `<span class="small text-danger">${escapeHtml(error.message)}</span>` }
  }))
```

If `statistics.cross_split_source_images` or `split_origins` doesn't exist under that name, read `versions.py:286-314` and use the actual keys.

- [ ] **Step 6: Styles.** Append to `static/curation.css`:

```css
.splits-card { margin: 12px 0 18px; }
.splits-tools { display: flex; gap: 8px; align-items: center; }
.split-bar { display: flex; width: 160px; height: 10px; border-radius: 5px; overflow: hidden; background: #eef0f2; }
.split-bar i { display: block; height: 100%; }
tr.split-rare td { background: #fff8e6; }
.version-breakdown { margin-top: 8px; }
```

- [ ] **Step 7: Run**

Run: TEST `tests/test_curation.py -k "splits_card or page_renders"`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add templates/curation.html static/curation/curation.js static/curation.css tests/test_curation.py
git commit -m "Dataset status: Splits card with rare classes and CSV; per-version breakdown"
```

---

### Task 6: Compare any two versions (server)

**Files:**
- Modify: `curation/versions.py`: `compare` (106-152), plus a new `resolve_snapshot`
- Modify: `routers/curation.py` (a route after `preview_version`)
- Test: `tests/test_version_compare.py`

**Interfaces:**
- Produces: `versions.resolve_snapshot(db, project_id: str, ref: str) -> tuple[subquery, str]`. `ref` is `"live"` or a version number as text. It raises `LookupError` for a missing version and `ValueError` for an unpublished one or bad text.
- Produces: `versions.compare` gains these keys:
  - top level: `"moved": int` and `"split_moves": [{"from","to","count"}]`, sorted by count descending;
  - per class: `"moved": int` and `"moved_to": {split: count}`.
- Produces: `GET /api/curation/projects/{id}/versions/compare?base=<n|live>&target=<n|live>` → `{base: "v1"|"live", target, changes}`.

- [ ] **Step 1: Write the failing tests** in `tests/test_version_compare.py`

```python
"""Comparing any two published versions, or a version with the live draft."""
from __future__ import annotations

from test_curation import P, _add, _project, client, db_session, team  # noqa: F401
from test_versions import _on_image, _publish


def _two_versions(client, db_session):
    from curation.models import CurationItem

    pid = _project(client, "Compare")["id"]
    a = _add(client, pid, {**_on_image("c1", "ACBR", "img-1"), "split": "train"})
    b = _add(client, pid, {**_on_image("c2", "ACBR", "img-2"), "split": "train"})
    s = _add(client, pid, {**_on_image("c3", "SAND", "img-3"), "split": "val"})
    _publish(client, pid, "first")
    client.patch(f"{P}/projects/{pid}/items/{a['id']}", json={"expected_revision": a["revision"], "current_label": "POCS"})
    client.patch(f"{P}/projects/{pid}/items/{s['id']}", json={"expected_revision": s["revision"], "included": False})
    moved = db_session.get(CurationItem, b["id"])
    moved.split = "test"
    db_session.commit()
    _publish(client, pid, "second")
    return pid


def test_compare_two_versions_reports_relabels_exclusions_and_split_moves(client, db_session):
    pid = _two_versions(client, db_session)
    body = client.get(f"{P}/projects/{pid}/versions/compare?base=1&target=2").json()
    changes = body["changes"]
    assert (body["base"], body["target"]) == ("v1", "v2")
    assert (changes["before"], changes["after"], changes["in"], changes["out"],
            changes["relabelled"], changes["moved"]) == (3, 2, 0, 1, 1, 1)
    assert changes["split_moves"] == [{"from": "train", "to": "test", "count": 1}]
    per_class = {row["label"]: row for row in changes["per_class"]}
    assert per_class["ACBR"]["moved"] == 1 and per_class["ACBR"]["moved_to"] == {"test": 1}
    assert per_class["SAND"]["out"] == 1


def test_compare_with_live(client, db_session):
    pid = _two_versions(client, db_session)
    reverse = client.get(f"{P}/projects/{pid}/versions/compare?base=live&target=1").json()
    assert reverse["base"] == "live" and reverse["changes"]["in"] == 1  # SAND is excluded live, present in v1


def test_compare_refuses_bad_pairs(client, db_session):
    from curation.models import CurationDatasetVersion

    pid = _two_versions(client, db_session)
    url = f"{P}/projects/{pid}/versions/compare"
    assert client.get(f"{url}?base=1&target=1").status_code == 422
    assert client.get(f"{url}?base=7&target=1").status_code == 404
    assert client.get(f"{url}?base=abc&target=1").status_code == 422
    db_session.add(CurationDatasetVersion(project_id=pid, version=3, status="failed", item_count=0))
    db_session.commit()
    response = client.get(f"{url}?base=2&target=3")
    assert response.status_code == 422 and "not published" in response.json()["detail"]


def test_viewers_can_compare(team):
    pid = _project(team["admin"], "Viewer compare")["id"]
    assert team["vic"].get(f"{P}/projects/{pid}/versions/compare?base=live&target=1").status_code == 404
```

Before running, check which constructor arguments `CurationDatasetVersion` actually requires (`curation/models.py`, ~line 200). Add any other NOT NULL columns to the `failed` row in `test_compare_refuses_bad_pairs`.

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_version_compare.py`
Expected: FAIL. `/versions/compare` is matched by `/versions/{number}` and returns 422 for every request.

- [ ] **Step 3: Split moves in `compare`.** In `curation/versions.py`, inside `compare`, add this query after `relabels`:

```python
    moves = db.execute(
        select(base.c.split, target.c.split, target.c.label, func.count())
        .select_from(base.join(target, target.c.item_id == base.c.item_id))
        .where(base.c.split != target.c.split)
        .group_by(base.c.split, target.c.split, target.c.label)
    ).all()
    moved_to: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    move_matrix: dict[tuple[str, str], int] = defaultdict(int)
    for old_split, new_split, label, count in moves:
        moved_to[label][new_split] += count
        move_matrix[(old_split, new_split)] += count
```

In the per-class loop, extend `row` and the keep condition:

```python
            "relabelled_out": relabel_out.get(label, 0), "after": after.get(label, 0),
            "moved": sum(moved_to[label].values()) if label in moved_to else 0,
            "moved_to": dict(moved_to[label]) if label in moved_to else {},
        }
        row["net"] = row["after"] - row["before"]
        if row["net"] or row["in"] or row["out"] or row["relabelled_in"] or row["relabelled_out"] or row["moved"]:
            per_class.append(row)
```

Use `label in moved_to`, not `moved_to[label]`, so the `defaultdict` doesn't grow while reading. In the returned dict, add:

```python
        "moved": sum(move_matrix.values()),
        "split_moves": [{"from": old, "to": new, "count": count}
                        for (old, new), count in sorted(move_matrix.items(), key=lambda kv: -kv[1])],
```

- [ ] **Step 4: `resolve_snapshot`.** Add after `_snapshot`:

```python
def resolve_snapshot(db: Session, project_id: str, ref: str):
    """'live' or a published version number → (an item_id/label/split subquery, its name)."""
    if ref == "live":
        return _live(project_id), "live"
    try:
        number = int(ref)
    except ValueError:
        raise ValueError(f"'{ref}' is neither 'live' nor a version number") from None
    version = db.scalar(select(CurationDatasetVersion).where(
        CurationDatasetVersion.project_id == project_id, CurationDatasetVersion.version == number))
    if version is None:
        raise LookupError(f"No version v{number}")
    if version.status != "published":
        raise ValueError(f"v{number} is not published ({version.status})")
    return _snapshot(version.id), f"v{number}"
```

- [ ] **Step 5: Route.** In `routers/curation.py`, directly after `preview_version` and before `@router.get("/projects/{project_id}/versions/{number}")`:

```python
@router.get("/projects/{project_id}/versions/compare")
def compare_versions(project_id: str, base: str = Query(..., max_length=12), target: str = Query(..., max_length=12),
                     db: Session = Depends(get_db)):
    """What changed between two published versions, or a version and the live draft."""
    service.get_project_or_404(db, project_id)
    if base == target:
        raise HTTPException(status_code=422, detail="Choose two different versions to compare")
    try:
        base_set, base_name = versions.resolve_snapshot(db, project_id, base)
        target_set, target_name = versions.resolve_snapshot(db, project_id, target)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"base": base_name, "target": target_name, "changes": versions.compare(db, base_set, target_set)}
```

- [ ] **Step 6: Run these tests and the existing version tests**

Run: TEST `tests/test_version_compare.py tests/test_versions.py`
Expected: PASS. `test_two_versions_record_what_came_in_went_out_and_was_relabelled` still passes, because preview and publish gain the same additive keys.

- [ ] **Step 7: Commit**

```bash
git add curation/versions.py routers/curation.py tests/test_version_compare.py
git commit -m "Compare any two versions, or a version and live, including split moves"
```

---

### Task 7: Compare bar in the Versions view

**Files:**
- Modify: `templates/curation.html` (top of `versions-panel`, ~line 368)
- Modify: `static/curation/curation.js`: `loadVersions` and `renderVersionList`, plus new `runCompare` and `renderCompare`
- Test: `tests/test_curation.py`

**Interfaces:**
- Consumes: `GET /versions/compare` from Task 6.
- Consumes: the existing crop-grid class filter. Clicking a class runs `state.filters` the same way the "Add code" filter does: call the function behind the `add-code` button with that label, then `setView('crop')`. Find it with `grep -n "function addCodeFilters" static/curation/curation.js`.

- [ ] **Step 1: Write the failing check**

```python
def test_versions_view_has_a_compare_bar(client):
    page = client.get("/curation").text
    for element in ('id="compare-base"', 'id="compare-target"', 'id="compare-run"', 'id="compare-result"'):
        assert element in page
    assert "function renderCompare(" in client.get("/static/curation/curation.js").text
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_curation.py::test_versions_view_has_a_compare_bar`
Expected: FAIL.

- [ ] **Step 3: Template.** First child of `<section id="versions-panel" …>`:

```html
    <section class="compare-bar">
      <strong>Compare</strong>
      <select id="compare-base" class="form-select form-select-sm" aria-label="Compare from"></select>
      <span>→</span>
      <select id="compare-target" class="form-select form-select-sm" aria-label="Compare to"></select>
      <button id="compare-run" class="btn btn-sm btn-outline-primary" type="button"><i class="bi bi-arrow-left-right"></i> Compare</button>
      <div id="compare-result" class="compare-result"></div>
    </section>
```

- [ ] **Step 4: Script.** In `renderVersionList(list)`, at the top, fill both selects with published versions plus Live. Keep the current choices when the values still exist.

```js
  const published = list.filter(v => v.status === 'published').map(v => v.version)
  const options = [...published.map(n => `<option value="${n}">v${n}</option>`), '<option value="live">Live draft</option>'].join('')
  for (const [id, fallback] of [['compare-base', published[1] ?? published[0]], ['compare-target', published[0] !== undefined && published.length > 1 ? published[0] : 'live']]) {
    const select = byId(id)
    const keep = select.value
    select.innerHTML = options
    select.value = [...select.options].some(o => o.value === keep) ? keep : String(fallback ?? 'live')
  }
  byId('compare-run').disabled = !published.length
```

`list` is sorted newest first, so `published[0]` is the latest.

Add, after `renderVersionList`:

```js
async function runCompare() {
  const base = byId('compare-base').value
  const target = byId('compare-target').value
  const box = byId('compare-result')
  if (base === target) { box.innerHTML = '<span class="small text-muted">Choose two different versions.</span>'; return }
  box.innerHTML = '<span class="small text-muted">Comparing…</span>'
  try {
    renderCompare(await request(`/api/curation/projects/${state.projectId}/versions/compare?base=${encodeURIComponent(base)}&target=${encodeURIComponent(target)}`))
  } catch (error) { box.innerHTML = `<span class="small text-danger">${escapeHtml(error.message)}</span>` }
}

function renderCompare(result) {
  const c = result.changes
  const name = value => value === 'live' ? 'live' : value
  byId('compare-result').innerHTML = `
    <div class="status-kpis">${[[`In ${name(result.base)}`, fmt(c.before)], ['Came in', `+${fmt(c.in)}`], ['Went out', `−${fmt(c.out)}`],
      ['Relabelled', fmt(c.relabelled)], ['Changed split', fmt(c.moved)], [`In ${name(result.target)}`, fmt(c.after)]]
      .map(([label, value]) => `<div><strong>${value}</strong><span>${escapeHtml(label)}</span></div>`).join('')}</div>
    ${c.split_moves.length ? `<p class="small mb-1">Split moves: ${c.split_moves.map(m => `${escapeHtml(m.from)} → ${escapeHtml(m.to)} ${fmt(m.count)}`).join(' · ')}</p>` : ''}
    <div class="table-responsive versions-table-wrap"><table class="table table-sm align-middle mb-0">
      <thead><tr><th>Class</th><th class="num">Before</th><th class="num">In</th><th class="num">Out</th><th class="num">Relabelled in</th><th class="num">Relabelled out</th><th class="num">Moved split</th><th class="num">After</th><th class="num">Net</th></tr></thead>
      <tbody>${c.per_class.length ? c.per_class.map(row => `<tr>
        <td><a href="#" data-compare-class="${escapeHtml(row.label)}"><i class="legend-dot" style="background:${labelColor(row.label)}"></i> <strong>${escapeHtml(row.label)}</strong></a></td>
        <td class="num">${fmt(row.before)}</td><td class="num delta-in">${row.in ? '+' + fmt(row.in) : ''}</td>
        <td class="num delta-out">${row.out ? '−' + fmt(row.out) : ''}</td>
        <td class="num">${row.relabelled_in ? fmt(row.relabelled_in) : ''}</td><td class="num">${row.relabelled_out ? fmt(row.relabelled_out) : ''}</td>
        <td class="num" title="${escapeHtml(Object.entries(row.moved_to || {}).map(([s, n]) => `${n} to ${s}`).join(', '))}">${row.moved ? fmt(row.moved) : ''}</td>
        <td class="num">${fmt(row.after)}</td>
        <td class="num ${row.net > 0 ? 'delta-in' : row.net < 0 ? 'delta-out' : ''}">${row.net > 0 ? '+' : ''}${row.net ? fmt(row.net) : '0'}</td>
      </tr>`).join('') : '<tr><td colspan="9" class="text-muted">No differences.</td></tr>'}</tbody></table></div>
    ${c.relabels.length ? `<p class="small mt-2 mb-0">Top relabels: ${c.relabels.slice(0, 10).map(r => `${escapeHtml(r.from)} → ${escapeHtml(r.to)} ${fmt(r.count)}`).join(' · ')}</p>` : ''}`
  byId('compare-result').querySelectorAll('[data-compare-class]').forEach(link => link.addEventListener('click', event => {
    event.preventDefault()
    byId('filter-label').value = link.dataset.compareClass
    addCodeFilters()
    setView('crop')
  }))
}

byId('compare-run').addEventListener('click', runCompare)
```

If `addCodeFilters` reads its input differently, follow its body. The goal is "filter the crop grid to this class", the same as typing the code and pressing +.

- [ ] **Step 5: Styles.** Append to `static/curation.css`:

```css
.compare-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 14px; }
.compare-bar select { width: auto; }
.compare-result { flex-basis: 100%; }
```

- [ ] **Step 6: Run**

Run: TEST `tests/test_curation.py -k "compare_bar or page_renders"`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add templates/curation.html static/curation/curation.js static/curation.css tests/test_curation.py
git commit -m "Versions view: compare any two versions or live, with split moves; click a class to see it"
```

---

### Task 8: One shared header

**Files:**
- Modify: `templates/_header.html`
- Modify: `templates/library.html:14-46`, `templates/collections.html:14-45`, `templates/collection_form.html:14-38`, `templates/index.html:14-49`, `templates/curation.html:14`, `templates/account.html`, `templates/users.html`
- Modify: `static/app.css` (`.nav-link.active`, `.nav-divider`)
- Modify: `docs/IMAGE_LIBRARY.md` (the library is at `/`, not `/library`)
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: the `_header.html` variables `brand_name`, `tagline`, `active`, `header_extra` and `page_actions` (the last two set with `{% set %}…{% endset %}`), plus the existing `hide_nav`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_api.py`)

```python
def test_every_page_has_the_same_main_navigation(client):
    import re

    orders = {}
    for path in ("/", "/collections", "/collections/new", "/frames", "/curation"):
        page = client.get(path)
        assert page.status_code == 200, path
        orders[path] = tuple(re.findall(r'class="nav-link nav-main[^"]*"[^>]*>([^<]+)</a>', page.text))
    assert set(orders.values()) == {("Library", "Curation", "Frame Analyser")}, orders
    assert re.search(r'nav-main active"[^>]*>Curation<', client.get("/curation").text)
    assert re.search(r'nav-main active"[^>]*>Library<', client.get("/").text)
    assert 'id="open-datasets"' in client.get("/").text  # page-specific links survive
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_api.py::test_every_page_has_the_same_main_navigation`
Expected: FAIL; no page has `nav-main` yet.

- [ ] **Step 3: Rewrite `templates/_header.html`**

```jinja
{# Site header shared by every page, so the main links never move.
   Before including, optionally set:
     brand_name    – defaults to "OceanFrame"
     tagline       – the small line under the brand
     active        – library | curation | frames, to highlight that main link
     header_extra  – markup between the brand and the nav (status chips, a session switcher)
     page_actions  – page-specific links, shown after the main links
     hide_nav      – true on the sign-in page
   using {% set name %}…{% endset %} for the markup ones. #}
<header class="site-header">
  <div class="header-inner">
    <a class="header-brand" href="/">
      <svg class="brand-icon" viewBox="0 0 32 32" fill="none">
        <path d="M4 20 Q8 14 12 18 Q16 22 20 16 Q24 10 28 14" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>
        <circle cx="16" cy="12" r="4" stroke="currentColor" stroke-width="2"/>
        <line x1="16" y1="16" x2="16" y2="22" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
      </svg>
      <div>
        <h1 class="brand-name">{{ brand_name | default('OceanFrame') }}</h1>
        <p class="brand-tagline">{{ tagline | default('') }}</p>
      </div>
    </a>
    {{ header_extra | default('') }}

    {% if not hide_nav %}
    <nav class="header-nav" aria-label="Main">
      {% for href, key, label in [("/", "library", "Library"), ("/curation", "curation", "Curation"), ("/frames", "frames", "Frame Analyser")] %}
      <a href="{{ href }}" class="nav-link nav-main{% if active == key %} active{% endif %}"{% if active == key %} aria-current="page"{% endif %}>{{ label }}</a>
      {% endfor %}
      {% if page_actions is defined %}<span class="nav-divider" aria-hidden="true"></span>{{ page_actions }}{% endif %}
    </nav>
    {% include "_user_menu.html" %}
    {% endif %}
    <div class="team-badge" title="OceanFrame is built and maintained by the OPTICS SI team">
      <span class="team-badge-text">an app by</span>
      <img src="/static/img/optics-si.png" alt="OPTICS SI" class="team-logo" />
    </div>
  </div>
</header>
```

- [ ] **Step 4: Switch pages to the partial.** Replace each hand-written `<header class="site-header">…</header>` with `{% set %}` lines plus `{% include "_header.html" %}`. Keep each page's brand, tagline, extras and links exactly.

`library.html`:

```jinja
{% set brand_name = "OceanFrame Library" %}
{% set tagline = "Image search, tagging & datasets" %}
{% set active = "library" %}
{% set header_extra %}
    <div class="header-status">
      <span id="status-assets" class="stat-chip">— assets</span>
      <span id="status-embedder" class="stat-chip" title="Active embedding backend">—</span>
    </div>
{% endset %}
{% set page_actions %}
      <a href="#" id="open-datasets" class="nav-link">Datasets</a>
      <a href="/collections" class="nav-link">My collections</a>
      <a href="/collections/new" class="nav-link nav-link-alt">+ Add collection</a>
{% endset %}
{% include "_header.html" %}
```

The other pages follow the same pattern:
- **`collections.html`:** brand "OceanFrame Library", tagline "My collections", `active = "library"`, the same `header-status` extra, and `page_actions` = `+ Add collection`.
- **`collection_form.html`:** brand "OceanFrame Library", tagline `{{ 'Edit' if source_id else 'Add' }} collection` (write it as `{% set tagline = ('Edit' if source_id else 'Add') ~ ' collection' %}`), `active = "library"`, and `page_actions` = `My collections`. Copy any `header-status` block it has at lines 27-37.
- **`index.html`:** tagline "Marine Video Frame Analysis", `active = "frames"`, `header_extra` = the `session-switcher` div exactly as it is now, and `page_actions` = the four in-page anchors (Upload, Parameters, Frames, Export). Drop "← Image Library"; the main Library link replaces it.
- **`curation.html`:** add `{% set active = "curation" %}` before its include.
- **`account.html` and `users.html`:** unchanged apart from the new partial; no `active`.

- [ ] **Step 5: Styles.** Append to `static/app.css`, after `.nav-link-alt:hover`:

```css
.nav-link.active { color: var(--primary); background: var(--primary-light); font-weight: 600; }
.nav-divider { width: 1px; align-self: stretch; margin: 4px 6px; background: rgba(0, 0, 0, .12); }
```

- [ ] **Step 6: Docs.** In `docs/IMAGE_LIBRARY.md`, change `/library` to `/` where it names the library's address (lines 8 and 64). Mention that `/library` still redirects.

- [ ] **Step 7: Run the header test and every page and API test**

Run: TEST `tests/test_api.py tests/test_auth.py tests/test_curation.py -k "navigation or page or landing or library_url or login"`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add templates/ static/app.css docs/IMAGE_LIBRARY.md tests/test_api.py
git commit -m "One shared header: Library · Curation · Frame Analyser in the same place on every page"
```

---

### Task 9: Links to a view

**Files:**
- Modify: `static/curation/curation.js`: `setView` (~531), `loadProjects` (~69), the `project-select` change handler (~2035), the `matrix-source` change listener (~1958), `loadEvaluationCatalog` (where it fills `matrix-source`), and `init` (~2104)
- Test: `tests/test_curation.py`

**Interfaces:**
- Produces: the URL hash format `#project=<id>&view=<source|crop|quality|status|matrix|versions>[&run=<run id>]`, and the functions `readHash()` and `writeHash()`.

- [ ] **Step 1: Write the failing check**

```python
def test_curation_remembers_the_view_in_the_url(client):
    script = client.get("/static/curation/curation.js").text
    for needle in ("function readHash()", "function writeHash()", "history.replaceState", "'hashchange'"):
        assert needle in script
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_curation.py::test_curation_remembers_the_view_in_the_url`
Expected: FAIL.

- [ ] **Step 3: Hash helpers.** Add before `setView`:

```js
// ── Links to a view ──────────────────────────────────────────────────────────
// The hash keeps project, view and evaluation run, so a refresh or a shared
// link lands where it was: /curation#project=<id>&view=versions
const VIEWS = ['source', 'crop', 'quality', 'status', 'matrix', 'versions']

function readHash() {
  const params = new URLSearchParams(window.location.hash.slice(1))
  const view = params.get('view')
  return { project: params.get('project'), view: VIEWS.includes(view) ? view : null, run: params.get('run') }
}

function writeHash() {
  if (!state.projectId) return
  const params = new URLSearchParams({ project: state.projectId, view: state.view })
  const run = state.view === 'matrix' ? selectedRunId() : null
  if (run) params.set('run', run)
  const next = `#${params}`
  if (window.location.hash !== next) history.replaceState(null, '', next)
}
```

- [ ] **Step 4: Wire it in.**
- At the end of `setView`, add `writeHash()`.
- In `loadProjects` and at the end of the `project-select` change handler, replace `setView('matrix')` with:

```js
    setView(state.pendingView || 'matrix')
    state.pendingView = null
```

- In the `matrix-source` change listener, add `writeHash()` as its last statement.
- In `loadEvaluationCatalog`, right after it sets `byId('matrix-source').innerHTML`:

```js
  if (state.pendingRun && [...byId('matrix-source').options].some(option => option.value === `run:${state.pendingRun}`)) {
    byId('matrix-source').value = `run:${state.pendingRun}`
  }
  state.pendingRun = null
```

- In `init`, replace `try { state.projectId = localStorage.getItem('curation-project') } catch (_) {}` with:

```js
  const linked = readHash()
  state.pendingView = linked.view
  state.pendingRun = linked.run
  try { state.projectId = linked.project || localStorage.getItem('curation-project') } catch (_) { state.projectId = linked.project }
```

`loadProjects` already falls back to the first project when `state.projectId` is unknown. Then add, near the other `window.addEventListener` calls:

```js
window.addEventListener('hashchange', () => {
  const linked = readHash()
  if (linked.project && linked.project !== state.projectId && state.projects.some(project => project.id === linked.project)) {
    state.pendingView = linked.view
    state.pendingRun = linked.run
    const select = byId('project-select')
    select.value = linked.project
    select.dispatchEvent(new Event('change'))
  } else if (linked.view && linked.view !== state.view) {
    setView(linked.view)
  }
})
```

- [ ] **Step 5: Run**

Run: TEST `tests/test_curation.py -k "remembers_the_view or page_renders"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add static/curation/curation.js tests/test_curation.py
git commit -m "Curation URL keeps project, view and run, so refresh and shared links land in place"
```

---

### Task 10: Background model registration

**Files:**
- Create: `curation/registry_worker.py`
- Modify: `curation/registry.py:203-237` (`register_model` queues; remove `_discard`, which becomes unused); make sure `summary()` includes `status` and `error_message`
- Modify: `routers/registry.py` (202 for queued registrations, plus a retry route)
- Modify: `main.py` (lifespan starts and stops the worker)
- Modify: `static/curation/hub.js` (`hubRegisterModel`, the registry list status column with Retry, polling)
- Modify: `docs/CURATION.md` (the registry section: registration runs in the background on the coordinator)
- Test: `tests/test_registry.py`

**Interfaces:**
- Produces in `curation/registry_worker.py`:
  - `claim_next() -> Optional[int]`
  - `process(entry_id: int) -> None`
  - `recover() -> int`
  - `run_pending() -> int`
  - the `RegistryWorker` class with `start()` and `stop(timeout)`
  - the module singleton `registry_worker`
- Produces: `POST /hub/models` returns **202** with `status: "queued"` for runnable models, and still 201 for catalog-only ones. `POST /hub/entries/{entry_id}/retry` returns 202 for a failed model entry, 409 otherwise, and is guarded by `require_creator`.
- Model entry statuses: `queued → downloading → ready | failed`.

- [ ] **Step 1: Write the failing tests.**

Replace `test_register_model_streams_the_chosen_weights` and `test_losing_the_registration_race_drops_the_extra_classifier` in `tests/test_registry.py` with the tests below, and add the rest.

```python
def test_register_model_queues_then_the_worker_registers_the_weights(hub_client):
    from appdb import session_scope
    from curation import registry_worker
    from curation.models import CurationClassifier

    response = hub_client.post(f"{P}/hub/models", json={
        "ref": f"https://huggingface.co/{MODEL}", "weight_file": "weights/best.pt", "trusted": True})
    assert response.status_code == 202, response.text
    entry = response.json()
    assert entry["status"] == "queued" and entry["classifier_id"] is None and entry["runnable"] is True

    assert registry_worker.run_pending() == 1
    listed = next(item for item in hub_client.get(f"{P}/hub/entries?kind=model").json() if item["id"] == entry["id"])
    assert listed["status"] == "ready" and listed["classifier_id"]
    assert listed["trained_on"] == [DATASET]
    models = hub_client.get(f"{P}/evaluation-models").json()["models"]
    assert next(m for m in models if m["id"] == listed["classifier_id"])["name"] == "yolo11m-cls-noaa-pacific-benthic-cover-t3"
    with session_scope() as db:
        provenance = db.get(CurationClassifier, listed["classifier_id"]).provenance
    assert provenance == {"source": "huggingface", "repo_id": MODEL, "revision": MODEL_SHA,
                          "file": "weights/best.pt", "license": "agpl-3.0", "trusted": True}


def test_registering_the_same_commit_twice_makes_one_entry_and_one_classifier(hub_client):
    from appdb import session_scope
    from curation import registry_worker
    from curation.models import CurationClassifier

    body = {"ref": MODEL, "kind": "model", "weight_file": "weights/best.pt", "trusted": True}
    first = hub_client.post(f"{P}/hub/models", json=body).json()
    second = hub_client.post(f"{P}/hub/models", json=body)
    assert second.status_code == 202 and second.json()["id"] == first["id"]
    registry_worker.run_pending()
    with session_scope() as db:
        assert db.query(CurationClassifier).count() == 1


def test_a_failed_download_is_kept_on_the_entry_and_can_be_retried(hub_client, monkeypatch):
    from fastapi import HTTPException

    from curation import huggingface, registry_worker

    entry = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                     "weight_file": "weights/best.pt", "trusted": True}).json()
    real = huggingface.download

    def down(*args):
        raise HTTPException(502, "Hugging Face is unreachable")

    monkeypatch.setattr(huggingface, "download", down)
    registry_worker.run_pending()
    failed = next(i for i in hub_client.get(f"{P}/hub/entries?kind=model").json() if i["id"] == entry["id"])
    assert failed["status"] == "failed" and "unreachable" in failed["error_message"]

    monkeypatch.setattr(huggingface, "download", real)
    retried = hub_client.post(f"{P}/hub/entries/{entry['id']}/retry")
    assert retried.status_code == 202 and retried.json()["status"] == "queued"
    registry_worker.run_pending()
    ready = next(i for i in hub_client.get(f"{P}/hub/entries?kind=model").json() if i["id"] == entry["id"])
    assert ready["status"] == "ready" and ready["error_message"] is None
    assert hub_client.post(f"{P}/hub/entries/{entry['id']}/retry").status_code == 409


def test_a_download_interrupted_by_a_restart_is_queued_again(hub_client):
    from appdb import session_scope
    from curation import registry_worker
    from curation.models import CurationHubEntry

    entry = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                     "weight_file": "weights/best.pt", "trusted": True}).json()
    with session_scope() as db:
        db.get(CurationHubEntry, entry["id"]).status = "downloading"
    assert registry_worker.recover() == 1
    with session_scope() as db:
        assert db.get(CurationHubEntry, entry["id"]).status == "queued"


def test_a_registrant_who_no_longer_exists_fails_the_entry_clearly(hub_client):
    from appdb import session_scope
    from curation import registry_worker
    from curation.models import CurationHubEntry

    entry = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                     "weight_file": "weights/best.pt", "trusted": True}).json()
    with session_scope() as db:
        db.get(CurationHubEntry, entry["id"]).created_by_user_id = None
    registry_worker.run_pending()
    with session_scope() as db:
        stored = db.get(CurationHubEntry, entry["id"])
        assert stored.status == "failed" and "register it again" in stored.error_message


def test_retry_needs_the_create_role(team):
    assert team["vic"].post(f"{P}/hub/entries/1/retry").status_code == 403
    assert team["ana"].post(f"{P}/hub/entries/1/retry").status_code == 404
```

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_registry.py -k "queues_then or same_commit_twice or failed_download or interrupted or no_longer_exists or retry_needs"`
Expected: FAIL. The route returns 201, and `curation.registry_worker` doesn't exist.

- [ ] **Step 3: Queue instead of downloading.** In `curation/registry.py` `register_model`, replace everything from `local = huggingface.download(...)` to the end of the function with the code below, and delete `_discard`. Check first with `grep -n "_discard" curation/ routers/ tests/` that nothing else uses it.

```python
    # The weights are fetched by the coordinator (curation/registry_worker.py),
    # so this request returns at once; checks above already ran.
    display_name = (name or info.repo_id.split("/", 1)[1])[:200]
    return _create(db, info, parsed.ref, user, runnable=True, status="queued",
                   options={"weight_file": weight_file, "name": display_name})
```

Then confirm `summary()` (~line 256) returns `"status": entry.status` and `"error_message": entry.error_message`, and add them if missing.

- [ ] **Step 4: Create `curation/registry_worker.py`**

```python
"""
Background registration of Hugging Face models: fetch the weights, then register them.

``POST /hub/models`` checks the request and queues an entry; the slow part
(downloading a checkpoint that may be hundreds of MB) happens here, on the one
coordinator per database (``OCEANFRAME_BACKGROUND_WORKERS``). A second app on
the same database, like the T3 review, can queue registrations; this process
runs them.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, update

from appdb.engine import session_scope
from auth.models import User
from auth.service import snapshot
from curation import huggingface, model_registry
from curation.models import CurationClassifier, CurationHubEntry

logger = logging.getLogger(__name__)
POLL_SECONDS = 5.0


def claim_next() -> Optional[int]:
    with session_scope() as db:
        entry_id = db.scalar(select(CurationHubEntry.id).where(
            CurationHubEntry.kind == "model", CurationHubEntry.status == "queued",
        ).order_by(CurationHubEntry.id).limit(1))
        if entry_id is None:
            return None
        # Conditional update: if another worker got there first, rowcount is 0.
        claimed = db.execute(update(CurationHubEntry).where(
            CurationHubEntry.id == entry_id, CurationHubEntry.status == "queued",
        ).values(status="downloading", error_message=None)).rowcount
        return entry_id if claimed else None


def _message(exc: Exception) -> str:
    text = exc.detail if isinstance(exc, HTTPException) else str(exc)
    return (str(text).strip() or type(exc).__name__)[:2000]


def process(entry_id: int) -> None:
    """Download and register one claimed entry. A failure is kept on the entry, never raised."""
    try:
        with session_scope() as db:
            entry = db.get(CurationHubEntry, entry_id)
            owner = db.get(User, entry.created_by_user_id) if entry.created_by_user_id else None
            if owner is None:
                raise RuntimeError("The person who registered this model no longer has an account; register it again")
            weight_file = entry.options["weight_file"]
            local = huggingface.download("model", entry.repo_id, entry.revision, weight_file)
            with local.open("rb") as stream:
                classifier = model_registry.register_stream(
                    db, stream, name=entry.options.get("name") or entry.repo_id.split("/", 1)[1],
                    filename=weight_file, trusted=True, user=snapshot(owner),
                    provenance={"source": "huggingface", "repo_id": entry.repo_id, "revision": entry.revision,
                                "file": weight_file, "license": (entry.card or {}).get("license")})
            entry.classifier_id = classifier.id
            entry.status = "ready"
            entry.error_message = None
            classifier_id = classifier.id
    except Exception as exc:  # noqa: BLE001 — the entry carries the reason
        logger.warning("Registering Hugging Face model entry %s failed", entry_id, exc_info=True)
        with session_scope() as db:
            db.execute(update(CurationHubEntry).where(CurationHubEntry.id == entry_id)
                       .values(status="failed", error_message=_message(exc)))
        return
    _validate(classifier_id)


def _validate(classifier_id: str) -> None:
    """Start validation in the isolated worker when it is online, as the dialog used to."""
    try:
        with session_scope() as db:
            model = db.get(CurationClassifier, classifier_id)
            if model is not None and model.status in {"staged", "failed"} and model_registry.worker_available():
                model_registry.submit_validation(db, model)
    except Exception:  # noqa: BLE001 — the model list still offers Validate
        logger.warning("Validation of classifier %s did not start", classifier_id, exc_info=True)


def recover() -> int:
    """A download that was running when the coordinator stopped starts again."""
    with session_scope() as db:
        return db.execute(update(CurationHubEntry).where(
            CurationHubEntry.kind == "model", CurationHubEntry.status == "downloading",
        ).values(status="queued")).rowcount


def run_pending() -> int:
    """Process every queued entry now, in this thread (tests, and a manual nudge)."""
    done = 0
    while (entry_id := claim_next()) is not None:
        process(entry_id)
        done += 1
    return done


class RegistryWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            recovered = recover()
            if recovered:
                logger.info("Re-queued %d interrupted model registration(s)", recovered)
        except Exception:  # noqa: BLE001 — database briefly unavailable; the loop retries
            logger.exception("Could not recover interrupted model registrations")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="curation-registry", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                entry_id = claim_next()
            except Exception:  # noqa: BLE001
                logger.exception("Could not poll for model registrations")
                entry_id = None
            if entry_id is None:
                self._stop.wait(POLL_SECONDS)
                continue
            process(entry_id)


registry_worker = RegistryWorker()
```

Confirm the imports before running:
- `grep -n "^class User" auth/models.py` (the ORM user class);
- `grep -n "^def snapshot" auth/service.py`;
- `grep -n "def worker_available" curation/model_registry.py`.

Adjust the names if they differ.

- [ ] **Step 5: Routes.** In `routers/registry.py`, add `Response` to the `fastapi` import, then:

```python
@router.post("/models", status_code=201)
def register_model(body: RegisterModelIn, response: Response, user: CurrentUser = Depends(require_creator),
                   db: Session = Depends(get_db)):
    entry = registry.register_model(db, user, ref=body.ref, kind=body.kind or "model",
                                    weight_file=body.weight_file, name=body.name, trusted=body.trusted)
    if entry.status in {"queued", "downloading"}:
        response.status_code = 202  # the weights download in the background
    return registry.summary(db, entry)


@router.post("/entries/{entry_id}/retry", status_code=202)
def retry_entry(entry_id: int, user: CurrentUser = Depends(require_creator), db: Session = Depends(get_db)):
    entry = registry.get_entry_or_404(db, entry_id, "model")
    if entry.status != "failed":
        raise HTTPException(status_code=409, detail="Only a failed registration can be retried")
    entry.status = "queued"
    entry.error_message = None
    db.commit()
    db.refresh(entry)
    return registry.summary(db, entry)
```

- [ ] **Step 6: Lifespan.** In `main.py`:
- add `from curation.registry_worker import registry_worker` next to the other curation imports;
- in the `if workers:` start block, add `registry_worker.start()`;
- in the shutdown block, add `await run_in_threadpool(registry_worker.stop)` before the evaluation pool stops.

- [ ] **Step 7: Run the registry suite**

Run: TEST `tests/test_registry.py`
Expected: PASS. Any other existing test that posts a runnable model and expects 201 or an immediate `classifier_id` must now expect 202 and call `registry_worker.run_pending()` first. Update only those assertions. Catalog-only tests keep 201.

- [ ] **Step 8: `hub.js`.** Replace `hubRegisterModel` and `openRegistry`, and add polling:

```js
const hubWatched = new Set()
let hubWatchTimer = null

async function hubRegisterModel() {
  hubBusy(true)
  let entry
  try {
    entry = await request('/api/curation/hub/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: byId('hub-ref').value.trim(), kind: 'model', weight_file: hub.preview?.runnable ? byId('hub-weight-file').value : null,
        name: byId('hub-model-name').value.trim() || null, trusted: byId('hub-model-trusted').checked }) })
  } catch (error) { hubMessage(error.message, true); hubBusy(false); return }
  hubEntriesCache = null
  byId('hub-dialog').close()
  hubBusy(false)
  if (['queued', 'downloading'].includes(entry.status)) {
    hubWatched.add(entry.id)
    showNotice(`Downloading ${entry.repo_id} in the background. It joins the model list when it is ready and is validated automatically when the worker is online.`)
    watchRegistrations()
  } else if (state.projectId) {
    loadEvaluationCatalog().catch(error => showNotice(error.message))
  }
}

// While any model registration is queued or downloading, check every 3 s and
// say when one finishes or fails.
async function watchRegistrations() {
  clearTimeout(hubWatchTimer)
  let entries
  try { entries = await request('/api/curation/hub/entries?kind=model') } catch (_) { hubWatchTimer = setTimeout(watchRegistrations, 10000); return }
  const pending = new Set(entries.filter(entry => ['queued', 'downloading'].includes(entry.status)).map(entry => entry.id))
  let finished = false
  for (const entry of entries) {
    if (!hubWatched.has(entry.id) || pending.has(entry.id)) continue
    hubWatched.delete(entry.id)
    finished = true
    if (entry.status === 'failed') showNotice(`Registering ${entry.repo_id} failed: ${entry.error_message || 'unknown error'}. Retry it from the Registry panel.`)
    else showNotice(`${entry.repo_id} is registered${entry.classifier_id ? ' and in the model list' : ''}.`)
  }
  pending.forEach(id => hubWatched.add(id))
  if (byId('hub-registry-dialog').open) renderRegistry(await request('/api/curation/hub/entries'))
  if (finished && state.projectId) loadEvaluationCatalog().catch(error => showNotice(error.message))
  if (pending.size) hubWatchTimer = setTimeout(watchRegistrations, 3000)
}

const HUB_STATUS = { queued: 'queued', downloading: 'downloading…', ready: 'ready', failed: 'failed', registered: 'registered' }

function renderRegistry(entries) {
  const list = byId('hub-registry-list')
  list.innerHTML = entries.length ? `<table class="table table-sm"><thead><tr><th>Repository</th><th>Kind</th><th>Task</th><th>Status</th><th>Use</th><th>Registered</th></tr></thead><tbody>${entries.map(entry => `
    <tr><td><a href="${escapeHtml(entry.url)}" target="_blank" rel="noreferrer">${escapeHtml(entry.repo_id)}</a> <code>${escapeHtml(entry.short_revision)}</code>${entry.matches_t3_baseline ? ' <span class="badge text-bg-info">T3 v0</span>' : ''}</td>
    <td>${escapeHtml(entry.kind)}</td><td>${escapeHtml(entry.task || '—')}${entry.runnable ? '' : ` <small class="text-muted">(${escapeHtml(entry.not_runnable_reason || 'catalog only')})</small>`}</td>
    <td>${escapeHtml(HUB_STATUS[entry.status] || entry.status)}${entry.error_message ? `<br><small class="text-danger">${escapeHtml(entry.error_message)}</small>` : ''}${entry.status === 'failed' && entry.kind === 'model' && canDo('create') ? ` <button type="button" class="btn btn-sm btn-outline-primary" data-hub-retry="${entry.id}">Retry</button>` : ''}</td>
    <td>${entry.kind === 'model' ? (entry.classifier_id ? 'classifier registered' : '—') : (entry.projects.map(p => escapeHtml(p.name)).join(', ') || '—')}</td>
    <td>${escapeHtml(entry.created_by_name || '')}<br><small>${new Date(entry.created_at).toLocaleString()}</small></td></tr>`).join('')}</tbody></table>`
    : '<p class="text-muted">Nothing registered yet. Add datasets from "New project" and models from the confusion matrix view.</p>'
  list.querySelectorAll('[data-hub-retry]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true
    try {
      const entry = await request(`/api/curation/hub/entries/${button.dataset.hubRetry}/retry`, { method: 'POST' })
      hubWatched.add(entry.id)
      watchRegistrations()
    } catch (error) { showNotice(error.message); button.disabled = false }
  }))
}

async function openRegistry() {
  hubEntriesCache = null
  byId('hub-registry-dialog').showModal()
  byId('hub-registry-list').textContent = 'Loading…'
  try { renderRegistry(await request('/api/curation/hub/entries')) } catch (error) { byId('hub-registry-list').textContent = error.message }
}
```

At the end of `hub.js`, start watching anything already pending, for example after a page reload mid-download:

```js
watchRegistrations()
```

- [ ] **Step 9: Docs.** In `docs/CURATION.md`, in the Hugging Face models paragraph, say that registering returns at once. The coordinator app downloads the weights in the background, then registers and validates them. A failed download shows in the Registry panel with Retry.

- [ ] **Step 10: Run the registry suite and the page tests**

Run: TEST `tests/test_registry.py tests/test_curation.py -k "hub or registry or page_renders"`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add curation/registry.py curation/registry_worker.py routers/registry.py main.py static/curation/hub.js docs/CURATION.md tests/test_registry.py
git commit -m "Hugging Face models register in the background on the coordinator; retry failed ones"
```

---

### Task 11: The model cache persists

**Files:**
- Modify: `library/modelcache.py`: `_writable_cache_dir` (28-57) and `resolve` (163-176)
- Test: `tests/test_modelcache.py` (new)

**Interfaces:**
- Produces:
  - `modelcache.resolve("yolo11n.pt")` returns `<writable cache dir>/yolo11n.pt` when the file isn't cached yet. Ultralytics downloads its release assets to the exact path it is given.
  - The writable cache dir is the first writable of `settings.MODEL_DIR`, then `settings.DATA_DIR / "models"`, then the temp dir.

- [ ] **Step 1: Write the failing tests** in `tests/test_modelcache.py`

```python
"""Model weights are cached where they survive a container being recreated."""
from __future__ import annotations


def test_a_bare_ultralytics_name_resolves_into_the_cache(tmp_path, monkeypatch):
    from library import modelcache, settings

    monkeypatch.setattr(settings, "MODEL_DIR", tmp_path / "models")
    assert modelcache.resolve("yolo11n.pt") == str(tmp_path / "models" / "yolo11n.pt")


def test_an_unwritable_model_dir_falls_back_to_the_data_volume(tmp_path, monkeypatch):
    from library import modelcache, settings

    blocked = tmp_path / "models"
    blocked.write_text("a file where the directory should be")  # mkdir fails
    monkeypatch.setattr(settings, "MODEL_DIR", blocked)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    assert modelcache.resolve("yolo11n.pt") == str(tmp_path / "data" / "models" / "yolo11n.pt")


def test_cached_and_absolute_references_are_unchanged(tmp_path, monkeypatch):
    from library import modelcache, settings

    monkeypatch.setattr(settings, "MODEL_DIR", tmp_path)
    (tmp_path / "sam3.pt").write_bytes(b"weights")
    assert modelcache.resolve("sam3.pt") == str(tmp_path / "sam3.pt")
    absolute = str(tmp_path / "elsewhere" / "custom.pt")
    assert modelcache.resolve(absolute) == absolute
```

- [ ] **Step 2: Run them to verify they fail**

Run: TEST `tests/test_modelcache.py`
Expected: the first two FAIL, because the bare name comes back unchanged.

- [ ] **Step 3: Implement.** In `library/modelcache.py`, replace `_writable_cache_dir` with:

```python
def _is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".oceanframe-write-test"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _writable_cache_dir() -> Path:
    """
    Where downloaded weights are written.

    ``LIB_MODEL_DIR`` is frequently a read-only or differently-owned mount — a
    bind-mounted ./models owned by the host user while the container runs as
    another uid is the common case — so fall back to the data volume, which
    survives the container being recreated, and only then to a temp dir.
    Weights already present in the configured directory are still used from
    there; only downloads move.
    """
    global _warned
    for directory in (settings.MODEL_DIR, settings.DATA_DIR / "models"):
        if _is_writable(directory):
            return directory
    fallback = Path(tempfile.gettempdir()) / "oceanframe-models"
    fallback.mkdir(parents=True, exist_ok=True)
    if not _warned:
        _warned = True
        log.warning(
            "Neither %s nor %s is writable; caching downloaded weights in %s instead. "
            "They will be re-downloaded when the container restarts.",
            settings.MODEL_DIR, settings.DATA_DIR / "models", fallback,
        )
    return fallback
```

In `resolve`, replace the non-`gs://` branch with:

```python
    if not ref.startswith("gs://"):
        if ref and Path(ref).name == ref:
            for directory in (settings.MODEL_DIR, settings.DATA_DIR / "models",
                              Path(tempfile.gettempdir()) / "oceanframe-models"):
                candidate = directory / ref
                if candidate.exists() and candidate.stat().st_size > 0:
                    return str(candidate)
            # Not cached yet. Hand Ultralytics a path inside the cache: it
            # downloads its own release assets (yolo11n.pt, …) to exactly that
            # path, instead of the working directory a recreated container loses.
            return str(_writable_cache_dir() / ref)
        return ref
```

Update the `resolve` docstring bullet for bare names to say "→ a path in the model cache, where Ultralytics downloads it". Also check `settings.DATA_DIR` exists in `library/settings.py` (`grep -n "^DATA_DIR" library/settings.py`).

- [ ] **Step 4: Run these tests and the annotator and status tests**

Run: TEST `tests/test_modelcache.py tests/test_api.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add library/modelcache.py tests/test_modelcache.py
git commit -m "Model weights cache on the data volume when ./models is not writable; bare names download there"
```

---

### Task 12: Full verification and deployment

**Files:** none new. `docs/CURATION.md` already gained notes in Tasks 4 and 10. Add one short paragraph on the quality buttons, splits and compare under the existing quality and versions headings.

- [ ] **Step 1: Docs paragraph.** In `docs/CURATION.md`, cover:
- **Quality review:** Include, Hold and Exclude; "Hold leaves inclusion as it was".
- **Dataset status:** Splits, with `CURATION_RARE_MIN`.
- **Versions view:** Compare, with split moves.

Commit: `git commit -m "CURATION.md: include/hold/exclude, splits, compare"`.

- [ ] **Step 2: Offline suite.** Run TEST `tests`. Expected: everything passes, with 0 failures.
- [ ] **Step 3: Oracle suite.** Run `docker compose -f docker-compose.stack.yml --profile test run --build --rm test-oracle`. Expected: everything passes.
- [ ] **Step 4: Deploy only the app container**

```bash
docker compose -f docker-compose.stack.yml -f docker-compose.evaluation.yml --profile evaluation up -d --build --no-deps oceanframe-app
curl -s localhost:8080/healthz   # {"status":"ok","app_db":"oracle"}
```

- [ ] **Step 5: Check by hand,** signed in as `tester` (password in `$TMP/of-tester-password.txt`; never print it):
  1. **Quality review:** Include / Hold / Exclude; bulk "Include recommended (N)" sends one request (watch the app log for one `POST …/quality/bulk`).
  2. **Crop grid:** relabel 3 crops with a reason. Dataset status → activity shows them under one operation, with the reason.
  3. **Splits card** on "NOAA Pacific T3": `time curl` on `/breakdown?by=label` comes back in under 2 s.
  4. **Versions:** compare v1 vs Live on the HF test project.
  5. **Header:** the same order on `/`, `/collections`, `/frames` and `/curation`.
  6. **View links:** refresh on Versions stays on Versions.
  7. **Background registration:** register a second public HF classifier. The dialog closes at once, and the entry goes queued → downloading → ready.
  8. **Model cache:** recreate the app container with the same command; `docker logs oceanframe-app | grep -c "Downloading.*yolo11n"` stays at 0 after the library page loads.
