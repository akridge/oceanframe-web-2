"""The Patch T3 confusion-matrix flow, as a curator meets it on a new install (read-only).

Signs in through the login form with no ``next`` and expects to land on curation; opens the Hugging Face
starter project; on the matrix either finds the baseline evaluation's table (clicks an off-diagonal cell
and checks that the crops come into view) or the empty-state card explaining where it stands; then checks
the review keys on the source view (R focuses the label; nothing is saved). Screenshots in both themes.

  OF_PW="..." MSYS_NO_PATHCONV=1 docker run --rm --network oceanframe-net -e OF_PW \
    -v "$PWD/tests/browser:/work" -w /work mcr.microsoft.com/playwright/python:v1.47.0-jammy \
    sh -c "pip install -q playwright==1.47.0 2>/dev/null; python matrix_flow_check.py"

OF_USER (default user1) and OF_PW sign in; OF_BASE overrides the app URL. Exit 0 pass, 1 a failed check, 2 setup.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("OF_BASE", "http://oceanframe-app:8080")
USER = os.environ.get("OF_USER", "user1")
HUB_PROJECT = "NOAA Pacific T3 (Hugging Face)"
OUT = Path(__file__).resolve().parent / "shots" / "matrix_flow"
WRITE = os.environ.get("OF_WRITE") == "1"


def flag_and_restore(page, project_id: str, check) -> None:
    """F flags the selected item, saves and moves on; the item is then put back as it was."""
    item = page.evaluate("({...state.selected})")
    api = f"{BASE}/api/curation/projects/{project_id}/items/{item['id']}"
    page.locator("body").click(position={"x": 5, "y": 5})
    started = page.evaluate("performance.now()")
    page.keyboard.press("f")
    page.wait_for_function(f"state.selected && state.selected.id !== {item['id']}", timeout=30000)
    elapsed = page.evaluate("performance.now()") - started
    detail = page.request.get(f"{BASE}/api/curation/projects/{project_id}/source-images/{item['id']}").json()
    saved = next(row for row in detail["items"] if row["id"] == item["id"])
    check("F flags, saves and moves on", saved["review_state"] == "flagged", {"ms": round(elapsed)})
    check("F moves on within 3 s", elapsed < 3000, round(elapsed))
    restored = page.request.patch(api, data={
        "expected_revision": saved["revision"], "current_label": item["current_label"], "included": item["included"],
        "review_state": item["review_state"], "reason": "browser check: undo"})
    check("the flagged item is put back", restored.status == 200 and restored.json()["review_state"] == item["review_state"],
          restored.status)


def main() -> int:
    password = os.environ.get("OF_PW")
    if not password:
        print("OF_PW is not set")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    failures: list[str] = []
    errors: list[str] = []

    def check(name: str, ok: bool, detail=None) -> None:
        report[name] = {"ok": bool(ok), **({"detail": detail} if detail is not None else {})}
        if not ok:
            failures.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for theme in ("dark", "light"):
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.add_init_script(f"try {{ localStorage.setItem('of-theme', '{theme}') }} catch (_) {{}}")
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)

            page.goto(f"{BASE}/login")
            page.fill("#login-form [name=username]", USER)
            page.fill("#login-form [name=password]", password)
            page.click("#login-submit")
            try:
                page.wait_for_url("**/curation**", timeout=15000)
            except Exception:  # noqa: BLE001
                print("login did not reach /curation:", page.url)
                return 2
            check(f"{theme}: lands on curation after sign-in", "/curation" in page.url, page.url)
            page.wait_for_function("typeof state !== 'undefined' && state.projects && state.projects.length > 0", timeout=30000)
            project = page.evaluate(f"state.projects.find(p => p.name === {json.dumps(HUB_PROJECT)})")
            if not project:
                print("no starter project yet:", page.evaluate("state.projects.map(p => p.name)"))
                return 2
            page.goto(f"{BASE}/curation#project={project['id']}&view=matrix")
            page.reload()
            page.wait_for_function("state.view === 'matrix' && state.projectId", timeout=30000)
            page.wait_for_timeout(2500)
            check(f"{theme}: matrix tab is the active tab",
                  page.evaluate("document.getElementById('matrix-view-button').classList.contains('active')"))
            check(f"{theme}: no read-only notice (the account is a member)",
                  page.evaluate("document.getElementById('membership-notice').classList.contains('d-none')"))
            has_table = page.evaluate("!document.getElementById('matrix-scroll').classList.contains('d-none')")
            report[f"{theme}: matrix shown"] = has_table
            page.screenshot(path=str(OUT / f"{theme}-matrix.png"))
            if has_table:
                cell = page.locator("[data-matrix-cell]:not(.diagonal)").filter(has_not_text="0").first
                cell.scroll_into_view_if_needed()
                cell.click()
                page.wait_for_selector("#evaluation-drilldown:not(.d-none)", timeout=20000)
                page.wait_for_timeout(1500)
                top = page.evaluate("document.getElementById('evaluation-drilldown').getBoundingClientRect().top")
                check(f"{theme}: evidence crops scroll into view", 0 <= top < 900, top)
                page.screenshot(path=str(OUT / f"{theme}-evidence.png"))
            else:
                card = page.locator(".matrix-empty")
                check(f"{theme}: empty matrix explains itself", card.count() == 1,
                      card.inner_text()[:300] if card.count() else None)

            page.goto(f"{BASE}/curation#project={project['id']}&view=source")
            page.reload()
            page.wait_for_function("state.view === 'source' && state.images.length > 0", timeout=90000)
            page.wait_for_function("state.selected", timeout=60000)
            page.locator("body").click(position={"x": 5, "y": 5})
            page.keyboard.press("r")
            check(f"{theme}: R focuses the label", page.evaluate("document.activeElement.id") == "current-label")
            check(f"{theme}: review keys are listed", page.locator(".review-keys").is_visible())
            if WRITE and theme == "dark":
                flag_and_restore(page, project["id"], check)
            page.screenshot(path=str(OUT / f"{theme}-source.png"))
            context.close()
        browser.close()
    report["errors"] = errors
    print(json.dumps(report, indent=1))
    print("FAILED: " + ", ".join(failures) if failures else "PASS")
    return 1 if failures else 0


sys.exit(main())
