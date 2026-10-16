"""Task 10 live check of the command palette providers (read-only).

  OF_PW2="$(cat "$TMP/of-tester2-password.txt")" MSYS_NO_PATHCONV=1 docker run --rm --network oceanframe-net -e OF_PW2 \
    -v "$PWD/tests/browser:/work" -w /work mcr.microsoft.com/playwright/python:v1.47.0-jammy \
    sh -c "pip install -q playwright==1.47.0 2>/dev/null; python palette_providers_check.py"
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = os.environ.get("OF_BASE", "http://oceanframe-app:8080")
PROJECT = os.environ.get("OF_PROJECT", "53d400c6-097b-4995-b3af-edb7a7528925")
OUT = Path(__file__).resolve().parent / "shots" / "task10"


def main() -> int:
    pw = os.environ.get("OF_PW2")
    if not pw:
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    rep: dict = {}
    errs: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
        page.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
        page.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" else None)
        r = page.request.post(f"{BASE}/api/auth/login", data={"username": "tester2", "password": pw})
        if r.status != 200:
            print("login", r.status); return 2
        page.goto(f"{BASE}/curation#project={PROJECT}&view=source")
        page.wait_for_timeout(8000); print(page.evaluate("[typeof state, state.view, state.projectId, state.images.length, state.sourceQueueLoaded]"), page.url, flush=True)
        imgs = page.evaluate("fetch(`/api/curation/projects/${state.projectId}/source-images?limit=3&source_missing=any`).then(r=>r.json())"); print(str(imgs)[:300], flush=True); name = imgs["images"][0]["image_name"] or imgs["images"][0]["source_image_uri"].split("/").pop()
        first_id = imgs["images"][0]["first_item_id"]
        # (a)
        page.keyboard.press("Control+k"); page.fill("#of-palette-input", "crop"); page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / "a-crop.png"))
        page.keyboard.press("Enter"); page.wait_for_timeout(1500)
        rep["a_view"] = page.evaluate("state.view")
        # (b)
        q = name[:5]
        page.keyboard.press("Control+k"); page.fill("#of-palette-input", q); page.wait_for_timeout(1500)
        groups = page.eval_on_selector_all(".palette-group", "e=>e.map(x=>x.textContent)")
        items = page.eval_on_selector_all(".palette-item", "e=>e.map(x=>x.textContent)")
        rep["b_groups"] = groups; rep["b_items"] = items[:4]
        page.screenshot(path=str(OUT / "b-images.png"))
        img_idx = None
        # pick first item under Images group
        page.evaluate("""()=>{const g=[...document.querySelectorAll('.palette-group')].find(x=>x.textContent==='Images'); if(g){const n=g.nextElementSibling; n.dispatchEvent(new MouseEvent('mousemove',{bubbles:true}));}}""")
        page.keyboard.press("Enter"); page.wait_for_timeout(2500)
        rep["b_view"] = page.evaluate("state.view")
        rep["b_current"] = page.evaluate("state.currentImage && (state.currentImage.image_name || state.currentImage.source_image_uri)")
        rep["b_expected_name"] = name
        # (c)
        page.goto(f"{BASE}/"); page.wait_for_timeout(2000)
        page.keyboard.press("Control+k"); page.fill("#of-palette-input", "new collection"); page.wait_for_timeout(700)
        rep["c_items"] = page.eval_on_selector_all(".palette-item", "e=>e.map(x=>x.textContent)")
        page.screenshot(path=str(OUT / "c-library.png"))
        b.close()
    rep["d_errors"] = errs
    print(json.dumps(rep, indent=1))
    return 0

sys.exit(main())
