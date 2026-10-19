"""Read-only browser check of the P2 collaboration UI on the new shell (opens sheets, changes nothing).

tester2 is a curator on the HF project and not a member of NOAA, which a lead has made members-only (Members
sheet -> Members only; an open project lets every curator account curate). Checks: the read-only membership notice on
NOAA, the change-set history on HF, the Members sheet and the Releases sheet (both opened from the top bar's
More menu, #topbar-more -> #topbar-more-menu), and the compare-people form on the Dataset status tab.

Changes from the original (pre-shell) script: dialogs are sheets opened through the More menu; releases are
asserted by "any release listed" rather than one name (the DB holds "Pacific T3 deployment check" today, but
the name is data); the output folder is tests/browser/shots/; exit code is 1 on a failed check or an
unexpected console error.

  OF_PW2="$(cat "$TMP/of-tester2-password.txt")" MSYS_NO_PATHCONV=1 docker run --rm --network oceanframe-net -e OF_PW2 \
    -v "$PWD/tests/browser:/work" -w /work mcr.microsoft.com/playwright/python:v1.47.0-jammy \
    sh -c "pip install -q playwright==1.47.0 2>/dev/null; python p2_browser_check.py"
"""
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("OF_BASE", "http://oceanframe-app:8080")
HF = os.environ.get("OF_HF_PROJECT", "8341e5fc-3868-4e48-af44-77ba4c6544a6")
NOAA = os.environ.get("OF_PROJECT", "53d400c6-097b-4995-b3af-edb7a7528925")
OUT = Path(__file__).resolve().parent / "shots"
OUT.mkdir(parents=True, exist_ok=True)
results, errors = {}, []


def check(name, ok, detail=""):
    results[name] = {"ok": bool(ok), "detail": str(detail)[:250]}


def login(page, user, password):
    res = page.request.post(f"{BASE}/api/auth/login", data={"username": user, "password": password})
    return res.status == 200


def open_more_item(page, item_id):
    page.click("#topbar-more")
    page.wait_for_selector("#topbar-more-menu:not([hidden])")
    page.click(item_id)
    page.wait_for_timeout(1500)


def load(page, project, view):
    page.goto(f"{BASE}/curation#project={project}&view={view}")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(4000)


def main():
    password = os.environ.get("OF_PW2")
    if not password:
        print("OF_PW2 is not set", file=sys.stderr)
        return 2
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.set_default_timeout(20000)
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" and "404" not in m.text else None)
        check("signed in", login(page, "tester2", password))

        load(page, NOAA, "versions")
        notice = page.inner_text("#membership-notice") if page.is_visible("#membership-notice") else ""
        check("non-member sees read-only notice naming leads", "ask a project lead" in notice.lower(), notice)
        check("non-member publish disabled", page.eval_on_selector("#publish-button", "el => el.disabled"))
        page.screenshot(path=str(OUT / "p2-1-nonmember.png"))

        load(page, HF, "versions")
        check("member: no read-only notice", not page.is_visible("#membership-notice"))
        history = page.inner_text("#history-list")
        check("history lists the deployment-check change set", "Deployment check" in history, history[:160])
        page.screenshot(path=str(OUT / "p2-2-history.png"))
        page.click("[data-history-open]")
        page.wait_for_timeout(1500)
        check("change set detail opens with Show these items", page.locator("[data-history-show]").count() > 0)

        open_more_item(page, "#members-button")
        check("members sheet is open", page.eval_on_selector("#members-dialog", "el => el.open"))
        members = page.inner_text("#members-list")
        check("members sheet lists tester and tester two", "tester" in members.lower() and "tester two" in members.lower(), members[:200])
        check("curator cannot add members", page.eval_on_selector("#members-add-button", "el => el.disabled"))
        page.screenshot(path=str(OUT / "p2-3-members.png"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        check("members sheet closes on Escape", not page.eval_on_selector("#members-dialog", "el => el.open"))

        page.click("#status-view-button")
        page.wait_for_timeout(3000)
        check("compare-people form filled", page.locator("#people-a option").count() >= 2, page.locator("#people-a option").count())
        page.click("#people-run")
        page.wait_for_timeout(2500)
        check("compare people renders", "changes" in page.inner_text("#people-result"), page.inner_text("#people-result")[:160])
        page.screenshot(path=str(OUT / "p2-4-people.png"))

        open_more_item(page, "#releases-button")
        check("releases sheet is open", page.eval_on_selector("#releases-dialog", "el => el.open"))
        releases = page.inner_text("#releases-list")
        check("releases sheet lists a release", "Pacific T3 deployment check" in releases or "T3" in releases or "T1" in releases, releases[:160])
        check("curator cannot build a release", page.eval_on_selector("#release-build", "el => el.disabled"))
        page.screenshot(path=str(OUT / "p2-5-releases.png"))
        browser.close()

    failed = [k for k, v in results.items() if not v["ok"]]
    print(json.dumps({"results": results, "errors": errors[:20]}, indent=1))
    print(f"checks: {len(results)}; failed: {failed}; unexpected console errors: {len(errors)}")
    return 1 if failed or errors else 0


if __name__ == "__main__":
    sys.exit(main())
