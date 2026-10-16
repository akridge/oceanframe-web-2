# Follow-ups from curation P2 (collaboration)

Deferred in task and final reviews; the final whole-branch review triaged every item, and the fix-before-merge ones were fixed.

## Residual found in deployment

- **Releases and version-side project compare read only `curation_version_inputs`.** A version published before those rows existed (NOAA Pacific T3 v1, 2026-10-02: 0 input rows, 941,550 membership rows) is silently treated as empty. Fix: refuse such a version with a 422 ("published before releases were supported — publish a new version"), or fall back to `curation_version_items` joined to live item fields. The test release "Pacific T3 deployment check" r1 (56,864 items) shows the effect.

## Not yet documented

- Project compare's label/split disagreement counts are per matched row pair; a crop held twice on one side multiplies them. Document it in docs/CURATION.md.

## Deferred minors

- Task 1: minor (deferred): unused P/_test_buckets imports in test_collab_schema (fixture import intended); `number` column is an Oracle reserved word — raw SQL must quote it
- Task 1: minor (deferred): upgrade test checks index name only, not its columns
- Task 2: minor (deferred): evaluate no longer honours CURATION_CREATE_ROLE (comment); test_bulk_quality_needs_a_curator name loose; members test gaps (app-lead non-member, guard 404, non-lead PATCH/DELETE, link apply lead vs non-lead); _check_role duplicates pydantic pattern; add_member race → 500; LIKE wildcards unescaped; link apply on missing project 403 not 404
- Task 3: minor (deferred): window reports change set's total count not in-window count; detail item_ids unbounded; missing coverage (in_window, since_version/before/limit, lead viewing others' pending, concurrent double commit)
- Task 2: minor (deferred): no test for duplicate-name 409 on POST /projects
- Task 3: minor (deferred): race test doesn't assert stored summary excludes the late edit; long lines in change_sets.py
- Task 4: minor (deferred): no v2 window test (earlier change set excluded); no old-version changelog render test; poll loop doesn't assert published; local import mid-function; empty name renders " 1"
- Task 5: minor (deferred): no test pins the no-op-not-a-latest-value rule; overlap counts no-op events (undocumented: "both touched"); file_name/current loop runs over rows beyond MAX_ROWS; change_count not window-limited; people not checked for membership
- Task 6: minor (deferred): no tests for unpublished-version 422 / non-numeric ref 422; "²".isdigit() → 500 (use isdecimal); repeats tested on B only; published-version test asserts matched only; check a (version_id, source_label, file_name) index on curation_version_inputs at 941k scale
- Task 7: minor (deferred): 422 before 403 for empty sources; kept duplicate depends on source order; changelog compares only published previous releases; files listed even if local folder gone (no GCS fallback); leakage counts within-project too
- Task 8: minor (deferred): render-time canDo controls not re-rendered after a role change; user_id unescaped (numeric); test string-only; Releases icon reused
- Task 9: minor (deferred): history reloads on every 3 s version-build poll
- Task 7: minor (deferred): agreeing rows retained for every duplicated (non-conflicting) key — memory grows with duplicates (ruling)
- Task 9: minor (deferred): showItems silent on empty; matrixFilter shape from showItems lacks actual/predicted if user opens Matrix view; pill not refreshed after role change; test string-only
- Task 8: minor (deferred): refreshMyRole calls applyRoleGates without re-running bulk control updates; refreshMyRole inside memberAction try makes a post-success refresh error look like a failed add
- Task 10: minor (deferred): "Show in A" click lacks catch; releaseBreakdown rare_min 0 footer text; null disagreement values render "null"; fillPeople not guarded against mid-flight project switch
- Task 9: minor (deferred): person options miss former members outside the newest 50 change sets; history caches reset lazily not on switchProject; history list capped at 50 with no paging
