# UX foundation: follow-ups

Items noted during the per-task and final reviews of the UX foundation branch that the final fix wave did not fix.
Everything else from the earlier list (project picker after a palette jump, popover key isolation, the shared discard
guard, palette gating on bare pages, Sign out and Account, password prompt type, crop tile images and the crop load
error state, view tab `aria-current`, hard-coded colours, form focus outline in forced-colors mode, the `.is-error`
ring, the palette Commit action and rejected `run()`) is done.

## Accepted spec deviation

- **Nested ES-module imports carry no `?v=`.** Entry scripts are versioned (`?v={{ asset_v }}`), but modules they import
  (`./ui.js`, `./modules/charts.js`, `./session.js`, the `static/library/*` and `static/auth/*` sub-modules) resolve
  without the query, and StaticFiles sends no `Cache-Control`, so a browser can pair a new entry script with a stale
  sub-module. Export surfaces are unchanged from `main`, so nothing breaks at link time. Revisit with an import map
  emitted by `base.html`, or by serving `/static` with `Cache-Control: no-cache` (ETag revalidation), before any
  sub-module changes an export.

## Shell and shared UI (`ui.js`, `menu.js`, `session.js`)

- `session.js` can load as two module instances (versioned and unversioned import paths).
- Esc pressed after an Enter that is still in flight still runs the second action.
- The `[contenteditable]` selector used by the key guards also matches `contenteditable="false"`.
- `asset_v` does not change when a file is reverted to an older mtime, so browsers can keep the newer cached copy.
- Some actions show both an inline status and a toast.
- The unsaved-edits guard's shared promise covers a second guard while the sheet is open; a rapid back/forward can still
  leave the lower history entry's view change confusing (no data is lost).
- The avatar menu's Theme control marks the current choice once at render; it does not follow a theme change made from
  the palette until reload (subscribe with `OF.ui.theme.onChange`).
- `#user-menu` is `role="menu"` but holds a segmented group, a header and a credit row that are not menu items.
- The palette input has no focus indicator besides the caret (`shell.css`).
- The project picker is a styled native `<select>`, not a button with a searchable popover list (spec section 6; no ruling recorded).
- Nested `<main>` landmarks: `library.html`, `collections.html` and `collection_form.html` each put a `<main>` inside `base.html`'s.
- `login.html`'s logo `<img>` has no `?v=`.

## Library

- `test_api` leniency about navigation: nav assertions now rest on the rendered pages (`test_ui_integrity`), so tighten or drop the lenient API test.
- A library toast (`.job-toast`) can overlap the library action bar at 640 px and below while a selection is active.
- **The library page is not browser-verified.** tester2's `/` redirects to `/collections/new`: when `/api/library/status`
  lists no sources, `static/library/app.js` (`refreshStatus`) sends the browser to the Add-collection form. That is the
  existing "no collections yet" onboarding, not a regression, but it means the grid tiles, detail overlay, filter rail,
  top-bar search row, the All/Datasets `aria-current` observer and the Library palette actions have only been checked by
  reading the code. Run `ui_tour.py` and `responsive_sweep.py` once against an instance with a collection (as admin) after merge.

## Frame Analyser

- Chart colours in `charts.js` carry a hex fallback instead of reading tokens only.

## Curation

- Crop-card `<img>` for a `source_missing` item hides itself when its preview 404s; the browser logs that 404 to the console.

## Browser checks

- `palette_providers_check.py`: replace the fixed wait, remove the debug print, and handle the (c) redirect for tester2.
- Screenshot scripts should wait for `img.complete` rather than a fixed delay (a 1.2 s wait once produced blank screenshots).

## From the final re-review

- **Unsaved-edits guard:** the shared in-flight promise (`discardInflight`) has no `.catch`/`finally`. A rejected confirm would leave it stuck. `OF.ui.confirm` never rejects today.
- **Tests:** the static tests in `tests/test_ui_dialogs.py` check that the right code is present, not how it behaves. The behaviour is covered by the browser checks in `tests/browser/`.
