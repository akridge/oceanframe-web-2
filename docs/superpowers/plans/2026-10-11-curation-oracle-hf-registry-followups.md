# Follow-ups from the Oracle + Hugging Face registry build

Minor issues found in review and deliberately deferred. None blocked merge, per the final whole-branch review's triage. Good candidates for sub-project D.

- Task 1: minor (deferred): no positive/parametrized test for require_oracle() truthy values
- Task 1: minor (deferred): badge test cannot exercise the oracle branch under SQLite
- Task 1: minor (deferred): backend_name() reads env while the engine is cached (plan-mandated; note only)
- Task 2: minor (deferred): test-oracle lists test files created in T3/T9 (fails until those land; plan-mandated R2)
- Task 2: minor (deferred): t3-review needs the oceanframe-local network from `make up` first (documented in header)
- Task 2: minor (deferred): dev profile now depends on Oracle (needs passwords to actually run); by design per spec
- Task 3: minor (deferred): download()/404/RevisionNotFound/GatedRepoError raise paths and browse_url untested; endpoint kwarg not asserted
- Task 3: minor (deferred): download() maps repo/revision-missing and gated to 502 (plan said 502/404); broad except at the boundary (plan-mandated)
- Task 3: minor (deferred): URL regex ignores slashed revisions, /commit/ links, other tails (fall to 422, never a wrong repo)
- Task 3: minor (deferred): fakes patch module attributes — later code must call huggingface.* via the module (plan code does)
- Task 4: minor (deferred): size cap skips files without recorded size; 413 may arrive after earlier splits yielded
- Task 4: minor (deferred): detect_columns grammar ("a image"); message says gs:// only though GCS https accepted
- Task 4: minor (deferred): crop_uri keeps https query strings / no URL-decoding; storage.cloud.google.com not recognised
- Task 4: minor (deferred): fnmatch `*` crosses `/` (slightly looser than HF globs)
- Task 5: minor (deferred): weight-file size cap skipped when listing omits size; hub_max_bytes not applied to weight download
- Task 5: minor (deferred): summary scan is O(entries x projects)
- Task 5: minor (deferred): DB session held open across weight download/hash
- Task 5: minor (deferred): no tests for 409 different weight file / 413 cap
- Task 5: minor (deferred): _discard unlink can race a third concurrent registration of identical bytes; keep-file branch untested
- Task 6: minor (deferred): label_report scans the whole dataset per request with no cache
- Task 6: minor (deferred): source_key truncated to 400 chars (collision only for very long names)
- Task 6: minor (deferred): no cancel test for hub imports
- Task 7: minor (deferred): link apply is read-modify-write on item.revision without a revision check; a concurrent curator edit during a link can be overwritten
- Task 7: minor (deferred): a non-dry apply matching nothing records no hub_links entry
- Task 8: minor (deferred): CHANGELOG "Derived from" lines run into the "Published by" paragraph (no blank line); partial links not flagged in CHANGELOG
- Task 8: minor (deferred): hub_provenance tests miss "source" relation / non-dict source_config / non-list hub_links; unused settings import (plan-mandated)
- Task 9: minor (deferred): load_patch counts any OSError (incl. transient connection errors) as "undecodable" missing; bounded by the 5% guard
- Task 9: minor (deferred): fetch-worker clamp duplicated in library/storage/gcs.py; CSV escaping ignores leading tab/CR
- Task 10: minor (deferred): openHub closes project-dialog even in the model flow (harmless)
- Task 10: minor (deferred): "validation did not start" message flashes before the dialog closes
- Task 10: minor (deferred): editing ref/location mid-registration lets the pending action continue with the old entry (text inputs not disabled)
- Task 11: minor (deferred): docs say private repos get 422; unknown/private actually 404 (gated → 422)
- Final: minor (parked): two simultaneous resumes can both queue (second only skips rows)
- Final: minor (parked): Image/Label column selects have no blank option when nothing was detected
- Final: minor (parked): CURATION.md says a column change "registers again" — it resets; user clicks Register
- Final: observation (parked): publish/image-export threads started from T3 review aren't gated by the coordinator flag; cancel of a running import doesn't cross processes; T3 review can't see the main app's eval artifacts under make up

Deployment follow-up:
- With the evaluation overlay, `/data/library/curation_eval` is created root-owned by Docker for the nested spool bind mounts, so the app (uid 1000) can't write `hub/`, `patch_cache/` or `models/`. Fixed by hand on this machine (chown). A fresh install needs the Dockerfile to pre-create that folder owned by the app user.

## Found in local testing (2026-10-11)

- Header tabs: the Curation and Library links in the page header swap places depending on which page you are on. They should stay in one fixed order.
- Model registration from Hugging Face downloads the weights inside the request, so the dialog waits for the whole download. Make it a 202 background operation with progress, as dataset project creation and linking already are.
- `yolo11n.pt` is downloaded again every time the app container is recreated. `modelcache.resolve` returns bare Ultralytics names unchanged, so Ultralytics saves them in the container's working directory. Map bare names into the model cache directory, or point `LIB_YOLO_MODEL` at a file on a mounted volume. Since the 2026-10-11 fix this happens in the background and no longer freezes the app, but it is still a wasted download on every recreate.
- Patch-only Hugging Face projects show no source image or point, although the matching items in the regional CSV project have both (378,680 of 378,680 rows match). Possible addition: a background "copy source context from a matched project" operation that reuses `service.match_items` to copy `source_image_uri`, `row_px` and `column_px`.
