"""Image library navigation and the library ↔ curation links, on the local T3 samples (read-only).

Needs two collections indexed (the 2,000-image samples of pacific_all and pacific_t3_splits) with the
class path-tag rule. Signs in as OF_USER (default user1) and checks, without saving anything:
  - with no collection picked the folder tree lists the collections; picking one shows its folders, and
    switching collection while deep in a folder resets the tree and crumbs to the new collection;
  - the banner never prints "null"; the header chip says it is library-wide;
  - filters survive a reload through the URL hash; a search shows its own chip;
  - a slow search dims the grid with "Searching…"; an empty result names its filters and undoes the last;
  - a class tag (from the folder name) filters the grid;
  - a library image that is a curation patch opens it in curation, and the patch links back.

  MSYS_NO_PATHCONV=1 docker run --rm --network oceanframe-net -e OF_PW \
    -v "$PWD/tests/browser:/work" -w /work mcr.microsoft.com/playwright/python:v1.47.0-jammy \
    sh -c "pip install -q playwright==1.47.0 2>/dev/null; python p4a_library_check.py"

OF_PW is the password (never printed). Exit 0 pass, 1 a failed check, 2 setup.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

BASE = os.environ.get("OF_BASE", "http://oceanframe-app:8080")
USER = os.environ.get("OF_USER", "user1")
OUT = Path(__file__).resolve().parent / "shots" / "p4a"


def main() -> int:
    password = os.environ.get("OF_PW")
    if not password:
        print("OF_PW is not set")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    errors: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("PASS " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.goto(f"{BASE}/login")
        page.fill("#login-form [name=username]", USER)
        page.fill("#login-form [name=password]", password)
        page.click("#login-submit")
        page.wait_for_url("**/curation**", timeout=15000)

        def library(hash_: str = "") -> None:
            page.goto(f"{BASE}/{hash_}")
            page.wait_for_selector("#grid .tile, #grid-empty:not(.hidden)", timeout=30000)
            settle()

        def settle() -> None:
            page.wait_for_function("!document.querySelector('#grid').classList.contains('loading')"
                                   " && !document.querySelector('#folder-tree').classList.contains('stale')", timeout=30000)

        def tree() -> list[str]:
            return page.eval_on_selector_all("#folder-tree .folder-row .name", "els => els.map(e => e.textContent)")

        def crumbs() -> list[str]:
            return page.eval_on_selector_all("#folder-crumbs button", "els => els.map(e => e.textContent)")

        library()
        sources = page.evaluate("fetch('/api/library/sources').then(r => r.json())")["sources"]
        if len(sources) < 2:
            print(f"needs two collections, found {len(sources)}")
            return 2
        by_root = {s["root"].rstrip("/").rsplit("/", 1)[-1]: s for s in sources}
        t3_all, t3_hf = by_root.get("pacific_all"), by_root.get("pacific_t3_splits")
        if not t3_all or not t3_hf:
            print("needs the pacific_all and pacific_t3_splits samples")
            return 2

        check(set(tree()) >= {t3_all["label"], t3_hf["label"]}, "no collection picked: the tree lists the collections")
        check(page.text_content("#status-assets").startswith("library:"), "header chip is labelled library-wide")

        page.click(f"#folder-tree .folder-row:has-text('{t3_hf['label']}')")
        settle()
        # The sample took the first 2,000 objects, all under test/.
        check("test" in tree() and crumbs() == [t3_hf["label"]],
              f"picking a collection in the tree shows its split folders ({tree()[:4]} · {crumbs()})")
        check("null" not in page.text_content("#collection-banner"), "collection banner has no 'null'")
        page.click("#folder-tree .folder-row:has-text('test')")
        settle()
        page.click("#folder-tree .folder-row >> nth=1")  # a class folder under test/
        settle()
        deep = crumbs()
        check(len(deep) == 3 and deep[1] == "test", f"drilled into test/<class> ({'/'.join(deep)})")

        page.click(f".source-btn:has-text('{t3_all['label']}')")
        settle()
        check(crumbs() == [t3_all["label"]] and "No sub-folders" not in page.text_content("#folder-tree")
              and "test" not in tree(), "switching collection resets the tree to the new collection's top")
        page.screenshot(path=str(OUT / "switched.png"))

        page.reload()
        page.wait_for_selector("#grid .tile", timeout=30000)
        settle()
        check(f'"source_id":{t3_all["id"]}' in page.evaluate("decodeURIComponent(location.hash)")
              and t3_all["label"] in page.text_content("#active-filters"), "the collection survives a reload (URL hash)")

        page.fill("#search-text", "coral")
        page.press("#search-text", "Enter")
        settle()
        chip = page.text_content("#active-filters")
        check('search: “coral”' in chip and "sorted by relevance" in chip, "a search shows its own chip and sort")

        # Hold the next search back to see the loading state.
        held = []
        page.route("**/api/library/search", lambda route: held.append(route))
        page.click(".filter-chip.search-chip button")
        page.wait_for_function("document.querySelector('#result-count').textContent === 'Searching…'", timeout=5000)
        loading = page.evaluate("[document.querySelector('#grid').classList.contains('loading'),"
                                " document.querySelector('#result-count').textContent]")
        check(loading == [True, "Searching…"], f"a pending search dims the grid with Searching… ({loading})")
        page.unroute("**/api/library/search")
        for route in held:
            route.continue_()
        settle()

        # A class from the folder name.
        tag = page.locator("#facet-tags .facet", has_text="class:acbr")
        acbr = int(tag.first.locator(".n").text_content().replace(",", "")) if tag.count() else 0
        check(tag.count() == 1, "the class tag is offered as a facet")
        if tag.count():
            tag.first.click()
            settle()
            shown = int(page.text_content("#result-count").split()[0].replace(",", ""))
            check(acbr > 0 and shown == acbr, f"class:acbr filters the grid to its facet count ({shown} of {acbr})")

        library("#q=" + quote(json.dumps({"source_id": t3_all["id"], "tags": ["class:no-such-class"]})))
        empty = page.text_content("#grid-empty")
        check("class:no-such-class" in empty, "the empty state names its filters")
        page.click("#grid-empty button")
        settle()
        check(page.locator("#grid .tile").count() > 0, "Remove the last filter brings images back")

        # Library → curation → library.
        library("#q=" + quote(json.dumps({"source_id": t3_hf["id"]})))
        page.click("#grid .tile >> nth=0")
        page.wait_for_selector("#detail-curation:not(.hidden) a", timeout=15000)
        links = page.eval_on_selector_all("#detail-curation a", "els => els.map(e => [e.textContent, e.href])")
        check(len(links) >= 1 and all(t.startswith("Open in curation") for t, _ in links),
              f"the image links to {len(links)} curation project(s)")
        page.screenshot(path=str(OUT / "detail.png"))
        item = int(links[0][1].split("item=")[1].split("&")[0])
        page.goto(links[0][1])
        page.wait_for_function(f"typeof state !== 'undefined' && state.selected && state.selected.id === {item}", timeout=60000)
        check(True, "Open in curation lands on that patch")
        page.wait_for_selector("#library-links:not(.d-none) a", timeout=15000)
        back = page.get_attribute("#library-links a", "href")
        check("asset=" in back, "the patch links back to the library")
        name = page.evaluate("state.selected.file_name")
        page.goto(back if back.startswith("http") else f"{BASE}{back}")
        page.wait_for_function(f"document.querySelector('#detail-name').textContent === {json.dumps(name)}", timeout=30000)
        check(True, f"View in library opens that image's detail ({name})")
        page.screenshot(path=str(OUT / "curation.png"))

        check(not errors, f"no page errors ({errors[:3]})")
        browser.close()
    print("ok" if not failures else f"{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
