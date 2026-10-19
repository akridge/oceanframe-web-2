# Browser checks

Playwright scripts that drive the deployed app. They run in the Playwright container on the stack network, against
`http://oceanframe-app:8080`, signed in as one of the starter accounts the deploy scripts create (`user1`, a curator,
or `test`, a lead; their passwords are in `.env` under `OCEANFRAME_STARTER_USERS`). None of them needs a host Python or Node.

## Running one

Deploy the app first (`bash deploy/stack_up.sh`, or `docker compose -f docker-compose.stack.yml up -d --build`),
then, from the repository root in Git Bash:

```bash
export OF_USER2=user1 OF_PW2="$(grep '^OCEANFRAME_STARTER_USERS=' .env | cut -d= -f2- | tr , '\n' | grep '^user1:' | cut -d: -f3)"
export OF_PW="$OF_PW2" OF_PROJECT=<NOAA Pacific T3 id> OF_HF_PROJECT=<NOAA Pacific T3 (Hugging Face) id>
MSYS_NO_PATHCONV=1 docker run --rm --network oceanframe-net -e OF_PW -e OF_PW2 -e OF_USER2 -e OF_PROJECT -e OF_HF_PROJECT \
  -v "$PWD/tests/browser:/work" -w /work mcr.microsoft.com/playwright/python:v1.47.0-jammy \
  sh -c "pip install -q playwright==1.47.0 2>/dev/null; python <script>.py"
```

- `OF_PW2` (`OF_PW` for `matrix_flow_check.py`) is the password, passed through the environment. Never print it or put it
  in a file in the repo. The older scripts default to `tester2` and project ids from an earlier database: set `OF_USER2`,
  `OF_PROJECT` (the NOAA project id) and `OF_HF_PROJECT` for a new install.
- `OF_BASE` overrides the app URL (default `http://oceanframe-app:8080`).
- `p2_browser_check.py` needs a user who is a curator on the Hugging Face project but not a member of NOAA, with
  NOAA made members-only (projects are open to every curator account otherwise), and
  `p3a_browser_check.py` needs the 'Hawaii sample' project; a new install has neither, so set them up first.
- Exit codes: 0 pass, 1 a failed check, 2 setup problem (no password, login failed), 3 cleanup failed (P3a).

## Output

Everything the scripts write (screenshots, JSON summaries) goes to `tests/browser/shots/`, which is git-ignored along with
anything else under `tests/browser/` that is not a `.py` or `.md` file.

## The scripts

| script | what it checks | writes data? |
|---|---|---|
| `matrix_flow_check.py` | the Patch T3 flow as a curator: sign-in lands on curation, no read-only notice, the matrix tab is active, the baseline matrix (cell click brings its crops into view) or the empty-state card, R focuses the label; with `OF_WRITE=1`, F flags one item, moves on, and the item is put back. Both themes; screenshots in `shots/matrix_flow/`. | only with `OF_WRITE=1`: the item is put back, but the flag and unflag stay in the account's change log as 2 uncommitted changes (commit or ignore them) |
| `ui_tour.py` | every page (`/`, `/collections`, `/collections/new`, `/frames`, `/account`, `/curation` source and crops) in dark and light at 1440x900, curation at 390x844; no page errors or console errors; theme persists across a reload; Ctrl+K opens the palette and "crops" + Enter jumps to the crop view. Screenshots `tour-*.png`. | no (resets the theme preference) |
| `responsive_sweep.py` | no horizontal scroll at 360 to 1024 px wide; the unsaved-edits pill, crop selection bar and Review popover stay inside the viewport and above the tab bar; touch devices show the crop tile's Open source image button | no |
| `curation_topbar_sweep.py` | every view tab sits inside the viewport (or is scrollable at 640 px and below) at widths from 360 to 1920 px, with the commit pill and badge showing | no (the pill and badge are faked in the browser only) |
| `p2_browser_check.py` | collaboration: the read-only notice for a non-member, change-set history, the Members and Releases sheets (opened from the Project menu), compare people | no |
| `claims_browser_check.py` | two curators at once on the Hugging Face project (`OF_USER`/`OF_PW`, default user1, and `OF_USER2`/`OF_PW2`, default test): an open image is claimed and left out of the other's queue, opening it anyway says who has it, Quality review claims its undecided patches, the Activity tab shows the other's view with the online badge, leaving the page lets the claims and presence go. Screenshots in `shots/claims/`. | no (claims and presence only, released at the end) |
| `p3a_browser_check.py` | the source-image editor: move, resize, remove, bulk add, save in one request, keyboard, overlays, the unsaved-edits guard sheets | yes, in the Hawaii sample project; see below |
| `ui_tour.py` note | tester2's `/` redirects to `/collections/new`: the library page sends the browser there when no collection is registered (the "no collections yet" onboarding in `static/library/app.js`). The library page (grid, filter rail, search row, its palette actions) is therefore not covered by the tour or the sweeps; check it by hand on an instance with at least one collection. | no |
| `curation_views_shots.py`, `palette_providers_check.py` | screenshots of the curation views; the palette's project, image and action providers | no |

## Cleanup

`p3a_browser_check.py` is the only script that writes. Before it starts it records every image's points; in a `finally` block
it puts moved and resized points back, restores points it removed and deletes points it added (through the same `/edits`
API), then verifies the result. If cleanup cannot be verified it prints an error and exits 3: fix the data by hand before
running anything else. All other scripts are read-only apart from the browser's own stored theme.

Expectations that depend on data (for example the release "Pacific T3 deployment check" in the P2 check) describe the
test database as it is; if the data changes, change the assertion rather than the app.
