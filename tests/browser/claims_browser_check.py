"""Two curators at once on the Hugging Face starter project: claims, live refresh and the Activity tab (read-only).

Signs in as two starter accounts in separate browser contexts (OF_USER, default user1, a curator; OF_USER2,
default test, a lead) and checks, without saving anything:
  - the first curator's open image is claimed and left out of the second curator's queue;
  - the second curator opening that image is told who has it;
  - Quality review claims the undecided patches it loads, and the other's item list leaves them out;
  - each sees the other on the Activity tab, with the tab's online badge;
  - moving to another view, and closing the page, lets the claims go.

  MSYS_NO_PATHCONV=1 docker run --rm --network oceanframe-net -e OF_PW -e OF_PW2 \
    -v "$PWD/tests/browser:/work" -w /work mcr.microsoft.com/playwright/python:v1.47.0-jammy \
    sh -c "pip install -q playwright==1.47.0 2>/dev/null; python claims_browser_check.py"

OF_PW and OF_PW2 are the two passwords (never printed). Exit 0 pass, 1 a failed check, 2 setup.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("OF_BASE", "http://oceanframe-app:8080")
USERS = (os.environ.get("OF_USER", "user1"), os.environ.get("OF_USER2", "test"))
HUB_PROJECT = "NOAA Pacific T3 (Hugging Face)"
OUT = Path(__file__).resolve().parent / "shots" / "claims"


def sign_in(browser, user: str, password: str, errors: list[str]):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.on("pageerror", lambda e: errors.append(f"{user} pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"{user} console: {m.text}") if m.type == "error" else None)
    page.goto(f"{BASE}/login")
    page.fill("#login-form [name=username]", user)
    page.fill("#login-form [name=password]", password)
    page.click("#login-submit")
    page.wait_for_url("**/curation**", timeout=15000)
    page.wait_for_function("typeof state !== 'undefined' && state.projects && state.projects.length > 0", timeout=30000)
    return context, page


def open_view(page, project_id: str, view: str) -> None:
    page.goto(f"{BASE}/curation#project={project_id}&view={view}")
    page.reload()
    page.wait_for_function(f"state.view === '{view}' && state.projectId && live.watermark != null", timeout=30000)


def claimed(page, project_id: str) -> dict:
    """What others hold, as this user's change feed reports it."""
    feed = page.evaluate(f"request('/api/curation/projects/{project_id}/changes')")
    return {entry["item_id"]: entry["user_name"] for entry in feed["claimed"]}


def settled(page, read, ok, timeout_ms: int = 10000):
    """Read until *ok* holds (claims are posted after live.claimKey is set), or give the last reading."""
    value = read()
    for _ in range(timeout_ms // 500):
        if ok(value):
            break
        page.wait_for_timeout(500)
        value = read()
    return value


def main() -> int:
    passwords = (os.environ.get("OF_PW"), os.environ.get("OF_PW2"))
    if not all(passwords):
        print("OF_PW and OF_PW2 must be set")
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
        try:
            (ctx_a, a), (ctx_b, b) = (sign_in(browser, user, pw, errors) for user, pw in zip(USERS, passwords))
        except Exception as exc:  # noqa: BLE001
            print("sign-in failed:", exc)
            return 2
        project = a.evaluate(f"state.projects.find(p => p.name === {json.dumps(HUB_PROJECT)})")
        if not project:
            print("no starter project yet")
            return 2
        pid = project["id"]
        name_a = a.evaluate("session.user.label")

        # 1. A opens the source view: its first image is claimed.
        open_view(a, pid, "source")
        a.wait_for_function("state.currentImage && live.claimKey", timeout=30000)
        a.wait_for_timeout(1500)  # past any skip over images someone else had open
        image_a = a.evaluate("({ uri: state.currentImage.source_image_uri, first: state.currentImage.first_item_id,"
                             " ids: state.items.map(i => i.id) })")
        held = settled(b, lambda: claimed(b, pid), lambda h: set(image_a["ids"]) <= set(h))
        check("A's open image is claimed in A's name", set(image_a["ids"]) <= set(held) and
              {held[i] for i in image_a["ids"]} == {name_a}, {"points": len(image_a["ids"])})

        # 2. B's queue leaves it out; B does not start on it.
        open_view(b, pid, "source")
        b.wait_for_function("state.currentImage", timeout=30000)
        queue_b = b.evaluate("state.images.map(i => i.source_image_uri)")
        check("B's queue leaves out A's open image", image_a["uri"] not in queue_b, len(queue_b))
        check("B starts on a different image", b.evaluate("state.currentImage.source_image_uri") != image_a["uri"])

        # 3. B opening A's image on purpose is told who has it.
        b.evaluate(f"selectImage({image_a['first']})")
        b.wait_for_function("state.currentHeldBy !== undefined && state.currentImage && "
                            f"state.currentImage.source_image_uri === {json.dumps(image_a['uri'])}", timeout=15000)
        b.wait_for_timeout(1000)
        notice = b.evaluate("document.getElementById('curation-notice').classList.contains('d-none') ? '' :"
                            " document.getElementById('curation-notice-text').textContent")
        check("B is told A is reviewing it", name_a in notice and "reviewing this image" in notice, notice)
        b.screenshot(path=str(OUT / "b-told-who-has-it.png"))

        # 4. A moves to Quality review: the image is let go, the loaded undecided patches are claimed.
        a.evaluate("setView('quality')")
        # The claim set becomes exactly the undecided patches loaded there (which may include some of the image).
        a.wait_for_function("state.qualityItems.length > 0 && live.claimKey === claimIds().join(',')", timeout=60000)
        quality_ids = a.evaluate("state.qualityItems.filter(i => !i.savedDecision).map(i => i.reviewId)")
        let_go = set(image_a["ids"]) - set(quality_ids)
        held = settled(b, lambda: claimed(b, pid),
                       lambda h: not let_go & set(h) and set(quality_ids[:50]) <= set(h))
        check("leaving the source view lets the image go", not let_go & set(held), len(let_go))
        check("Quality review claims its undecided patches", set(quality_ids[:50]) <= set(held), len(quality_ids))
        listed = b.evaluate(f"request('/api/curation/projects/{pid}/items?limit=200&hide_claimed=true&quality_decision=pending')")
        check("B's patch list leaves A's patches out", not {i["id"] for i in listed["items"]} & set(quality_ids),
              listed["returned"])

        # 5. Activity: each sees the other, and the badge counts both.
        b.evaluate("setView('activity')")
        b.wait_for_function(f"live.online.some(p => p.name === {json.dumps(name_a)})", timeout=30000)
        b.wait_for_timeout(500)
        text = b.evaluate("document.getElementById('online-list').innerText")
        check("B's Activity tab shows A in Quality review", name_a in text and "Quality review" in text, text[:200])
        check("the online badge counts both", b.evaluate("document.getElementById('online-count').textContent") == "2")
        b.screenshot(path=str(OUT / "b-activity.png"))

        # 6. A leaves the page: its claims and its presence go at once (well inside the claim TTL).
        # Navigating away fires pagehide; Playwright's page.close() skips unload handlers.
        a.goto("about:blank")
        left = set(quality_ids) & set(settled(b, lambda: claimed(b, pid), lambda h: not set(quality_ids) & set(h)))
        check("leaving the page lets the claims go", not left, len(left))
        a.close()
        online = b.evaluate(f"request('/api/curation/projects/{pid}/presence', {{ method: 'POST',"
                            " headers: { 'Content-Type': 'application/json' },"
                            " body: JSON.stringify({ holder: live.holder, view: 'activity' }) })")["online"]
        check("leaving the page takes A off the online list",name_a not in {person["name"] for person in online})
        ctx_a.close()
        ctx_b.close()
        browser.close()

    report["errors"] = errors
    (OUT / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for name, result in report.items():
        if name != "errors":
            print(("PASS " if result["ok"] else "FAIL ") + name + (f"  ({result['detail']})" if "detail" in result else ""))
    if errors:
        print("page errors:", *errors[:10], sep="\n  ")
    return 1 if failures or errors else 0


if __name__ == "__main__":
    sys.exit(main())
