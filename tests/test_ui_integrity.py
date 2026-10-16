"""Every page renders on the shared shell and every id its scripts read exists exactly once."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_curation import client  # noqa: F401  (login is off: everything runs as the local admin)

STATIC = Path(__file__).resolve().parents[1] / "static"
APPS = ["Library", "Collections", "Curation", "Frame Analyser"]

# (path, script files whose id lookups must resolve on that page, app key that is current)
PAGES: list[tuple[str, list[str], str | None]] = [
    ("/account", ["auth/account.js", "auth/menu.js", "ui/ui.js"], "account"),
    ("/users", ["auth/users.js", "auth/menu.js", "ui/ui.js"], "users"),
    ("/", ["library/app.js", "library/ui.js", "library/api.js", "library/state.js", "library/grid.js", "library/filters.js", "library/detail.js", "library/collection-info.js", "library/datasets.js", "library/jobs.js", "auth/session.js", "auth/menu.js", "ui/ui.js"], "library"),
    ("/collections", ["library/admin.js", "library/ui.js", "library/api.js", "library/jobs.js", "library/models.js", "auth/session.js", "auth/menu.js", "ui/ui.js"], "collections"),
    ("/collections/new", ["library/collection-form.js", "library/ui.js", "library/api.js", "library/jobs.js", "library/models.js", "auth/session.js", "auth/menu.js", "ui/ui.js"], "collections"),
    ("/frames", ["app.js", "modules/upload.js", "modules/analysis.js", "modules/filtering.js", "modules/charts.js", "modules/presets.js", "modules/export.js", "modules/frame-browser.js", "modules/lightbox.js", "modules/batch.js", "modules/state.js", "modules/dom.js", "modules/utils.js", "auth/session.js", "auth/menu.js", "ui/ui.js"], "frames"),
    ("/curation", ["curation/curation.js", "curation/hub.js", "curation/collab.js", "curation/editor.js", "curation/palette.js", "auth/menu.js", "ui/ui.js"], "curation"),
]

# Ids that scripts create themselves at runtime (never in the server-rendered HTML).
# curation/hub.js: #hub-labels is written into #hub-check after a dataset check;
# #evaluation-hub-note is created after #evaluation-run-form the first time it is needed.
CREATED_AT_RUNTIME: set[str] = {"hub-labels", "evaluation-hub-note"}

# Ids that a shared helper looks up behind a null guard on pages that never call it.
# library/ui.js renderStatusChips() reads the status chips and returns early when they are absent;
# the collection form imports ui.js but never calls it (and never had the chips).
GUARDED_ON_PAGE: dict[str, set[str]] = {"/collections/new": {"status-assets", "status-embedder"}}

LOOKUPS = [
    re.compile(r"""byId\(\s*['"]([A-Za-z0-9_-]+)['"]"""),
    re.compile(r"""querySelector(?:All)?\(\s*['"]#([A-Za-z0-9_-]+)['"]"""),
    re.compile(r"""\$\(\s*['"]#([A-Za-z0-9_-]+)['"]"""),
]


# Files whose $ is the id => document.getElementById(id) helper, so $('foo') is an id lookup.
PLAIN_DOLLAR = re.compile(r"""\$\(\s*['"]([A-Za-z0-9_-]+)['"]\s*\)""")
PLAIN_DOLLAR_FILES = ("library/", "modules/")


def render(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.text


def script_ids(files: list[str]) -> set[str]:
    found: set[str] = set()
    for name in files:
        source = (STATIC / name).read_text(encoding="utf-8")
        for pattern in LOOKUPS:
            found |= set(pattern.findall(source))
        if name.startswith(PLAIN_DOLLAR_FILES) or name == "app.js":
            found |= set(PLAIN_DOLLAR.findall(source))
    return found


@pytest.mark.parametrize("path,scripts,active", PAGES)
def test_page_is_on_the_shell(client, path, scripts, active):
    html = render(client, path)
    head = html.split("</head>", 1)[0]
    theme_at = head.find("localStorage.getItem('of-theme')")
    first_css = head.find('rel="stylesheet"')
    assert 0 <= theme_at < first_css, "theme must be set before the first stylesheet"
    assert re.search(r'/static/ui/tokens\.css\?v=[^"]+', head)
    for asset in re.findall(r'(?:href|src)="(/static/(?!vendor/)[^"]+\.(?:css|js)[^"]*)"', html):
        assert "?v=" in asset, asset
    rail = re.search(r'<nav[^>]*id="rail".*?</nav>', html, re.S)
    assert rail, "rail missing"
    labels = re.findall(r'class="rail-label">([^<]+)<', rail.group(0))
    assert labels[:4] == APPS
    if active in ("library", "collections", "curation", "frames"):
        current = re.search(r'<a[^>]*aria-current="page"[^>]*>.*?class="rail-label">([^<]+)<', rail.group(0), re.S)
        assert current and current.group(1) == APPS[["library", "collections", "curation", "frames"].index(active)]


@pytest.mark.parametrize("path,scripts,active", PAGES)
def test_ids_are_unique_and_every_script_lookup_resolves(client, path, scripts, active):
    html = render(client, path)
    ids = re.findall(r'(?<![\w-])id="([^"]+)"', html)
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"duplicate ids on {path}: {duplicates}"
    missing = sorted(script_ids(scripts) - set(ids) - CREATED_AT_RUNTIME - GUARDED_ON_PAGE.get(path, set()))
    assert not missing, f"{path}: scripts read ids that are not on the page: {missing}"


@pytest.fixture
def auth_client(library, monkeypatch):
    """Login switched on, with a bootstrap admin (same pattern as tests/test_auth.py)."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("OCEANFRAME_AUTH", "on")
    monkeypatch.setenv("OCEANFRAME_ADMIN_USERNAME", "boss")
    monkeypatch.setenv("OCEANFRAME_ADMIN_PASSWORD", "admin-pass-1")
    from appdb.bootstrap import init_app_db

    init_app_db()
    import main

    return TestClient(main.app, base_url="http://testserver")


def test_login_page_is_bare_but_themed(auth_client):
    html = auth_client.get("/login").text
    assert 'id="rail"' not in html and "data-theme" in html and "optics-si-logo.png" in html
    ids = re.findall(r'(?<![\w-])id="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    missing = script_ids(["auth/login.js"]) - set(ids) - CREATED_AT_RUNTIME  # ui.js reads shell ids that a bare page omits
    assert not missing, missing


def test_curation_filters_live_in_one_popover(client):
    html = render(client, "/curation")
    popover = re.search(r'<div[^>]*id="filters-popover"[^>]*>(.*?)\n\s*</div>\s*<!-- /filters-popover -->', html, re.S)
    assert popover, "filters popover missing (close it with <!-- /filters-popover -->)"
    for control in ("filter-region", "filter-year", "filter-state", "filter-included", "filter-hide-missing", "filter-show-removed", "apply-filters"):
        assert f'id="{control}"' in popover.group(1), control
    assert 'id="filters-button"' in html and 'id="filter-chips"' in html and 'id="queue-toggle"' in html


def test_charts_follow_the_theme():
    source = (STATIC / "modules" / "charts.js").read_text(encoding="utf-8")
    assert "OF.ui.theme.token(" in source and "OF.ui.theme.onChange(" in source


def test_source_workspace_structure(client):
    html = render(client, "/curation")
    for element in ("stage-toolbar", "stage-nav", "stage-prev", "stage-next", "stage-position", "inspector-tabs",
                    "inspector-point", "inspector-image", "inspector-model", "pending-review"):
        assert f'id="{element}"' in html, element
    wrap = html.split('id="source-image-wrap"', 1)[1].split("<!-- /source-image-wrap -->", 1)[0]
    assert 'id="stage-toolbar"' in wrap and 'id="stage-nav"' in wrap, "overlays belong inside the image wrap"
    script = (STATIC / "curation" / "curation.js").read_text(encoding="utf-8")
    assert "ResizeObserver" in script and "#stage-toolbar, #stage-nav, #pending-tray" in script


def test_curation_views_use_shared_components(client):
    css = (STATIC / "curation.css").read_text(encoding="utf-8")
    assert "/* label palette */" in css, "the palette section marker is missing"
    themed_part = css.split("/* label palette */")[0]
    assert not re.findall(r"#[0-9a-fA-F]{6}\b", re.sub(r"/\*.*?\*/", "", themed_part, flags=re.S)), \
        "curation.css uses hex colours outside the label palette section"
    assert css.rstrip().endswith("}") and "--heat-" in css.split("/* label palette */")[1]
    script = (STATIC / "curation" / "curation.js").read_text(encoding="utf-8")
    assert "empty-state" in script and "skeleton" in script and "Not in bucket" in script
    assert "var(--ok)" in script and "SPLIT_COLORS = { train: 'var(--accent)'" in script
    html = (STATIC.parent / "templates" / "curation.html").read_text(encoding="utf-8")
    assert 'class="selection-bar' in html
    page = render(client, "/curation")
    for dialog in ("hub-dialog", "hub-registry-dialog", "members-dialog", "commit-dialog", "releases-dialog"):
        assert re.search(rf'<dialog id="{dialog}" class="sheet\b', page), dialog


def test_crop_tiles_and_back_targets():
    script = (STATIC / "curation" / "curation.js").read_text(encoding="utf-8")
    assert "data-open-crop-source" in script and "event.stopPropagation()" in script and "Open source image" in script
    assert "compare: 'compare'" in script and "makeBackTarget('compare')" in script and "makeBackTarget('matrix')" in script
    assert "makeBackTarget('versions')" in (STATIC / "curation" / "collab.js").read_text(encoding="utf-8")


def test_pages_register_palette_providers():
    curation = "".join((STATIC / "curation" / f).read_text(encoding="utf-8") for f in ("curation.js", "collab.js", "editor.js", "palette.js") if (STATIC / "curation" / f).exists())
    assert curation.count("palette.register(") >= 3
    assert "image_search=" in curation
    library = "".join(p.read_text(encoding="utf-8") for p in (STATIC / "library").glob("*.js"))
    assert "palette.register(" in library


def test_responsive_rules_exist():
    for name in ("ui/shell.css", "curation.css", "library.css", "frames.css"):
        css = (STATIC / name).read_text(encoding="utf-8")
        assert "@media (max-width: 900px)" in css or "@media (max-width:900px)" in css, name
        assert "@media (max-width: 640px)" in css or "@media (max-width:640px)" in css, name


def test_floating_bars_clear_the_tab_bar_and_touch_shows_open_source():
    shell = (STATIC / "ui" / "shell.css").read_text(encoding="utf-8")
    curation = (STATIC / "curation.css").read_text(encoding="utf-8")
    assert "--tabbar-h" in shell and "safe-area-inset-bottom" in shell
    assert "var(--tabbar-h" in curation
    assert "@media (hover: none)" in curation and "crop-open-source" in curation.split("@media (hover: none)", 1)[1]


def test_pill_is_pinned_to_the_viewport_when_the_stage_is_under_the_queue():
    css = (STATIC / "curation.css").read_text(encoding="utf-8")
    block = css.split("@media (max-width: 900px) {", 1)[1].split("\n}\n", 1)[0]
    assert ".pending-tray { position: fixed" in block
