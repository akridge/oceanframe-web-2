"""UI tour: every page in dark and light at 1440x900, plus curation at 390x844.

Signed in as tester2 (OF_PW2). Pages: /, /collections, /collections/new, /frames, /account and /curation (NOAA
project: the source view with an image loaded, and the crop view). Dark first, chosen through the avatar menu; then
the theme is switched to light through the avatar menu (the real persistence path) and every page is visited again.
Asserts: no pageerror and no console.error (404s for crop previews of known-missing images are ignored); the theme
survives a reload (documentElement.dataset.theme === 'light'); Ctrl+K opens #of-palette, and typing "crops" + Enter
lands on the curation crop view; the palette offers Sign out and Account (signed in) and no Commit changes (nothing
uncommitted); no horizontal page scroll. Notes (not failures): tester2's "/" redirects to /collections/new. That is the
library's "no collections yet" onboarding (static/library/app.js refreshStatus sends the browser to /collections/new when
/api/library/status lists no sources), not a bug; the library page itself is only shown once a collection exists, so the tour
does not cover it (creating a collection would write data).

Read-only apart from the stored theme preference, which is put back to "system" at the end.

  OF_PW2="$(cat "$TMP/of-tester2-password.txt")" MSYS_NO_PATHCONV=1 docker run --rm --network oceanframe-net -e OF_PW2 \
    -v "$PWD/tests/browser:/work" -w /work mcr.microsoft.com/playwright/python:v1.47.0-jammy \
    sh -c "pip install -q playwright==1.47.0 2>/dev/null; python ui_tour.py"

Output (screenshots tour-*.png, ui-tour.json) goes to tests/browser/shots/.
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
PAGES = [("home", "/"), ("collections", "/collections"), ("collection-new", "/collections/new"), ("frames", "/frames"),
         ("account", "/account"), ("curation-source", f"/curation#project={PROJECT}&view=source"),
         ("curation-crop", f"/curation#project={PROJECT}&view=crop")]
notes: list = []
failures: list = []
errors: list = []
current = ["-"]


def on_console(msg):
    if msg.type != "error":
        return
    loc = (msg.location or {}).get("url", "")
    if "404" in msg.text and "crop-preview" in loc:
        return  # known-missing source images
    errors.append(f"[{current[0]}] console.error: {msg.text} {loc}")


def watch(page):
    page.on("pageerror", lambda e: errors.append(f"[{current[0]}] pageerror: {e}"))
    page.on("console", on_console)


def pick_theme(page, value):
    page.click("#rail-avatar")
    page.click(f"#user-menu [data-theme='{value}']")
    page.wait_for_timeout(300)
    page.keyboard.press("Escape")


def visit(page, name, path, theme, width, height):
    current[0] = f"{name}/{theme}/{width}"
    page.goto(BASE + path, wait_until="networkidle")
    final = page.url.replace(BASE, "")
    if final.split("#")[0] != path.split("#")[0]:
        notes.append(f"{path} redirected to {final}")
    if name == "curation-source":
        page.wait_for_selector("#project-select option", state="attached", timeout=30000)
        page.wait_for_timeout(1500)
        page.evaluate("() => { document.getElementById('filters-button').click(); document.getElementById('apply-filters').click() }")
        try:
            page.wait_for_selector("#queue-list button[data-image-id]", state="attached", timeout=60000)
            page.evaluate("() => document.querySelector('#queue-list button[data-image-id]').click()")
        except Exception as exc:
            failures.append(f"{current[0]}: no image in the queue ({exc})")
        page.wait_for_timeout(3500)
    elif name == "curation-crop":
        page.wait_for_selector("#project-select option", state="attached", timeout=30000)
        try:
            page.wait_for_selector(".curation-crop-card", timeout=30000)
        except Exception as exc:
            failures.append(f"{current[0]}: no crop tiles ({exc})")
        page.wait_for_timeout(2000)
    else:
        page.wait_for_timeout(800)
    got = page.evaluate("document.documentElement.dataset.theme")
    if got != theme:
        failures.append(f"{current[0]}: data-theme is {got!r}, expected {theme!r}")
    sw = page.evaluate("() => [document.documentElement.scrollWidth, innerWidth]")
    if sw[0] > sw[1]:
        failures.append(f"{current[0]}: horizontal scroll {sw}")
    page.screenshot(path=str(SHOTS / f"tour-{name}-{theme}-{width}.png"))


def login(page, password):
    return page.request.post(f"{BASE}/api/auth/login", data={"username": USER, "password": password}).status == 200


def main() -> int:
    password = os.environ.get("OF_PW2")
    if not password:
        print("OF_PW2 is not set", file=sys.stderr)
        return 2
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        watch(page)
        if not login(page, password):
            print("login failed", file=sys.stderr)
            return 2
        page.goto(BASE + "/collections", wait_until="networkidle")
        pick_theme(page, "dark")
        for name, path in PAGES:
            visit(page, name, path, "dark", 1440, 900)

        page.goto(BASE + "/collections", wait_until="networkidle")
        page.click("#rail-avatar")
        page.screenshot(path=str(SHOTS / "tour-avatar-menu-dark-1440.png"))
        page.keyboard.press("Escape")
        pick_theme(page, "light")
        if page.evaluate("document.documentElement.dataset.theme") != "light":
            failures.append("light theme not applied from the avatar menu")
        page.reload(wait_until="networkidle")
        if page.evaluate("document.documentElement.dataset.theme") != "light":
            failures.append("theme did not persist across reload")
        for name, path in PAGES:
            visit(page, name, path, "light", 1440, 900)

        # Command palette on the curation page.
        current[0] = "palette"
        page.goto(f"{BASE}/curation#project={PROJECT}&view=source", wait_until="networkidle")
        page.wait_for_selector("#project-select option", state="attached", timeout=30000)
        page.wait_for_timeout(1500)
        page.keyboard.press("Control+k")
        page.wait_for_timeout(400)
        if not page.evaluate("() => { const d = document.getElementById('of-palette'); return !!(d && d.open) }"):
            failures.append("Ctrl+K did not open #of-palette")
        else:
            page.screenshot(path=str(SHOTS / "tour-palette-light-1440.png"))
            for query, label, want in (("sign out", "Sign out", True), ("account", "Account", True), ("commit", "Commit changes", False)):
                page.fill("#of-palette-input", query)
                page.wait_for_timeout(700)
                got = label in page.eval_on_selector_all(".palette-item", "e => e.map(x => x.textContent.trim())")
                if got != want:
                    failures.append(f"palette '{query}': '{label}' present={got}, expected {want}")
            page.fill("#of-palette-input", "")
            page.keyboard.type("crops")
            page.wait_for_timeout(600)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2500)
            if "view=crop" not in page.url:
                failures.append(f"palette 'crops' + Enter did not land on the crop view: {page.url}")

        # Curation on a phone, light then dark (the theme is set through the same stored preference).
        phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2).new_page()
        watch(phone)
        login(phone, password)
        for theme in ("light", "dark"):
            phone.goto(BASE + "/collections", wait_until="networkidle")
            phone.evaluate(f"() => window.OF.ui.theme.set('{theme}')")
            for name, path in PAGES[-2:]:
                visit(phone, name, path, theme, 390, 844)
        page.evaluate("() => window.OF.ui.theme.set('system')")
        browser.close()
    summary = {"failures": failures, "console_errors": errors, "notes": sorted(set(notes)), "pages": [n for n, _ in PAGES]}
    (SHOTS / "ui-tour.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 1 if failures or errors else 0


if __name__ == "__main__":
    sys.exit(main())
