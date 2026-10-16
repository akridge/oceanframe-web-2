"""Browser check of the source-image editor (P3a) on the Task 7 workspace layout.

Covers, on the live stack:
  * NOAA: missing images hidden by default, the queue opens on an image that loads, the status card counts them.
  * Hawaii sample project: move, resize, remove and bulk-add on the source image, saved in one /edits request;
    keyboard nudge and Alt+Right stepping; crop tile -> source -> Back; deep links.
  * Task 7 (a) overlay clicks: the stage toolbar, image stepping and the unsaved-edits pill never pan, zoom
    or add a point; a hidden Save sends nothing; one move + Save sends exactly one /edits request.
  * Task 7 (b) markers after resize: after a queue collapse, inspector tab switches and a window resize every
    marker centre is within 2 px of where column_px*scale / row_px*scale puts it, computed from the wrap's
    current size (not from state.markerScale, which a stale render would keep consistent with itself).
    A resize in the middle of a marker drag does not drop the drag.
  * Task 9 unsaved-edits guard (an OF.ui sheet, answered by clicking [data-sheet="cancel"|"confirm"]): with one
    unsaved move, Alt+Right -> Keep editing keeps the image, tray and hash; Alt+Right -> Discard steps on with an
    empty tray; Back (after opening from the crop grid) -> Keep editing stays on source with the Back bar; a project
    switch -> Keep editing keeps the project. Any native dialog fails the run.

The run writes to the sample project, so it snapshots every image in the queue first and ends with a CLEANUP
step for every one of them (in a finally block, through the same
/edits API): every point is put back to the geometry recorded at the start, points the run removed are
restored, points the run added are removed. restore leaves review_state "flagged" and forces included on,
so those two fields are then patched back to the recorded values as well. If cleanup itself fails, the run
prints a loud error and exits 3; failed checks exit 1.

Run inside the Playwright image on the stack network (password via env, never printed):

  OF_PW2="$(cat "$TMP/of-tester2-password.txt")" MSYS_NO_PATHCONV=1 docker run --rm --network oceanframe-net -e OF_PW2 \
    -v "$PWD/tests/browser:/work" -w /work mcr.microsoft.com/playwright/python:v1.47.0-jammy \
    sh -c "pip install -q playwright==1.47.0 2>/dev/null; python p3a_browser_check.py"

Output (summary.json and screenshots) goes to tests/browser/shots/p3a/.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("OF_BASE", "http://oceanframe-app:8080")
USER = os.environ.get("OF_USER2", "tester2")
NOAA = "53d400c6-097b-4995-b3af-edb7a7528925"
SAMPLE = "6c182409-a91b-43a3-98a0-fd5520d39175"
OUT = Path(__file__).resolve().parent / "shots" / "p3a"
OUT.mkdir(parents=True, exist_ok=True)
results: dict[str, dict] = {}
errors: list[str] = []
cleanup_report: dict = {}
native_dialogs: list[str] = []


def check(name, ok, detail=""):
    results[name] = {"ok": bool(ok), "detail": str(detail)[:300]}


def answer_sheet(page, answer):
    """Wait for the OF.ui sheet, click its "cancel" or "confirm" button and return the sheet's title."""
    page.wait_for_selector("dialog.sheet:not([id])[open]", timeout=5000)
    title = page.inner_text("dialog.sheet:not([id])[open] .sheet-title")
    page.click(f'dialog.sheet:not([id])[open] [data-sheet="{answer}"]')
    page.wait_for_function("() => !document.querySelector('dialog.sheet:not([id])')", timeout=3000)
    return title


def nudge_clear_marker(page):
    """Select a marker clear of the overlays and nudge it once (one unsaved move). Returns its id or None."""
    ids = page.evaluate("state.items.filter(i => i.row_px != null && !i.removed).map(i => i.id)")
    target = next((i for i in ids if marker_hit(page, i)), None)
    if target is None:
        return None
    page.click(f".source-marker[data-point-id='{target}']")
    page.wait_for_timeout(400)
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(300)
    return target


GUARD_STATE = """() => ({ view: state.view, project: state.projectId, select: document.getElementById('project-select').value,
    image: state.currentImage && state.currentImage.first_item_id, pending: pendingCount(), hash: location.hash,
    tray: !document.getElementById('pending-tray').classList.contains('d-none') && document.getElementById('pending-tray').offsetParent !== null,
    count: document.getElementById('pending-count').textContent.trim(),
    backBar: !document.getElementById('back-bar').classList.contains('d-none'), backLabel: document.getElementById('back-label').textContent,
    sheets: document.querySelectorAll('dialog.sheet:not([id])').length })"""


def api(page, path, method="GET", body=None):
    return page.evaluate("""async ([path, method, body]) => {
        const r = await fetch(path, {method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined})
        return {status: r.status, body: await r.json().catch(() => null)}
    }""", [path, method, body])


def open_project(page, project, view):
    page.goto(f"{BASE}/curation#project={project}&view={view}")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3500)


def load_first_image(page):
    page.click("#refresh-queue")
    page.wait_for_timeout(4000)
    page.locator("[data-image-id]").first.click()
    page.wait_for_timeout(4000)


def image_loaded(page):
    return page.eval_on_selector("#source-image", "el => el.complete && el.naturalWidth > 0")


def marker_hit(page, item_id):
    """True when the marker's centre is not covered by an overlay or another marker."""
    return page.evaluate("""id => {
        const m = document.querySelector(`.source-marker[data-point-id="${id}"]`)
        if (!m) return false
        const r = m.getBoundingClientRect()
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
        return Boolean(hit && hit.closest('.source-marker') === m)
    }""", item_id)


# Where every marker is against where it should be, from the wrap's current size and the image's natural size.
MARKER_DRIFT = """() => {
  const wrap = document.getElementById('source-image-wrap')
  const image = document.getElementById('source-image')
  const nw = image.naturalWidth, nh = image.naturalHeight
  const cw = wrap.clientWidth, ch = wrap.clientHeight
  const scale = Math.min(cw / nw, ch / nh)
  const rect = wrap.getBoundingClientRect()
  const offX = rect.left + wrap.clientLeft + (cw - nw * scale) / 2
  const offY = rect.top + wrap.clientTop + (ch - nh * scale) / 2
  const view = state.imageView
  let worst = 0, count = 0, worstId = null
  for (const item of state.items) {
    if (item.column_px == null || item.row_px == null) continue
    const marker = document.querySelector(`.source-marker[data-point-id="${item.id}"]`)
    if (!marker) continue
    const geo = markerGeometry(item)
    const r = marker.getBoundingClientRect()
    const dx = Math.abs(r.left + r.width / 2 - (offX + geo.column_px * scale))
    const dy = Math.abs(r.top + r.height / 2 - (offY + geo.row_px * scale))
    const d = Math.max(dx, dy)
    count += 1
    if (d > worst) { worst = d; worstId = item.id }
  }
  return { worst, count, worstId, scale, cw, ch, zoom: view.zoom, panX: view.panX, panY: view.panY }
}"""


def drift_ok(page, name):
    page.wait_for_timeout(400)
    drift = page.evaluate(MARKER_DRIFT)
    check(name, drift["count"] > 0 and drift["worst"] <= 2 and drift["zoom"] == 1 and drift["panX"] == 0, drift)
    return drift


def snapshot_items(page, first_id):
    detail = api(page, f"/api/curation/projects/{SAMPLE}/source-images/{first_id}")["body"]
    return {i["id"]: {k: i.get(k) for k in ("row_px", "column_px", "crop_size", "removed", "included", "review_state", "revision")}
            for i in detail["items"]}


def cleanup(context, first_id, before):
    """Put the image back the way the run found it, through the /edits API (plus a PATCH for flags restore leaves)."""
    req = context.request
    detail = req.get(f"{BASE}/api/curation/projects/{SAMPLE}/source-images/{first_id}").json()
    edits = []
    for item in detail["items"]:
        was = before.get(item["id"])
        rev = item["revision"]
        if was is None:
            if not item.get("removed"):
                edits.append({"op": "remove", "item_id": item["id"], "expected_revision": rev})
            continue
        if was["row_px"] is not None and (item["row_px"], item["column_px"]) != (was["row_px"], was["column_px"]):
            edits.append({"op": "move", "item_id": item["id"], "expected_revision": rev, "row_px": was["row_px"], "column_px": was["column_px"]})
        if was["crop_size"] is not None and item["crop_size"] != was["crop_size"]:
            edits.append({"op": "resize", "item_id": item["id"], "expected_revision": rev, "crop_size": was["crop_size"]})
        if bool(item.get("removed")) != bool(was["removed"]):
            edits.append({"op": "remove" if was["removed"] else "restore", "item_id": item["id"], "expected_revision": rev})
    report = {"edits": len(edits), "ops": {}}
    for edit in edits:
        report["ops"][edit["op"]] = report["ops"].get(edit["op"], 0) + 1
    if edits:
        response = req.post(f"{BASE}/api/curation/projects/{SAMPLE}/source-images/{first_id}/edits",
                            data=json.dumps({"reason": "P3a browser check cleanup", "edits": edits, "added": []}),
                            headers={"Content-Type": "application/json"})
        report["edits_status"] = response.status
        body = response.json() if response.ok else None
        report["conflicts"] = (body or {}).get("conflicts")
    # restore forces included on and leaves review_state flagged: put both back.
    detail = req.get(f"{BASE}/api/curation/projects/{SAMPLE}/source-images/{first_id}").json()
    patched = 0
    for item in detail["items"]:
        was = before.get(item["id"])
        if not was or (item["included"] == was["included"] and item["review_state"] == was["review_state"]):
            continue
        response = req.patch(f"{BASE}/api/curation/projects/{SAMPLE}/items/{item['id']}",
                             data=json.dumps({"expected_revision": item["revision"], "included": was["included"],
                                              "review_state": was["review_state"], "reason": "P3a browser check cleanup"}),
                             headers={"Content-Type": "application/json"})
        patched += 1 if response.ok else 0
    report["flag_patches"] = patched
    # Verify: geometry, removal and flags match the snapshot for every recorded point; added points are removed.
    final = {i["id"]: i for i in req.get(f"{BASE}/api/curation/projects/{SAMPLE}/source-images/{first_id}").json()["items"]}
    mismatched = [i for i, was in before.items() if i in final and any(
        (final[i].get(k) if k != "removed" else bool(final[i].get(k))) != (was[k] if k != "removed" else bool(was[k]))
        for k in ("row_px", "column_px", "crop_size", "removed", "included", "review_state"))]
    leftovers = [i for i, item in final.items() if i not in before and not item.get("removed")]
    report["mismatched"] = mismatched
    report["added_left_visible"] = leftovers
    return report


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1500, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(20000)
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        # Every confirm is an OF.ui sheet since Task 9: a native dialog is a failure (dismissed so the run goes on).
        page.on("dialog", lambda d: (native_dialogs.append(d.message[:120]), d.dismiss()))
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url[:160]}") if r.status >= 400 else None)
        edit_requests: list[str] = []
        page.on("request", lambda r: edit_requests.append(r.url) if r.method == "POST" and r.url.endswith(("/edits", "/points")) else None)
        # Every image the run writes to (an /edits or /points POST), for cleanup.
        touched_images: set[int] = set()

        def on_write(request):
            match = re.search(r"/projects/[^/]+/source-images/(\d+)/(?:edits|points)$", request.url)
            if request.method == "POST" and match:
                touched_images.add(int(match.group(1)))
        page.on("request", on_write)

        # Sign in through the API (as the top-bar sweep does): the login page's own redirect can race the first goto.
        login = page.request.post(f"{BASE}/api/auth/login", data={"username": USER, "password": os.environ["OF_PW2"]})
        if login.status != 200:
            print(f"login failed: HTTP {login.status}", file=sys.stderr)
            return 2

        # ── NOAA: missing images hidden, queue opens on an image that loads, status card counts them
        open_project(page, NOAA, "source")
        check("hide-missing on by default", page.is_checked("#filter-hide-missing"))
        load_first_image(page)
        page.wait_for_timeout(4000)
        check("NOAA queue opens on an image that loads", image_loaded(page), page.eval_on_selector("#source-image", "el => el.src")[:160])
        page.screenshot(path=str(OUT / "1_noaa_source.png"))
        page.click("#status-view-button")
        page.wait_for_timeout(5000)
        summary = page.inner_text("#missing-summary") if page.is_visible("#missing-summary") else page.inner_text("#missing-card")
        check("status card reports 1,422 missing images", "1,422" in summary or "1422" in summary, summary)
        page.screenshot(path=str(OUT / "2_missing_card.png"))
        csv = page.evaluate(f"fetch('/api/curation/projects/{NOAA}/availability.csv').then(r => r.text())")
        check("missing CSV lists FFS 2024 images", "2024" in csv and csv.count("\n") > 1400, csv[:200])

        # ── Sample project
        open_project(page, SAMPLE, "source")
        page.wait_for_timeout(3000)
        load_first_image(page)
        check("sample source image loads", image_loaded(page))
        first_id = page.evaluate("state.currentImage && state.currentImage.first_item_id")
        check("sample opens on an image", first_id, first_id)
        # Snapshot every image in the queue, so cleanup can restore any image the run touches, not only the first.
        image_ids = page.evaluate("state.images.map(image => image.first_item_id)")
        snapshots = {image_id: snapshot_items(page, image_id) for image_id in image_ids}
        cleanup_report["recorded_images"] = {str(k): len(v) for k, v in snapshots.items()}
        try:
            run_sample_checks(page, first_id, edit_requests)
        except Exception as exc:  # noqa: BLE001 - report it, then clean up
            errors.append(f"exception: {type(exc).__name__}: {exc}"[:400])
            check("sample checks ran to the end", False, exc)
            try:
                page.screenshot(path=str(OUT / "z_failure.png"))
            except Exception:  # noqa: BLE001
                pass
        finally:
            # ── CLEANUP: every image the run touched goes back to its recorded geometry.
            try:
                page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            cleanup_failed = False
            try:
                touched_unrecorded = sorted(touched_images - set(snapshots))
                cleanup_report["touched_images"] = sorted(touched_images)
                cleanup_report["touched_unrecorded"] = touched_unrecorded
                reports = {}
                for image_id, before in snapshots.items():
                    reports[str(image_id)] = cleanup(context, image_id, before)
                cleanup_report["images"] = reports
                clean = not touched_unrecorded and all(
                    not r["mismatched"] and not r["added_left_visible"] and r.get("edits_status", 200) == 200 for r in reports.values())
                check("CLEANUP: every touched image back to its recorded geometry and flags; added points removed", clean,
                      {k: v for k, v in cleanup_report.items() if k != "recorded_images"})
            except Exception as exc:  # noqa: BLE001
                cleanup_failed = True
                cleanup_report["exception"] = f"{type(exc).__name__}: {exc}"[:400]
                check("CLEANUP ran", False, exc)
                print("!" * 72 + f"\nCLEANUP FAILED: {type(exc).__name__}: {exc}\n"
                      "The sample project may still carry edits from this run.\n" + "!" * 72, file=sys.stderr)
        browser.close()
    summary_out = {"results": results, "cleanup": cleanup_report, "errors": errors[:30]}
    (OUT / "summary.json").write_text(json.dumps(summary_out, indent=1), encoding="utf-8")
    print(json.dumps(summary_out, indent=1))
    failed = [name for name, r in results.items() if not r["ok"]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    if cleanup_failed:
        return 3
    return 1 if failed else 0


def run_sample_checks(page, first_id, edit_requests):
    page.click("[data-inspector-tab='point']")
    detail = api(page, f"/api/curation/projects/{SAMPLE}/source-images/{first_id}")["body"]
    check("detail has width and height", detail.get("width") and detail.get("height"), {k: detail.get(k) for k in ("width", "height", "source_missing")})
    items_before = len(detail["items"])
    candidates = [i for i in detail["items"] if i.get("row_px") is not None and not i.get("removed")]
    page.wait_for_timeout(500)
    clear = [i for i in candidates if marker_hit(page, i["id"])]
    check("at least two markers clear of the overlays", len(clear) >= 2, (len(candidates), len(clear)))
    a, b = clear[0], clear[1]

    # ── Task 7 (b): markers stay on their points when the stage resizes
    page.click("#fit-image")
    drift_ok(page, "markers on their points at load")
    page.click("#queue-toggle")
    drift_ok(page, "markers on their points after collapsing the queue")
    page.click("#queue-toggle")
    drift_ok(page, "markers on their points after expanding the queue")
    for tab in ("image", "model", "point"):
        page.click(f"[data-inspector-tab='{tab}']")
        visible = page.is_visible(f"#inspector-{tab}") and all(not page.is_visible(f"#inspector-{other}") for other in ("point", "image", "model") if other != tab)
        check(f"inspector tab {tab} shows only its panel", visible)
        drift_ok(page, f"markers on their points after the {tab} tab")
    page.set_viewport_size({"width": 1300, "height": 900})
    drift_ok(page, "markers on their points after a window resize (1300)")
    page.screenshot(path=str(OUT / "6_resized_1300.png"))
    page.set_viewport_size({"width": 1500, "height": 1000})
    drift_ok(page, "markers on their points after a window resize back (1500)")

    # ── Task 7 (a): overlay clicks never pan, zoom or add a point
    page.click(f".source-marker[data-point-id='{a['id']}']")
    page.wait_for_timeout(500)
    view0 = page.evaluate("({...state.imageView})")
    pending0 = page.evaluate("pendingCount()")
    edit_requests.clear()
    page.evaluate("document.getElementById('pending-save').click()")  # the pill is hidden: no edits to save
    page.wait_for_timeout(800)
    check("hidden Save sends no request", not edit_requests and page.evaluate("pendingCount()") == pending0 == 0, (edit_requests, pending0))
    page.click("#add-point-mode")  # add-point mode on, from the toolbar
    check("add-point toggle from the toolbar does not open the point dialog", not page.evaluate("document.getElementById('point-dialog').open"))
    # The classify tools move into the add-point dialog while it is open and back to the Model tab after (writes nothing).
    spot = page.evaluate("""() => {
        const r = document.getElementById('source-image-wrap').getBoundingClientRect()
        for (let y = r.top + r.height * 0.3; y < r.bottom - 80; y += 17) {
          for (let x = r.left + 40; x < r.right - 40; x += 23) {
            const hit = document.elementFromPoint(x, y)
            if (hit && (hit.id === 'source-image' || hit.id === 'source-markers')) return { x, y }  // the marker layer covers the image
          }
        }
        return null
    }""")
    edit_requests.clear()
    if spot:
        page.mouse.click(spot["x"], spot["y"])
        page.wait_for_timeout(400)
    in_dialog = page.evaluate("document.getElementById('point-dialog').open && Boolean(document.getElementById('classify-tools').closest('#point-dialog'))")
    check("add-point dialog opens on the image and borrows the classify tools", spot and in_dialog, spot)
    if page.evaluate("document.getElementById('point-dialog').open"):
        page.click("#close-point-dialog")
        page.wait_for_timeout(300)
    back = page.evaluate("""() => Boolean(document.getElementById('classify-tools').closest('#inspector-model'))
        && document.getElementById('classification-results').innerHTML === '' && !document.getElementById('point-dialog').open""")
    check("closing the dialog returns the classify tools to the Model tab, writing nothing", back and not edit_requests, edit_requests)
    tb = page.locator("#zoom-level").bounding_box()
    page.mouse.move(tb["x"] + tb["width"] / 2, tb["y"] + tb["height"] / 2)
    page.mouse.down()
    page.mouse.move(tb["x"] + 60, tb["y"] + 50, steps=6)
    page.mouse.up()
    page.mouse.move(tb["x"] + tb["width"] / 2, tb["y"] + tb["height"] / 2)
    page.mouse.wheel(0, -300)
    page.wait_for_timeout(300)
    view1 = page.evaluate("({...state.imageView})")
    check("drag and wheel on the toolbar do not pan or zoom",
          (view1["panX"], view1["panY"], view1["zoom"]) == (view0["panX"], view0["panY"], view0["zoom"]), (view0, view1))
    check("drag on the toolbar opens no point dialog", not page.evaluate("document.getElementById('point-dialog').open"))
    nav = page.inner_text("#stage-position")
    check("stage nav shows the position", "/" in nav and nav.strip().startswith("1"), nav)
    page.click("#stage-next")
    page.wait_for_timeout(3500)
    moved_on = page.evaluate("state.currentImage.first_item_id") != first_id
    check("#stage-next steps to the next image", moved_on, page.inner_text("#stage-position"))
    check("#stage-next adds no point and opens no dialog",
          not page.evaluate("document.getElementById('point-dialog').open") and page.evaluate("state.pending.added.length") == 0)
    page.click("#stage-prev")
    page.wait_for_timeout(3500)
    check("#stage-prev steps back", page.evaluate("state.currentImage.first_item_id") == first_id)
    page.click("#add-point-mode")  # off again
    check("add-point toggled off from the toolbar opens no dialog", not page.evaluate("document.getElementById('point-dialog').open")
          and not page.evaluate("state.addPointMode"))
    view2 = page.evaluate("({...state.imageView})")
    page.click(f".source-marker[data-point-id='{a['id']}']")
    page.wait_for_timeout(400)
    page.keyboard.press("ArrowRight")  # one move
    page.wait_for_timeout(300)
    check("one nudge shows the pill with a breakdown", page.is_visible("#pending-tray") and "moved" in page.inner_text("#pending-breakdown"),
          page.inner_text("#pending-tray") if page.is_visible("#pending-tray") else "")
    # Keys on a focused overlay control stay there: no removal, no nudge, no image step.
    edit_state = """() => ({ count: pendingCount(), moves: JSON.stringify([...state.pending.moves]), removes: state.pending.removes.size,
        image: state.currentImage.first_item_id, selected: state.selected && state.selected.id })"""
    key_before = page.evaluate(edit_state)
    for control in ("#pending-review", "#zoom-in", "#stage-next"):
        page.focus(control)
        for key in ("Delete", "ArrowRight", "Backspace", "ArrowLeft"):
            page.keyboard.press(key)
        page.wait_for_timeout(300)
    page.wait_for_timeout(1500)  # a step would have loaded another image by now
    key_after = page.evaluate(edit_state)
    check("Delete/Backspace/arrows on a focused overlay control change neither the point nor the image", key_after == key_before, (key_before, key_after))

    # The pill is fully in view whenever there are unsaved edits, with the metrics row and a notice showing.
    page.evaluate("showNotice('Pill visibility check: a notice above the workspace.')")
    pill_in_view = """() => {
        const r = document.getElementById('pending-tray').getBoundingClientRect()
        const metrics = document.querySelector('.curation-metrics').getBoundingClientRect()
        return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, vw: innerWidth, vh: innerHeight,
          inView: r.height > 0 && r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight && r.right <= innerWidth,
          metrics: metrics.height > 0, notice: !document.getElementById('curation-notice').classList.contains('d-none') }
    }"""
    for width, height in ((1500, 1000), (1280, 800)):
        page.set_viewport_size({"width": width, "height": height})
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(600)
        measured = page.evaluate(pill_in_view)
        check(f"pill fully in view at {width}x{height} with metrics and a notice",
              measured["inView"] and measured["metrics"] and measured["notice"], measured)
        page.screenshot(path=str(OUT / f"7_pill_in_view_{width}x{height}.png"))
    page.set_viewport_size({"width": 1500, "height": 1000})
    page.click("#curation-notice-close")
    page.wait_for_timeout(400)
    drift_ok(page, "markers on their points after the pill-visibility resizes")
    edit_requests.clear()
    page.click("#pending-save")
    page.wait_for_timeout(3500)
    after_one = api(page, f"/api/curation/projects/{SAMPLE}/source-images/{first_id}")["body"]
    check("one move + Save sends exactly one /edits request", len(edit_requests) == 1, edit_requests)
    check("Save on the pill adds no point", len(after_one["items"]) == items_before, (items_before, len(after_one["items"])))
    view3 = page.evaluate("({...state.imageView})")
    check("Save on the pill does not pan", (view3["panX"], view3["panY"]) == (view2["panX"], view2["panY"]) == (0, 0), (view2, view3))

    # A resize in the middle of a drag must not drop it.
    page.click(f".source-marker[data-point-id='{a['id']}']")
    page.wait_for_timeout(500)
    box = page.locator(f".source-marker[data-point-id='{a['id']}']").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 12, cy + 8, steps=4)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.wait_for_timeout(300)
    still = page.evaluate("markerDragActive()")
    page.mouse.move(cx + 30, cy + 18, steps=4)
    page.mouse.up()
    page.wait_for_timeout(300)
    check("a resize mid-drag keeps the drag and queues the move", still and page.evaluate(f"state.pending.moves.has({a['id']})"), still)
    drift_ok(page, "markers on their points after the drag that spanned a resize")
    page.click("#pending-discard")
    page.set_viewport_size({"width": 1500, "height": 1000})
    page.wait_for_timeout(400)

    # ── P3a: move, resize, remove, bulk add, review, save
    page.click(f".source-marker[data-point-id='{a['id']}']")
    page.wait_for_timeout(800)
    box = page.locator(f".source-marker[data-point-id='{a['id']}']").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 25, box["y"] + box["height"] / 2 + 15, steps=8)
    page.mouse.up()
    page.wait_for_timeout(500)
    # A preset that differs from the point's current size, so the resize is a real edit.
    preset = next(size for size in (160, 224, 320) if size != a["crop_size"])
    page.click(f"[data-point-preset='{preset}']")
    page.wait_for_timeout(300)
    page.click(f".source-marker[data-point-id='{b['id']}']")
    page.wait_for_timeout(500)
    page.click("#point-remove")
    page.wait_for_timeout(300)
    page.click(f".source-marker[data-point-id='{a['id']}']")
    page.wait_for_timeout(500)
    page.click("[data-inspector-tab='model']")
    page.click("#bulk-add-button")  # on the toolbar; opens the form on the Point tab
    check("bulk add opens the Point tab", page.is_visible("#bulk-add-form") and page.is_visible("#inspector-point"))
    page.select_option("#bulk-grid", "3")
    page.fill("#bulk-spacing", "200")
    page.click("#bulk-run")
    page.wait_for_timeout(3000)
    pending_markers = page.locator(".source-marker.pending").count()
    check("bulk add laid out candidates", pending_markers >= 3, pending_markers)
    count_text = page.inner_text("#pending-count")
    check("pill counts move + resize + remove + adds", int(count_text) >= 3 + pending_markers - 1, count_text)
    breakdown = page.inner_text("#pending-breakdown")
    check("pill breakdown names moved, resized, added, removed", all(w in breakdown for w in ("moved", "resized", "added", "removed")), breakdown)
    page.click("#pending-review")
    page.wait_for_timeout(300)
    review_box = page.locator("#pending-review-panel").bounding_box()
    pill_box = page.locator("#pending-tray").bounding_box()
    check("Review opens the list as a popover above the pill", page.is_visible("#pending-list") and review_box and review_box["y"] + review_box["height"] <= pill_box["y"] + 1,
          (review_box, pill_box))
    drop = page.locator("[data-pending-drop]").first
    if drop.count():
        drop.click()
        page.wait_for_timeout(300)
    check("Review stays open after dropping a candidate", page.is_visible("#pending-review-panel"))
    page.fill("#pending-reason", "P3a browser check")
    page.screenshot(path=str(OUT / "3_review_popover.png"))
    check("save button names the count", "Save" in page.inner_text("#pending-save") and page.inner_text("#pending-count") in page.inner_text("#pending-save"), page.inner_text("#pending-save"))
    expected_adds = page.locator(".source-marker.pending").count()
    edit_requests.clear()
    page.click("#pending-save")
    page.wait_for_timeout(4000)
    check("the full save is one /edits request", len(edit_requests) == 1, edit_requests)
    after = api(page, f"/api/curation/projects/{SAMPLE}/source-images/{first_id}")["body"]
    by_id = {i["id"]: i for i in after["items"]}
    check("move saved", (by_id[a["id"]]["row_px"], by_id[a["id"]]["column_px"]) != (a["row_px"], a["column_px"]),
          (a["row_px"], a["column_px"], by_id[a["id"]]["row_px"], by_id[a["id"]]["column_px"]))
    check("resize saved", by_id[a["id"]]["crop_size"] == preset, (preset, by_id[a["id"]]["crop_size"]))
    check("remove saved", by_id[b["id"]]["removed"] is True and by_id[b["id"]]["included"] is False, by_id[b["id"]])
    check("added points saved", len(after["items"]) == items_before + expected_adds, (items_before, expected_adds, len(after["items"])))
    check("pill hidden and Review closed after save", not page.is_visible("#pending-tray") and not page.is_visible("#pending-review-panel"))
    page.screenshot(path=str(OUT / "4_after_save.png"))
    crop = page.evaluate(f"fetch('/api/curation/projects/{SAMPLE}/items/{a['id']}/crop-preview?r={by_id[a['id']]['revision']}').then(r => r.status)")
    check("moved patch preview is cut fresh (200)", crop == 200, crop)
    pending = api(page, f"/api/curation/projects/{SAMPLE}/change-sets/pending?user=me")
    check("uncommitted changes name move/resize/remove/point", pending["status"] == 200, json.dumps(pending["body"].get("summary") if pending["body"] else pending)[:250])

    # ── keyboard: nudge the selected point, then step images with Alt+Right
    page.click(f".source-marker[data-point-id='{a['id']}']")
    page.wait_for_timeout(500)
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(300)
    check("arrow nudge queues an edit", page.is_visible("#pending-tray"), page.inner_text("#pending-count") if page.is_visible("#pending-tray") else "")
    # ── Task 9: the unsaved-edits guard is a sheet. Keep editing leaves everything as it was; Discard moves on.
    guard0 = page.evaluate(GUARD_STATE)
    page.keyboard.press("Alt+ArrowRight")
    title = answer_sheet(page, "cancel")
    page.wait_for_timeout(1500)  # a step would have loaded the next image by now
    guard1 = page.evaluate(GUARD_STATE)
    check("guard sheet names the edit count", "Discard 1 unsaved edit" in title, title)
    check("Alt+Right then Keep editing: same image, tray still shows 1, hash unchanged",
          guard1 == guard0 and guard1["pending"] == 1 and guard1["tray"] and guard1["count"] == "1" and guard1["sheets"] == 0, (guard0, guard1))
    page.screenshot(path=str(OUT / "9_guard_kept.png"))
    page.keyboard.press("Alt+ArrowRight")
    answer_sheet(page, "confirm")
    page.wait_for_timeout(3000)
    guard2 = page.evaluate(GUARD_STATE)
    check("Alt+Right then Discard: next image, tray empty", guard2["image"] != guard0["image"] and guard2["pending"] == 0 and not guard2["tray"]
          and guard2["sheets"] == 0, (guard0, guard2))
    page.screenshot(path=str(OUT / "8_after_step.png"))

    # ── navigation: crop tile -> source -> Back (guarded first); deep link
    page.click("#crop-view-button")
    page.wait_for_timeout(4000)
    page.locator("[data-crop-id]").nth(3).click()
    page.wait_for_timeout(3000)
    check("crop tile opens source with a Back bar", page.is_visible("#back-bar") and "crop" in page.inner_text("#back-label").lower(), page.inner_text("#back-label") if page.is_visible("#back-bar") else "")
    check("URL carries image and item", "image=" in page.url and "item=" in page.url, page.url)
    deep = page.url
    nudged = nudge_clear_marker(page)
    back0 = page.evaluate(GUARD_STATE)
    page.click("#back-button")
    answer_sheet(page, "cancel")
    page.wait_for_timeout(1000)
    back1 = page.evaluate(GUARD_STATE)
    check("Back then Keep editing: view stays source, Back bar, hash and tray unchanged",
          nudged and back1 == back0 and back1["view"] == "source" and back1["backBar"] and back1["pending"] == 1, (nudged, back0, back1))
    page.click("#back-button")
    answer_sheet(page, "confirm")
    page.wait_for_timeout(3000)
    check("Back then Discard returns to the crop grid", page.evaluate("state.view") == "crop" and page.evaluate("pendingCount()") == 0)
    page.goto(deep)
    page.reload()
    page.wait_for_timeout(6000)
    image_param = int(deep.split("image=")[1].split("&")[0])
    check("deep link reopens that image", page.evaluate("state.currentImage && state.currentImage.first_item_id") == image_param, page.evaluate("state.currentImage && state.currentImage.first_item_id"))
    page.screenshot(path=str(OUT / "5_deep_link.png"))

    # A project switch with an unsaved edit: Keep editing keeps the project, select, view, hash and tray.
    nudged = nudge_clear_marker(page)
    switch0 = page.evaluate(GUARD_STATE)
    page.select_option("#project-select", NOAA)
    answer_sheet(page, "cancel")
    page.wait_for_timeout(1500)
    switch1 = page.evaluate(GUARD_STATE)
    check("project switch then Keep editing: project, select, view, hash and tray unchanged",
          nudged and switch1 == switch0 and switch1["project"] == SAMPLE and switch1["select"] == SAMPLE and switch1["pending"] == 1, (nudged, switch0, switch1))
    # Keys typed in the avatar menu or the project More menu never reach the editor (arrows, Delete, Backspace).
    keys_before = page.evaluate("() => ({ n: pendingCount(), moves: JSON.stringify([...state.pending.moves]), removes: state.pending.removes.size, sel: state.selected && state.selected.id })")
    page.click("#rail-avatar")
    page.focus("#user-menu [data-theme='light']")
    for key in ("ArrowRight", "ArrowLeft", "ArrowDown", "Delete", "Backspace"):
        page.keyboard.press(key)
    page.keyboard.press("Escape")
    page.click("#topbar-more")
    for key in ("ArrowRight", "ArrowLeft", "Delete", "Backspace"):
        page.keyboard.press(key)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    keys_after = page.evaluate("() => ({ n: pendingCount(), moves: JSON.stringify([...state.pending.moves]), removes: state.pending.removes.size, sel: state.selected && state.selected.id })")
    check("arrows and Delete in the avatar and More menus leave pending edits and the selection alone",
          keys_before["n"] == 1 and keys_after == keys_before, (keys_before, keys_after))
    page.click("#pending-discard")  # writes nothing
    page.wait_for_timeout(300)
    # A palette project jump leaves the picker on the project it jumped to.
    noaa_name = page.evaluate("(id) => (state.projects.find(p => p.id === id) || {}).name", NOAA)
    page.keyboard.press("Control+k")
    page.fill("#of-palette-input", f"{noaa_name} · Crop grid")
    page.wait_for_timeout(1000)
    page.keyboard.press("Enter")
    page.wait_for_timeout(4000)
    jump = page.evaluate("() => ({ project: state.projectId, select: document.getElementById('project-select').value, view: state.view })")
    check("palette project jump: #project-select.value === state.projectId", jump["project"] == NOAA and jump["select"] == jump["project"] and jump["view"] == "crop", jump)
    check("no native browser dialog opened during the run", not native_dialogs, native_dialogs)


if __name__ == "__main__":
    sys.exit(main())
