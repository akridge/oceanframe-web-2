# Curation P3a: follow-ups

These are the items deferred while building P3a (patch editing, navigation, missing images). Reviewers rated each one minor, or one was parked by a ruling during subagent-driven execution. None blocks merging. They are grouped by area, with the most useful first.

## Likely worth doing next

- **Removing points on a missing image.** Batch edits read the image size before applying anything, so on an image that isn't in the bucket, remove and restore also fail with 422. Remove/restore need no bounds, so they could skip the size probe.
- **CSV manifests.** For edited patches they still name the old `crop_uri`. They should name the folder-export file, or mark the row as cut from source.
- **Single `/points` route.** It still doesn't check that the point lies inside the image; the batch route does. One existing test posts a point with no image behind it, so it needs an image fixture before the check can be added.

## Editor

- If the marker layer re-renders during a save, the selected marker isn't draggable again until the next render; add `renderMarkers()` in `savePending`'s `finally`.
- Agreeing to discard pending edits clears them even when the action you started is then cancelled. Examples: backing out of the project-delete name prompt, a failed project create, or validation returns in the hub create/link paths.
- Plain arrow keys can step to another image while a save is in flight, which brings up the discard prompt.
- The bulk-add form stays open after the image changes.
- An invalid typed label stays in its input until the next render.
- A batch that lists the same `item_id` twice gives the second entry a spurious conflict.
- When a removed point is skipped in a bulk edit, the conflict gives no "restore first" reason.
- Quality review bulk accept with `scope=source` refuses a removed anchor, though a whole-image accept never touches it.
- Thumbnails rely on the client's copy of the item's metadata. A crop-grid page loaded before an edit in the same session stays stale until the grid reloads.
- Ticking "Show removed" doesn't re-fetch matrix evidence already on screen.

## Missing-image check

- Files with extensions outside the image list (`.jpg .jpeg .png .tif .tiff`, any case) are flagged missing.
- When a local prefix is skipped with `PermissionError`, the flags it had before are cleared.
- Without a persisted "last checked" time, the status card can't tell "none missing" from "never checked".
- When an import finishes while edits are pending, the import timer skips its refresh and doesn't retry.

## Navigation

- Back restores scroll by reloading page 1, so deep scroll positions in long lists are clamped.
- A failed view load can show its error notice twice.
- Loading more queue pages can repeat an image already in the list. This predates P3a.

## Cache

- The eviction cap is checked every 20 writes, so the cache can overshoot the cap between checks.
- Keys depend only on the URI, not the content. That is fine while bucket objects are immutable.
- A read racing an unlink can raise `PermissionError` on Windows-native runs only; the app runs in a Linux container.

## Tests to add

- Batch edits:
  - a refused batch mixing valid and invalid edits;
  - one `operation_id` shared across a batch;
  - restore;
  - 403 for a non-curator;
  - removed points listed in the source-image detail;
  - classifier-unavailable fallback;
  - suggest on a missing image.
- `storage.list_names`, and a failed listing leaving flags untouched.
- Behavioural tests for the editor, bulk add and the filters UI (today's tests only check that strings are present).
- The duplicate-id test reads only `curation.html`, not the included partials.

## Environment

- `tests/test_pipeline.py::test_text_search_without_clip_falls_back_and_says_so` fails in the `test-oracle` container whatever the code does. That image has `open_clip` installed and `LIB_EMBED_BACKEND=auto`, so CLIP text search is available and the fallback the test expects never happens. Pin `LIB_EMBED_BACKEND` to a non-CLIP backend for that test, or skip it when CLIP is available.

## From the final fix wave

- **Matrix assets:** on benchmark and split pages, removed points are filtered in Python. A page can therefore come back short or empty while `next_cursor` is still set, and the client has to keep paging past it.
- **`/imports`:** it pages with offsets after filtering in Python. An import inserted mid-scan can shift a row.
- **Older evaluation runs:** runs staged before P3a keep their old staged patches for moved points. Re-run them to score fresh crops.
