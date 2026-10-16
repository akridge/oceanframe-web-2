"""Curation top bar width sweep (Task 6, fix round 1).

For every viewport width from 360 to 1920 px in 40 px steps, the six view tabs must each sit fully
inside the viewport, or at <= 640 px be reachable by scrolling the tab row sideways. The commit pill
and the SQLite badge must be showing while that holds. Neither the pill nor the badge is guaranteed
on a live stack: the badge only appears on a SQLite app DB, and the pill only when the user has
uncommitted changes. So the script makes both show, the way the page itself would: it sets
collab.js-shaped text on the real #commit-pill and drops its d-none, and inserts a badge with the
template's markup and text when the server rendered none. It writes nothing to the server.

Run inside the Playwright image on the stack network (password via env, never printed):

  OF_PW2="$(cat "$TMP/of-tester2-password.txt")" MSYS_NO_PATHCONV=1 docker run --rm --network oceanframe-net -e OF_PW2 \
    -v "$PWD/tests/browser:/work" -w /work mcr.microsoft.com/playwright/python:v1.47.0-jammy \
    sh -c "pip install -q playwright==1.47.0 2>/dev/null; python curation_topbar_sweep.py"

Output (JSON summary and screenshots) goes to tests/browser/shots/.
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
TABS = ["source-view-button", "crop-view-button", "quality-view-button", "status-view-button", "matrix-view-button", "versions-view-button"]
SHOTS = Path(__file__).resolve().parent / "shots"
SHOT_WIDTHS = {360, 400, 640, 680, 1000, 1280, 1440, 1920}

FORCE_PILL_AND_BADGE = """() => {
  const pill = document.getElementById('commit-pill')
  pill.textContent = '12 uncommitted changes · Commit…'
  pill.classList.remove('d-none')
  if (!document.getElementById('app-db-badge')) {
    const badge = document.createElement('span')
    badge.className = 'badge text-bg-warning'
    badge.dataset.sweepBadge = ''
    badge.title = 'Local SQLite database — not shared.'
    badge.textContent = 'Local SQLite'
    document.getElementById('dataset-mode-badge').after(badge)
  }
}"""

MEASURE = """(tabs) => {
  const vw = document.documentElement.clientWidth
  const box = el => { if (!el) return null; const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, width: r.width, height: r.height } }
  const inside = b => !!b && b.width > 0 && b.height > 0 && b.left >= -0.5 && b.right <= vw + 0.5
  const badge = document.getElementById('app-db-badge') || document.querySelector('[data-sweep-badge]')
  const pill = document.getElementById('commit-pill')
  return {
    vw,
    pageOverflowX: document.documentElement.scrollWidth > vw + 1,
    topbarHeight: document.querySelector('.topbar').getBoundingClientRect().height,
    tabs: Object.fromEntries(tabs.map(id => [id, inside(box(document.getElementById(id)))])),
    pill: inside(box(pill)) && getComputedStyle(pill).display !== 'none',
    badge: inside(box(badge)),
    more: inside(box(document.getElementById('topbar-more'))),
  }
}"""


def main() -> int:
    password = os.environ.get("OF_PW2")
    if not password:
        print("OF_PW2 is not set", file=sys.stderr)
        return 2
    SHOTS.mkdir(parents=True, exist_ok=True)
    results, failures = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        login = page.request.post(f"{BASE}/api/auth/login", data={"username": USER, "password": password})
        if login.status != 200:
            print(f"login failed: HTTP {login.status}", file=sys.stderr)
            return 2
        page.goto(f"{BASE}/curation#project={PROJECT}&view=matrix", wait_until="networkidle")
        page.wait_for_selector("#project-select option", state="attached", timeout=30000)
        page.wait_for_timeout(1500)  # let refreshCommitPill and the matrix load settle before forcing the pill
        page.evaluate(FORCE_PILL_AND_BADGE)

        # The More menu holds the project actions with their ids.
        page.click("#topbar-more")
        menu_ok = page.locator("#topbar-more-menu").is_visible() and page.locator("#members-button").is_visible()
        page.screenshot(path=str(SHOTS / "curation-topbar-more-1440.png"))
        page.keyboard.press("Escape")

        for width in range(360, 1921, 40):
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(60)
            m = page.evaluate(MEASURE, TABS)
            reached_by_scroll = []
            if width <= 640:
                for tab in TABS:
                    if not m["tabs"][tab]:
                        page.evaluate("id => document.getElementById(id).scrollIntoView({ block: 'nearest', inline: 'nearest' })", tab)
                        page.wait_for_timeout(30)
                        if page.evaluate(MEASURE, TABS)["tabs"][tab]:
                            m["tabs"][tab] = True
                            reached_by_scroll.append(tab)
                page.evaluate("() => { document.querySelector('.curation-view-tabs').scrollLeft = 0; window.scrollTo(0, 0) }")
            ok = all(m["tabs"].values()) and m["pill"] and m["badge"] and m["more"] and not m["pageOverflowX"]
            row = {"width": width, "ok": ok, "scrolled": reached_by_scroll, **m}
            results.append(row)
            if not ok:
                failures.append(row)
            if width in SHOT_WIDTHS:
                page.screenshot(path=str(SHOTS / f"curation-topbar-{width}.png"), clip={"x": 0, "y": 0, "width": width, "height": 160})
        browser.close()

    summary = {"widths": len(results), "failures": len(failures), "more_menu_ok": menu_ok, "results": results}
    (SHOTS / "curation-topbar-sweep.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"widths checked: {len(results)}; failures: {len(failures)}; more menu ok: {menu_ok}")
    for row in failures:
        missing = [t for t, v in row["tabs"].items() if not v]
        print(f"  FAIL {row['width']}px: tabs missing {missing}, pill={row['pill']}, badge={row['badge']}, more={row['more']}, page h-scroll={row['pageOverflowX']}")
    heights = sorted({round(r["topbarHeight"]) for r in results})
    print(f"top bar heights seen: {heights}")
    print("scrolled into view at: " + ", ".join(f"{r['width']}px({len(r['scrolled'])})" for r in results if r["scrolled"]))
    return 0 if not failures and menu_ok else 1


if __name__ == "__main__":
    sys.exit(main())
