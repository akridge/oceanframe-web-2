"""No native browser dialogs: every confirm/prompt/alert is an OF.ui sheet or toast."""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
NATIVE = re.compile(r"(?<![\w.])(?:window\.)?(confirm|prompt|alert)\s*\(")


def test_no_native_dialogs_in_first_party_scripts():
    offenders = []
    for path in STATIC.rglob("*.js"):
        if "vendor" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if NATIVE.search(line):
                offenders.append(f"{path.relative_to(STATIC)}:{number}: {stripped[:100]}")
    assert not offenders, "\n".join(offenders)


def test_discard_guard_is_awaited_everywhere():
    for name in ("curation/curation.js", "curation/editor.js", "curation/collab.js", "curation/hub.js"):
        source = (STATIC / name).read_text(encoding="utf-8")
        assert "async function confirmDiscardPending" in source or name != "curation/editor.js"
        bare = re.findall(r"(?<!await )(?<!function )confirmDiscardPending\(\)", source)
        bare = [b for b in bare if "typeof confirmDiscardPending" not in b]
        assert not bare, f"{name}: confirmDiscardPending() called without await"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_popovers_keep_their_keys_and_prompts_take_a_type():
    ui = _read("ui/ui.js")
    assert "panel.addEventListener('keydown', onPanelKey)" in ui and "panel.removeEventListener('keydown', onPanelKey)" in ui
    assert "ui.prompt = ({ label, value, validate, type," in ui and "field.type = input.type || 'text'" in ui
    assert 'type: "password"' in _read("auth/users.js")


def test_switch_project_writes_the_picker_and_the_guard_is_shared():
    curation = _read("curation/curation.js")
    body = curation.split("async function switchProject", 1)[1].split("byId('add-taxonomy')", 1)[0]
    assert "byId('project-select').value = projectId" in body
    assert "let discardInflight" in _read("curation/editor.js")


def test_palette_gates_account_sign_out_commit_and_bare_pages():
    ui = _read("ui/ui.js")
    assert "of-bare" in ui and "signedInUser" in ui and "'/logout'" in ui
    assert "ui.toast(" in ui.split("function runResult", 1)[1].split("let debounce", 1)[0]
    assert "commit-pill').classList.contains('d-none')" in _read("curation/palette.js")


def test_no_hard_coded_light_colours_left_in_the_curation_page():
    assert "#dce5e3" not in _read("curation/curation.js")
    assert "#fff" not in (STATIC / "curation.css").read_text(encoding="utf-8").lower()
    components = _read("ui/components.css")
    assert "outline: 2px solid transparent" in components and "forced-colors" in components
    assert ".is-invalid, input.is-error" in components


def test_view_tabs_and_crop_grid_states():
    curation = _read("curation/curation.js")
    assert "setAttribute('aria-current', 'true')" in curation
    assert "Crops could not be loaded" in curation
    assert "${item.source_missing ? '' : `<img" not in curation
