"""Responsive sweep (Task 11): no horizontal page scroll, and the floating pills clear the tab bar.

Viewports include 641x900, 768x1024, 900x800 and 1024x768. Pages: /, /collections, /frames, /account, /curation (source view with an image selected, and the crop
view) at 360, 390, 768 and 1024 px. For each: document.documentElement.scrollWidth <= window.innerWidth.
On /curation the unsaved-edits pill (#pending-tray) and the crop selection bar are made visible in the
browser only (text set, d-none dropped; nothing is saved) and must sit fully inside the viewport and,
at <= 640 px, fully above the bottom tab bar. A touch context (hover: none) also checks that the crop
tile "Open source image" button is shown and focusable.

Read-only. Run (password via env, never printed):

  OF_PW2="$(cat "$TMP/of-tester2-password.txt")" MSYS_NO_PATHCONV=1 docker run --rm --network oceanframe-net -e OF_PW2 \
    -v "$PWD/tests/browser:/work" -w /work mcr.microsoft.com/playwright/python:v1.47.0-jammy \
    sh -c "pip install -q playwright==1.47.0 2>/dev/null; python responsive_sweep.py"

Output (JSON and screenshots) goes to tests/browser/shots/.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("OF_BASE", "http://oceanframe-app:8080")
USER = os.environ.get("OF_USER2", "tester2")
PROJECT = os.environ.get("OF_PROJECT", "53d400c6-097b-4995-b3af-edb7a7528925")
SHOTS = Path(__file__).resolve().parent / "shots"
VIEWPORTS = [(360, 800), (390, 800), (768, 800), (1024, 800), (641, 900), (768, 1024), (900, 800), (1024, 768)]
PAGES = [("home", "/"), ("collections", "/collections"), ("frames", "/frames"), ("account", "/account"),
         ("curation-source", f"/curation#project={PROJECT}&view=source"), ("curation-crop", f"/curation#project={PROJECT}&view=crop")]

OVERFLOW = "() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth })"
SELECT_IMAGE = """() => {
  const b = document.querySelector('#queue-list button[data-image-id]')
  if (b) b.click()
  return !!b
}"""
FORCE_FLOATERS = """() => {
  const tray = document.getElementById('pending-tray')
  if (tray) {
    document.getElementById('pending-count').textContent = '12'
    tray.classList.remove('d-none')
  }
  const bar = document.getElementById('crop-selection-bar')
  if (bar) bar.classList.remove('d-none')
}"""
RECTS = """() => {
  const r = id => { const e = document.getElementById(id); if (!e) return null; const b = e.getBoundingClientRect(); const vis = getComputedStyle(e).display !== 'none' && b.width > 0; return vis ? { l: b.left, r: b.right, t: b.top, b: b.bottom } : null }
  const rail = document.querySelector('.rail'); const rb = rail ? rail.getBoundingClientRect() : null
  const bar = rb && getComputedStyle(rail).position === 'fixed' && rb.width >= window.innerWidth - 1 ? rb.top : null
  return { vw: window.innerWidth, vh: window.innerHeight, barTop: bar, tray: r('pending-tray'), sel: r('crop-selection-bar') }
}"""


def check_floater(name, rect, m, problems):
    if rect is None:
        return None
    # Fully inside the viewport at every width; on a phone also fully above the tab bar.
    ok = rect["l"] >= -0.5 and rect["r"] <= m["vw"] + 0.5 and rect["t"] >= -0.5 and rect["b"] <= m["vh"] + 0.5
    if m["barTop"] is not None:
        ok = ok and rect["b"] <= m["barTop"] + 0.5
    if not ok:
        problems.append(f"{name} {rect} vs bar top {m['barTop']} viewport {m['vw']}x{m['vh']}")
    return ok


def login(page, password):
    for _ in range(2):  # one retry: a redeploy can restart the app mid-run
        try:
            res = page.request.post(f"{BASE}/api/auth/login", data={"username": USER, "password": password})
            if res.status == 200:
                return True
        except Exception:
            page.wait_for_timeout(5000)
    return False


def main() -> int:
    password = os.environ.get("OF_PW2")
    if not password:
        print("OF_PW2 is not set", file=sys.stderr)
        return 2
    SHOTS.mkdir(parents=True, exist_ok=True)
    rows, failures = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, height in VIEWPORTS:
            context = browser.new_context(viewport={"width": width, "height": height})
            page = context.new_page()
            if not login(page, password):
                print("login failed", file=sys.stderr)
                return 2
            for name, path in PAGES:
                row = {"width": width, "height": height, "page": name, "problems": []}
                page.goto(BASE + path, wait_until="networkidle")
                if name.startswith("curation"):
                    page.wait_for_selector("#project-select option", state="attached", timeout=30000)
                    page.wait_for_timeout(1500)
                    if name == "curation-source":
                        page.evaluate("() => { document.getElementById('filters-button').click(); document.getElementById('apply-filters').click() }")  # load the queue (a read)
                        try:
                            page.wait_for_selector("#queue-list button[data-image-id]", state="attached", timeout=60000)
                        except Exception:
                            pass
                        row["image_selected"] = page.evaluate(SELECT_IMAGE)
                        page.wait_for_timeout(1500)
                    else:
                        page.wait_for_timeout(1000)
                    page.evaluate(FORCE_FLOATERS)
                    page.wait_for_timeout(200)
                row.update(page.evaluate(OVERFLOW))
                if row["sw"] > row["iw"]:
                    row["problems"].append(f"horizontal scroll: scrollWidth {row['sw']} > {row['iw']}")
                if name.startswith("curation"):
                    m = page.evaluate(RECTS)
                    row["tray"], row["selection_bar"] = m["tray"], m["sel"]
                    row["tray_ok"] = check_floater("pending-tray", m["tray"], m, row["problems"])
                    row["selection_bar_ok"] = check_floater("crop-selection-bar", m["sel"], m, row["problems"])
                    if name == "curation-source" and m["tray"] is None:
                        row["problems"].append("pending tray not shown")
                    if name == "curation-crop" and m["sel"] is None:
                        row["problems"].append("crop selection bar not shown")
                if name == "curation-source":  # the Review popover opens fully inside the viewport, above the pill
                    page.evaluate("() => document.getElementById('pending-review').click()")
                    page.wait_for_timeout(300)
                    pr = page.evaluate("() => { const e = document.getElementById('pending-review-panel'); const b = e.getBoundingClientRect(); const t = document.getElementById('pending-tray').getBoundingClientRect(); return { t: b.top, b: b.bottom, l: b.left, r: b.right, pillTop: t.top, vw: innerWidth, vh: innerHeight } }")
                    row["review_panel"] = pr
                    if not (pr["t"] >= -0.5 and pr["b"] <= pr["pillTop"] + 0.5 and pr["l"] >= -0.5 and pr["r"] <= pr["vw"] + 0.5):
                        row["problems"].append(f"review panel not fully above the pill: {pr}")
                page.screenshot(path=str(SHOTS / f"responsive-{name}-{width}x{height}.png"))
                rows.append(row)
                if row["problems"]:
                    failures.append(row)
            context.close()

        # Touch (hover: none): the crop tile's open-source button is visible and focusable.
        context = browser.new_context(viewport={"width": 390, "height": 800}, has_touch=True, is_mobile=True)
        page = context.new_page()
        login(page, password)
        page.goto(f"{BASE}/curation#project={PROJECT}&view=crop", wait_until="networkidle")
        page.wait_for_selector("#project-select option", state="attached", timeout=30000)
        page.wait_for_timeout(2500)
        touch = page.evaluate("""() => {
          const b = document.querySelector('.crop-open-source')
          if (!b) return { found: false }
          return { found: true, hoverNone: matchMedia('(hover: none)').matches, opacity: getComputedStyle(b).opacity, tabIndex: b.tabIndex, display: getComputedStyle(b).display }
        }""")
        context.close()
        browser.close()
    touch_ok = bool(touch.get("found") and touch.get("hoverNone") and touch.get("opacity") == "1"
                    and touch.get("tabIndex", -1) >= 0 and touch.get("display") != "none")
    summary = {"checks": len(rows), "failures": len(failures), "touch_open_source": touch, "touch_ok": touch_ok, "rows": rows}
    (SHOTS / "responsive-sweep.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"checks: {len(rows)}; failures: {len(failures)}; touch open-source ok: {touch_ok} {touch}")
    for r in failures:
        print(f"  FAIL {r['page']} @{r['width']}x{r['height']}: " + "; ".join(r["problems"]))
    return 0 if not failures and touch_ok else 1


if __name__ == "__main__":
    sys.exit(main())
