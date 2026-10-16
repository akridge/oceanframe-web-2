"""Task 8 screenshots of the curation views on shared components (read-only: selection is client-side only).

  OF_PW2="$(cat "$TMP/of-tester2-password.txt")" MSYS_NO_PATHCONV=1 docker run --rm --network oceanframe-net -e OF_PW2 \
    -v "$PWD/tests/browser:/work" -w /work mcr.microsoft.com/playwright/python:v1.47.0-jammy \
    sh -c "pip install -q playwright==1.47.0 2>/dev/null; python curation_views_shots.py"

Output goes to tests/browser/shots/task8/.
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
OUT = Path(__file__).resolve().parent / "shots" / "task8"


def main() -> int:
    password = os.environ.get("OF_PW2")
    if not password:
        print("OF_PW2 is not set", file=sys.stderr)
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        login = page.request.post(f"{BASE}/api/auth/login", data={"username": USER, "password": password})
        if login.status != 200:
            print(f"login failed: HTTP {login.status}", file=sys.stderr)
            return 2
        for theme in ("dark", "light"):
            page.goto(f"{BASE}/curation#project={PROJECT}&view=crop")
            page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
            page.wait_for_selector(".curation-crop-card", timeout=20000)
            page.wait_for_timeout(1200)
            page.screenshot(path=str(OUT / f"crops-{theme}.png"))
            report[f"{theme}_tiles"] = page.locator(".curation-crop-card").count()
            report[f"{theme}_bar_hidden_when_none"] = not page.is_visible("#crop-selection-bar")
            page.locator(".curation-crop-card").nth(0).hover()
            page.locator(".curation-crop-card .crop-select").nth(0).check()
            page.locator(".curation-crop-card .crop-select").nth(1).check()
            page.wait_for_timeout(300)
            report[f"{theme}_bar_visible_when_selected"] = page.is_visible("#crop-selection-bar")
            box = page.locator("#crop-selection-bar").bounding_box()
            report[f"{theme}_bar_box"] = box
            page.screenshot(path=str(OUT / f"crops-selected-{theme}.png"))
            page.click("#crop-bar-clear")
            report[f"{theme}_bar_hidden_after_clear"] = not page.is_visible("#crop-selection-bar")
        # open-source button: does not toggle selection, opens the source view with a Back bar to the crop grid
        page.goto(f"{BASE}/curation#project={PROJECT}&view=crop")
        page.wait_for_selector(".curation-crop-card", timeout=20000)
        card = page.locator(".curation-crop-card").nth(2)
        card.hover()
        page.screenshot(path=str(OUT / "crop-hover.png"))
        card.locator(".crop-open-source").click()
        page.wait_for_timeout(1500)
        report["open_source_selection_unchanged"] = page.inner_text("#crop-selection-count") == "0 selected"
        report["back_label"] = page.inner_text("#back-label") if page.is_visible("#back-bar") else None
        page.click("#back-button")
        page.wait_for_timeout(1000)
        report["back_to_crop_view"] = page.is_visible("#crop-grid-panel")
        for view, name in (("quality", "quality"), ("matrix", "matrix"), ("status", "status"), ("versions", "history")):
            page.goto(f"{BASE}/curation#project={PROJECT}&view={view}")
            page.wait_for_timeout(2500)
            page.screenshot(path=str(OUT / f"{name}.png"), full_page=False)
        browser.close()
    report["page_errors"] = errors
    (OUT / "summary.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
