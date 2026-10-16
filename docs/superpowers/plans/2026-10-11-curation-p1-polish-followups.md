# Follow-ups from curation P1 (polish, splits, compare, background registration)

Deferred in task and final reviews; the final whole-branch review triaged every item as safe to defer (browser checks were then run and passed).

## Residual found in deployment

- Signed-out user opening a shared `/curation#…&view=…` link: the server redirects to `/login` and the browser keeps the fragment on the login URL, but `static/auth/login.js` redirects to `safeNext(form.dataset.next)` without `location.hash`, so the view is lost. One-line fix: append `location.hash` when it is not `#signup`.

## Deferred minors

- Task 1: minor (deferred): source scope with two anchors on one image applies the whole-image decision twice (double events/revision bumps; result correct)
- Task 1: minor (deferred): no source-scope test with a stale anchor, no event-count/operation-id assertion in source-scope tests
- Task 1: minor (deferred): no bulk manual_review (hold) assertion
- Task 1: minor (deferred): routers/curation.py schema import list formatting uneven
- Task 2: minor (deferred): source scope sends expected_revision null on first decision — use anchor item's revision from detail.items
- Task 2: minor (deferred): failed analysis in source scope shows "In dataset" (entry.included undefined → true)
- Task 2: minor (deferred): quality card click handler has no error handling (409 → unhandled rejection notice only)
- Task 2: minor (deferred): Include recommended skips items with savedDecision accept but later excluded by a plain edit
- Task 2: minor (deferred): bulk button titles say "patch" in source scope
- Task 2: minor (deferred): test is string-only; browser check pending (final verification, Playwright image available)
- Task 3: minor (deferred): UI test string-only; matrix relabel input lacks is-invalid feedback; applyMatrixBulkAction could send null label (unreachable)
- Task 4: minor (deferred): unreachable ValueError in breakdown(); no 409/unpublished-version test; island/year/annotator untested on versions; rare_min invalid-env fallback untested
- Task 5: minor (deferred): splits test string-only; loadSplits lacks request sequencing on quick group changes; open version breakdown collapses on 3s re-render during builds; SPLIT_COLORS has no fallback
- Task 6: minor (deferred): viewer compare test only asserts 404; int(ref) accepts "01"/"+1" so base=1&target=01 compares v1 with itself; commit 5fcef0f lists test file before it exists; no assertion of new keys in stored version changes
- Task 7: minor (deferred): identity `name` helper is dead code; class labels with spaces split by addCodeFilters; test string-only
- Task 8: minor (deferred): brand link may underline on hover on pages without library.css (move a.header-brand rule to app.css); active state only asserted for Library/Curation; nav-link-alt margin after divider (visual)
- Task 9: minor (deferred): pendingView/pendingRun cleared only on success path; hashchange ignores run-only changes; test string-only
- Task 10: minor (deferred): register_stream commits classifier before entry is linked (narrow window → duplicate classifier on retry); process() holds a DB session during download; entry.id not escaped in data-hub-retry; test gaps (concurrent claim, non-HTTP failure, workers-off, retry 409 for queued); duplicate import lines in test_hub_evaluation
- Task 10: minor (deferred): uncapped 10 s retry while something is watched; extra blank lines in test_registry.py
- Task 11: minor (deferred): _writable_cache_dir repeats the dir pair instead of reusing _cache_dirs(); writability probe runs on every uncached resolve; no all-unwritable fallback test
- Task 12: minor (deferred): CURATION.md omits crop-grid bulk Relabel (reason is OPTIONAL) + Select page, and that the URL keeps project/view/run; "class, region, island, year or annotator" should say it's a selector
