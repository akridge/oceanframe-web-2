# UX Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every OceanFrame page runs on one dark-pro shell (icon rail, top bar, ⌘K palette), one token-based design system with dark and light themes, and native-feeling sheets and toasts in place of browser dialogs. No feature is lost and every JS hook still works.

**Architecture:**
- **Templates:** a Jinja `base.html` provides the shell. Every page extends it and fills `topbar`, `content`, `page_css` and `page_scripts`.
- **CSS:** four files under `static/ui/` (tokens, base, components, shell) come first. Legacy page CSS (`app.css`, `library.css`, `curation.css`, `auth.css`) is migrated onto the tokens. Legacy variable names are aliased so pages theme correctly from the first task.
- **JS:** a classic script `static/ui/ui.js` exposes `window.OF.ui` (theme, toast, confirm, prompt, popover, palette) to both module and classic page scripts.

**Tech Stack:** FastAPI + Jinja2, vanilla CSS (custom properties, `color-mix`, `backdrop-filter`), vanilla JS (classic scripts plus the existing ES modules in library and auth), bootstrap-icons (vendored), pytest, Playwright (browser tour in Docker).

**Spec:** `docs/superpowers/specs/2026-10-13-ux-foundation-design.md`

## Global Constraints

- **Hooks are frozen:** no `id`, `data-*` attribute, `name`, or JS-read/toggled class is renamed or removed. Markup may move, be wrapped, and be restyled.
  - The toggled classes that keep their meaning: `d-none`, `hidden`, `active`, `selected`, `is-error`, `is-invalid`, `is-loading`, `loaded`, `busy`, `stuck`, `role-locked`, `required`, `lasso-hit`, `lasso-active`, `drag-over`, `text-danger`.
  - `d-none` and `hidden` mean `display: none !important`.
- **No native dialogs:** no native `confirm(`, `prompt(` or `alert(` in `static/**` outside `vendor/`, except the `beforeunload` guard. Enforced from Task 9 onward. Earlier tasks convert the dialogs on their own pages.
- **Theme before paint:** every page sets its theme with an inline `<head>` script placed before the first stylesheet.
- **Cache busting:** every first-party CSS/JS URL ends `?v={{ asset_v }}`.
- **Contrast** (WCAG):
  - `--text`, `--text-2` and `--text-3` on `--bg`/`--surface-1`: at least 4.5.
  - `--accent-text` on `--bg`: at least 4.5.
  - White on `--accent-fill`: at least 4.5.
  - `--accent` on `--bg`: at least 3.
  - Enforced by `tests/test_ui_tokens.py` for both themes.
- **Reduced motion:** all motion is 0 ms under `prefers-reduced-motion: reduce`.
- **No dependencies:** no new endpoints, no database changes, no CSS framework, no build step, no new Python or JS dependencies.
- **Tests:**
  - Run in Docker only, always with `--build`. TEST `<target>` means:

    ```bash
    MSYS_NO_PATHCONV=1 docker compose --env-file "$TMP/of-dummy.env" run --build --rm test sh -c "pip install -q --no-cache-dir -r requirements-dev.txt && python -m pytest -q <target>"
    ```

  - JS SYNTAX means:

    ```bash
    MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD/static:/s:ro" node:20-alpine sh -c 'find /s -name "*.js" -not -path "*/vendor/*" -exec node --check {} \; && echo SYNTAX-OK'
    ```

    The ES-module files in `static/library` and `static/auth` check fine with `node --check`. If one fails only because of `import` syntax, run that file with `node --input-type=module --check < file`.
- **Docker safety:** never `docker compose … down`, never `-v`, never touch `oceanframe-stack_*` volumes, `.env`, `.env copy` or `docs/*.png`. The one exception: Task 2 copies the OPTICS SI logo out of `docs/` into `static/img/` and leaves the original in place.
- **Commits:** end every commit with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never commit `.superpowers/`.
- **Curation files run one task at a time.** Tasks 6–10 all touch `curation.*`, so they run sequentially, with no overlapping implementer.
- **Page context survives the move to `base.html`.**
  - Each page carries its existing `<body>` class into `{% block body_class %}`: `auth-body`, `lib-body`, `curation-body`, and so on.
  - Each page carries its existing `<body>` attributes into `{% block body_attrs %}`. Curation has `data-create-role="{{ create_role }}"`, which `canDo('create')` reads.
  - Every `{{ … }}` expression in a template keeps rendering, including `source_id`, `next`, `signup_role`, `create_role` and `split_root`.
  - Audit step in Tasks 2–6: run `grep -n "{{" templates/<page>.html` before and after migrating, and confirm every expression from the old file is still present.
- **Legacy component rules don't override the new components.** Page stylesheets load after `components.css`, so each page task deletes the duplicate `.btn`, `.btn-sm`, `.btn-primary`, `.btn-outline-*`, `.form-control`, `.form-select`, `.form-label` and `.card` base rules from its page CSS. It keeps only page-specific modifiers, such as `.matrix-toolbar .btn`.
  - Check: `grep -nE "^\.(btn|btn-sm|btn-primary|form-control|form-select|form-label|card)\s*[,{]" static/app.css static/auth.css static/library.css static/curation.css` returns nothing once that page's task is done.
- **Integrity-test lookups** cover `byId('…')`, `$('#…')` and `querySelector('#…')`, but **not** `getElementById`. P3a deliberately uses `document.getElementById` for ids created at runtime. Never add fake elements to a template to satisfy the test.
- **Browser checks must not leave test data behind.**
  - **Writes and cleanup.** The P3a editor checks write to the Hawaii sample project. Each run ends with a cleanup step, through the same `/edits` API:
    - restore moved and resized points to the geometry recorded before the run;
    - restore points the run removed;
    - remove points the run added.
  - **Output files.** All browser-check output goes to `tests/browser/shots/`. `.gitignore` ignores everything under `tests/browser/` except `*.py` and `*.md`.
- **Unmigrated pages stay harmless during the branch.** The new `menu.js` returns early when `#rail-avatar` is absent. Pages not yet migrated keep loading `app.css` until their task runs, and Task 2 adds `tokens.css` to the `<head>` of every not-yet-migrated template, so the legacy variable aliases resolve.

## Review Focus

1. **A click on an on-canvas control** (Save in the floating pill, a toolbar button, image stepping) must never also pan the image, add a point or deselect. Covered by a Task 7 Playwright check.
2. **A theme switch while a page is open:** charts (Frame Analyser, curation split bars) and canvases re-read token colours. Nothing stays in the old theme until reload. Covered by a Task 5 check plus the Task 12 tour.
3. **Cancelling a discard sheet:** the user stays on the image with edits intact, and nothing navigated. Confirming discards exactly once. The `hashchange` and Back paths behave the same. Covered by a Task 9 Playwright check.
4. **The stage changes size without a window resize** (queue collapse, inspector tab change): markers stay on their points. Covered by a Task 7 check.
5. **Login switched off** (no user): the avatar menu still offers Theme. Account, Users and Sign out are absent, and nothing errors. Covered by Task 2 (the rendered page with login off, the default in tests) and the Task 12 tour.

---

### Task 1: Design tokens, base and components CSS

**Files:**
- Create: `static/ui/tokens.css`, `static/ui/base.css`, `static/ui/components.css`
- Test: `tests/test_ui_tokens.py`

**Interfaces — Produces:** the token names below, the legacy aliases, the component classes (`.btn` family, `.segmented`, `.tabs`, `.chip`, `.chip-active`, `.badge-*`, `.tile`, `.panel`, `.table`, `.popover`, `.sheet`, `.toast-host`/`.toast`, `.skeleton`, `.empty-state`, `.progress`, `.spinner`, `.switch`, `.kbd`), and the utilities `d-none`, `hidden`, `visually-hidden`, `text-danger`, `muted`.

- [ ] **Step 1: Write the failing test** in `tests/test_ui_tokens.py`

```python
"""Design tokens: both themes exist and meet WCAG contrast."""
from __future__ import annotations

import re
from pathlib import Path

TOKENS = Path(__file__).resolve().parents[1] / "static" / "ui" / "tokens.css"


def _theme(name: str) -> dict[str, str]:
    css = TOKENS.read_text(encoding="utf-8")
    block = re.search(r':root\[data-theme="%s"\]\s*\{(.*?)\n\}' % name, css, re.S)
    assert block, f"no {name} theme block"
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", block.group(1)))


def _lum(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_both_themes_define_the_same_tokens():
    dark, light = _theme("dark"), _theme("light")
    assert set(dark) == set(light)
    for name in ("--bg", "--bg-rail", "--surface-1", "--surface-2", "--surface-3", "--line", "--text", "--text-2",
                 "--text-3", "--accent", "--accent-fill", "--accent-text", "--ok", "--warn", "--danger", "--danger-fill"):
        assert name in dark, name


def test_text_and_accents_meet_wcag_in_both_themes():
    for theme in ("dark", "light"):
        t = _theme(theme)
        for fg in ("--text", "--text-2", "--text-3"):
            for bg in ("--bg", "--surface-1"):
                assert _ratio(t[fg], t[bg]) >= 4.5, (theme, fg, bg, round(_ratio(t[fg], t[bg]), 2))
        assert _ratio(t["--accent-text"], t["--bg"]) >= 4.5, theme
        assert _ratio("#ffffff", t["--accent-fill"]) >= 4.5, theme
        assert _ratio("#ffffff", t["--danger-fill"]) >= 4.5, theme
        assert _ratio(t["--accent"], t["--bg"]) >= 3.0, theme
        for status in ("--ok", "--warn", "--danger"):
            assert _ratio(t[status], t["--bg"]) >= 3.0, (theme, status)


def test_reduced_motion_zeroes_the_durations():
    css = TOKENS.read_text(encoding="utf-8")
    block = re.search(r"prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\}", css, re.S)
    assert block and "--t: 0ms" in block.group(1) and "--t-fast: 0ms" in block.group(1)


def test_legacy_variables_are_aliased():
    css = TOKENS.read_text(encoding="utf-8")
    for legacy in ("--surface:", "--border:", "--primary:", "--primary-dark:", "--primary-light:", "--text-muted:",
                   "--text-light:", "--success:", "--error:", "--warning:", "--radius:", "--shadow:"):
        assert legacy in css, legacy
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_ui_tokens.py`
Expected: FAIL, because `tokens.css` doesn't exist yet.

- [ ] **Step 3: Create `static/ui/tokens.css`**

```css
/* OceanFrame design tokens. Dark is the default; light uses the same names.
   Set on <html data-theme="dark|light"> by the inline script in base.html. */

:root {
  --font: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable", "Segoe UI", Inter, system-ui, sans-serif;
  --font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --fs-11: 11px; --fs-12: 12px; --fs-13: 13px; --fs-15: 15px; --fs-17: 17px; --fs-22: 22px; --fs-28: 28px;
  --fw-regular: 400; --fw-semibold: 590; --fw-bold: 700;
  --sp-1: 2px; --sp-2: 4px; --sp-3: 6px; --sp-4: 8px; --sp-5: 12px; --sp-6: 16px; --sp-7: 20px; --sp-8: 24px; --sp-9: 32px; --sp-10: 48px;
  --r-control: 7px; --r-tile: 9px; --r-panel: 12px; --r-sheet: 14px; --r-pill: 999px;
  --ease: cubic-bezier(.2, .8, .2, 1);
  --t-fast: 150ms; --t: 200ms; --t-slow: 250ms;
  --rail-w: 64px; --topbar-h: 44px;
}

:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #1c1c1e;
  --bg-rail: #111113;
  --surface-1: #2c2c2e;
  --surface-2: #3a3a3c;
  --surface-3: #48484a;
  --line: #38383a;
  --text: #f5f5f7;
  --text-2: #c7c7cc;
  --text-3: #98989f;
  --accent: #0a84ff;
  --accent-fill: #0071e3;
  --accent-text: #64b5ff;
  --ok: #30d158;
  --warn: #ff9f0a;
  --danger: #ff453a;
  --danger-fill: #d70015;
}
:root[data-theme="dark"] {
  --scrim: rgba(0, 0, 0, .5);
  --shadow-pop: 0 10px 30px rgba(0, 0, 0, .45), 0 0 0 1px rgba(255, 255, 255, .06);
  --shadow-sheet: 0 24px 60px rgba(0, 0, 0, .55), 0 0 0 1px rgba(255, 255, 255, .08);
}

:root[data-theme="light"] {
  color-scheme: light;
  --bg: #f5f5f7;
  --bg-rail: #e8e8ed;
  --surface-1: #ffffff;
  --surface-2: #e8e8ed;
  --surface-3: #d2d2d7;
  --line: #d2d2d7;
  --text: #1d1d1f;
  --text-2: #424245;
  --text-3: #6e6e73;
  --accent: #0071e3;
  --accent-fill: #0071e3;
  --accent-text: #0066cc;
  --ok: #1f8a3a;
  --warn: #b25e00;
  --danger: #d70015;
  --danger-fill: #d70015;
}
:root[data-theme="light"] {
  --scrim: rgba(0, 0, 0, .3);
  --shadow-pop: 0 10px 30px rgba(0, 0, 0, .12), 0 0 0 1px rgba(0, 0, 0, .06);
  --shadow-sheet: 0 24px 60px rgba(0, 0, 0, .18), 0 0 0 1px rgba(0, 0, 0, .08);
}

:root {
  --focus: color-mix(in srgb, var(--accent) 45%, transparent);
  --accent-tint: color-mix(in srgb, var(--accent) 18%, transparent);
  --ok-tint: color-mix(in srgb, var(--ok) 18%, transparent);
  --warn-tint: color-mix(in srgb, var(--warn) 18%, transparent);
  --danger-tint: color-mix(in srgb, var(--danger) 18%, transparent);
  --glass: color-mix(in srgb, var(--bg) 72%, transparent);
  --glass-strong: color-mix(in srgb, var(--surface-1) 88%, transparent);

  /* Legacy names used by app.css, library.css and curation.css, mapped onto the tokens. */
  --surface: var(--surface-1);
  --border: var(--line);
  --border-focus: var(--accent);
  --primary: var(--accent-fill);
  --primary-dark: color-mix(in srgb, var(--accent-fill) 80%, black);
  --primary-light: var(--accent-tint);
  --text-muted: var(--text-3);
  --text-light: var(--text-3);
  --success: var(--ok);
  --success-bg: var(--ok-tint);
  --warning: var(--warn);
  --warning-bg: var(--warn-tint);
  --error: var(--danger);
  --error-bg: var(--danger-tint);
  --shadow-sm: none;
  --shadow: 0 0 0 1px var(--line);
  --shadow-md: var(--shadow-pop);
  --shadow-lg: var(--shadow-sheet);
  --radius: var(--r-tile);
  --radius-sm: var(--r-control);
  --radius-lg: var(--r-panel);
}

@media (prefers-reduced-motion: reduce) {
  :root { --t-fast: 0ms; --t: 0ms; --t-slow: 0ms; }
}
```

The test regex reads only `#xxxxxx` values from the first `:root[data-theme=…]` block of each theme. That is why the shadows sit in a second block of the same selector.

- [ ] **Step 4: Create `static/ui/base.css`**

```css
/* Reset, type, focus and utilities. Loaded after tokens.css. */
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
html, body { margin: 0; height: 100%; }
body {
  font-family: var(--font); font-size: var(--fs-13); line-height: 1.45; color: var(--text); background: var(--bg);
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
}
h1, h2, h3, h4 { margin: 0 0 var(--sp-4); line-height: 1.2; font-weight: var(--fw-semibold); letter-spacing: -.01em; }
h1 { font-size: var(--fs-28); } h2 { font-size: var(--fs-22); } h3 { font-size: var(--fs-17); } h4 { font-size: var(--fs-15); }
p { margin: 0 0 var(--sp-4); }
a { color: var(--accent-text); text-decoration: none; }
a:hover { text-decoration: underline; }
img, svg, video, canvas { max-width: 100%; }
button, input, select, textarea { font: inherit; color: inherit; }
code, kbd, pre, samp { font-family: var(--font-mono); font-size: .92em; }
table { border-collapse: collapse; }
::selection { background: var(--accent-tint); }
* { scrollbar-width: thin; scrollbar-color: var(--surface-3) transparent; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 999px; border: 3px solid transparent; background-clip: padding-box; }
:focus { outline: none; }
:focus-visible { outline: none; box-shadow: 0 0 0 3px var(--focus); border-radius: var(--r-control); }
input[type="checkbox"], input[type="radio"], input[type="range"], progress { accent-color: var(--accent); }

/* Utilities that scripts toggle: keep these meanings. */
.d-none, [hidden], .hidden { display: none !important; }
.visually-hidden { position: absolute !important; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.text-danger { color: var(--danger) !important; }
.text-success { color: var(--ok) !important; }
.muted, .text-muted, .text-secondary { color: var(--text-3) !important; }
.small, .text-sm { font-size: var(--fs-12); }
.tabular { font-variant-numeric: tabular-nums; }
```

- [ ] **Step 5: Create `static/ui/components.css`**

```css
/* Shared components. Bootstrap-like class names are restyled here; no Bootstrap is loaded. */

/* Buttons */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: var(--sp-3);
  min-height: 30px; padding: 0 var(--sp-5); border: 0; border-radius: var(--r-control);
  background: var(--surface-2); color: var(--text); font-weight: var(--fw-semibold); font-size: var(--fs-13);
  cursor: pointer; white-space: nowrap; text-decoration: none; user-select: none;
  transition: background var(--t-fast) var(--ease), transform var(--t-fast) var(--ease), opacity var(--t-fast) var(--ease);
}
.btn:hover { background: var(--surface-3); text-decoration: none; }
.btn:active { transform: scale(.98); }
.btn:disabled, .btn.disabled, .btn[aria-disabled="true"] { opacity: .4; pointer-events: none; }
.btn-sm { min-height: 26px; padding: 0 var(--sp-4); font-size: var(--fs-12); }
.btn-lg { min-height: 38px; padding: 0 var(--sp-6); font-size: var(--fs-15); }
.btn-primary { background: var(--accent-fill); color: #fff; }
.btn-primary:hover { background: color-mix(in srgb, var(--accent-fill) 88%, white); }
.btn-ghost, .btn-link { background: transparent; color: var(--accent-text); }
.btn-ghost:hover, .btn-link:hover { background: var(--accent-tint); }
.btn-danger { background: var(--danger-fill); color: #fff; }
.btn-outline-danger { background: transparent; color: var(--danger); box-shadow: inset 0 0 0 1px var(--danger-tint); }
.btn-outline-danger:hover { background: var(--danger-tint); }
.btn-outline-success { background: transparent; color: var(--ok); box-shadow: inset 0 0 0 1px var(--ok-tint); }
.btn-outline-success:hover { background: var(--ok-tint); }
.btn-outline-primary { background: transparent; color: var(--accent-text); box-shadow: inset 0 0 0 1px var(--accent-tint); }
.btn-outline-primary:hover { background: var(--accent-tint); }
.btn-outline-secondary, .btn-secondary, .btn-light { background: var(--surface-2); color: var(--text); }
.btn-icon { width: 30px; padding: 0; }
.btn.busy { position: relative; color: transparent !important; pointer-events: none; }
.btn.busy::after { content: ""; position: absolute; width: 14px; height: 14px; border-radius: 50%; border: 2px solid currentColor; border-color: var(--text) transparent var(--text) transparent; animation: of-spin .8s linear infinite; }

/* Segmented control and tabs */
.segmented { display: inline-flex; gap: 2px; padding: 2px; border-radius: 9px; background: var(--surface-2); }
.segmented > button, .segmented > a, .segmented > label {
  border: 0; background: transparent; color: var(--text-2); padding: 4px 12px; border-radius: var(--r-control);
  font-weight: var(--fw-semibold); font-size: var(--fs-12); cursor: pointer; transition: background var(--t-fast) var(--ease), color var(--t-fast) var(--ease);
}
.segmented > .active, .segmented > [aria-pressed="true"], .segmented > [aria-selected="true"] { background: var(--surface-1); color: var(--text); box-shadow: 0 1px 2px rgba(0,0,0,.25); }
.tabs { display: flex; gap: 2px; align-items: center; overflow-x: auto; scrollbar-width: none; }
.tabs > button, .tabs > a {
  border: 0; background: transparent; color: var(--text-2); padding: 5px 10px; border-radius: var(--r-control);
  font-weight: var(--fw-semibold); font-size: var(--fs-13); cursor: pointer; white-space: nowrap; text-decoration: none;
}
.tabs > button:hover, .tabs > a:hover { color: var(--text); background: var(--surface-1); text-decoration: none; }
.tabs > .active, .tabs > [aria-selected="true"], .tabs > [aria-current="page"] { background: var(--surface-2); color: var(--text); }

/* Inputs */
.form-control, .form-select, input[type="text"], input[type="search"], input[type="number"], input[type="email"],
input[type="password"], input[type="url"], input[type="date"], select, textarea {
  min-height: 30px; padding: 4px 10px; border-radius: var(--r-control); border: 1px solid var(--line);
  background: var(--surface-1); color: var(--text); font-size: var(--fs-13);
  transition: border-color var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease);
}
.form-select, select { padding-right: 28px; appearance: none; background-image: linear-gradient(45deg, transparent 50%, var(--text-3) 50%), linear-gradient(135deg, var(--text-3) 50%, transparent 50%); background-position: calc(100% - 14px) 50%, calc(100% - 9px) 50%; background-size: 5px 5px; background-repeat: no-repeat; }
.form-control:focus, .form-select:focus, input:focus, select:focus, textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--focus); outline: none; }
.form-control-sm, .form-select-sm { min-height: 26px; font-size: var(--fs-12); padding-top: 2px; padding-bottom: 2px; }
.form-control::placeholder, input::placeholder, textarea::placeholder { color: var(--text-3); }
.is-invalid, .is-error { border-color: var(--danger) !important; box-shadow: 0 0 0 3px var(--danger-tint) !important; }
.form-label { display: block; margin-bottom: var(--sp-2); color: var(--text-2); font-size: var(--fs-12); font-weight: var(--fw-semibold); }
.form-check { display: inline-flex; align-items: center; gap: var(--sp-3); }
.switch { appearance: none; width: 34px; height: 20px; border-radius: 999px; background: var(--surface-3); position: relative; cursor: pointer; transition: background var(--t) var(--ease); }
.switch::after { content: ""; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: transform var(--t) var(--ease); }
.switch:checked { background: var(--ok); } .switch:checked::after { transform: translateX(14px); }

/* Chips and badges */
.chip { display: inline-flex; align-items: center; gap: var(--sp-2); min-height: 24px; padding: 0 10px; border-radius: var(--r-pill); border: 0; background: var(--surface-2); color: var(--text); font-size: var(--fs-12); cursor: pointer; }
.chip-active { background: var(--accent-tint); color: var(--accent-text); }
.chip .chip-x { opacity: .7; } .chip:hover .chip-x { opacity: 1; }
.badge, .badge-ok, .badge-warn, .badge-danger, .badge-neutral { display: inline-flex; align-items: center; gap: 3px; padding: 1px 7px; border-radius: 6px; font-size: var(--fs-11); font-weight: var(--fw-semibold); }
.badge-ok { background: var(--ok-tint); color: var(--ok); }
.badge-warn { background: var(--warn-tint); color: var(--warn); }
.badge-danger { background: var(--danger-tint); color: var(--danger); }
.badge, .badge-neutral { background: var(--surface-2); color: var(--text-2); }
.kbd, kbd { display: inline-block; padding: 0 6px; border-radius: 5px; background: var(--surface-2); color: var(--text-3); font-size: var(--fs-11); font-family: var(--font); line-height: 18px; }

/* Panels, tiles, tables */
.panel { background: var(--surface-1); border-radius: var(--r-panel); box-shadow: 0 0 0 1px var(--line); padding: var(--sp-6); }
.panel-title { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-4); margin-bottom: var(--sp-5); }
.panel-title h2, .panel-title h3, .panel-title strong { font-size: var(--fs-15); margin: 0; }
.panel-title small { display: block; color: var(--text-3); font-size: var(--fs-12); }
.tile { position: relative; border-radius: var(--r-tile); overflow: hidden; background: var(--surface-1); box-shadow: 0 0 0 1px var(--line); transition: transform var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease); cursor: pointer; }
.tile:hover { transform: translateY(-1px); box-shadow: 0 0 0 1px var(--surface-3), var(--shadow-pop); }
.tile.selected { box-shadow: 0 0 0 2px var(--accent); }
.tile img { display: block; width: 100%; height: auto; }
.tile .tile-label { position: absolute; left: 6px; bottom: 6px; padding: 1px 7px; border-radius: var(--r-pill); background: rgba(0,0,0,.6); color: #fff; font-size: var(--fs-11); backdrop-filter: blur(6px); }
.table { width: 100%; font-size: var(--fs-13); font-variant-numeric: tabular-nums; }
.table th { position: sticky; top: 0; background: var(--surface-1); color: var(--text-3); font-weight: var(--fw-semibold); font-size: var(--fs-12); text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); }
.table td { padding: 7px 10px; border-bottom: 1px solid var(--line); }
.table tbody tr:hover td { background: color-mix(in srgb, var(--surface-2) 50%, transparent); }

/* Popover, sheet, toast */
.popover { position: fixed; z-index: 60; min-width: 200px; max-width: min(420px, calc(100vw - 16px)); max-height: min(70vh, 560px); overflow: auto; padding: 6px; border-radius: var(--r-panel); background: var(--glass-strong); backdrop-filter: saturate(180%) blur(20px); box-shadow: var(--shadow-pop); animation: of-pop var(--t) var(--ease); }
.popover .menu-item, .popover [role="menuitem"] { display: flex; align-items: center; gap: var(--sp-4); width: 100%; padding: 6px 10px; border: 0; border-radius: var(--r-control); background: transparent; color: var(--text); text-align: left; cursor: pointer; font-size: var(--fs-13); text-decoration: none; }
.popover .menu-item:hover, .popover [role="menuitem"]:hover, .popover .menu-item:focus-visible { background: var(--accent-fill); color: #fff; box-shadow: none; }
.popover .menu-sep { height: 1px; margin: 4px 6px; background: var(--line); }
.popover .menu-label { padding: 4px 10px; color: var(--text-3); font-size: var(--fs-11); text-transform: uppercase; letter-spacing: .05em; }
dialog.sheet { width: min(420px, calc(100vw - 32px)); padding: var(--sp-7); border: 0; border-radius: var(--r-sheet); background: var(--surface-1); color: var(--text); box-shadow: var(--shadow-sheet); }
dialog.sheet[open] { animation: of-sheet var(--t-slow) var(--ease); }
dialog.sheet::backdrop { background: var(--scrim); backdrop-filter: blur(2px); }
.sheet-title { font-size: var(--fs-15); margin-bottom: var(--sp-3); }
.sheet-message { color: var(--text-2); margin-bottom: var(--sp-6); white-space: pre-line; }
.sheet .form-label { margin-bottom: var(--sp-5); } .sheet .form-control { width: 100%; margin-top: var(--sp-2); }
.sheet-error { color: var(--danger); font-size: var(--fs-12); margin-top: calc(-1 * var(--sp-3)); }
.sheet-actions { display: flex; justify-content: flex-end; gap: var(--sp-4); }
.toast-host { position: fixed; z-index: 80; left: 50%; bottom: 20px; transform: translateX(-50%); display: flex; flex-direction: column-reverse; gap: var(--sp-4); align-items: center; pointer-events: none; }
.toast { pointer-events: auto; display: flex; align-items: center; gap: var(--sp-4); max-width: min(560px, calc(100vw - 32px)); padding: 8px 8px 8px 14px; border-radius: var(--r-panel); background: var(--glass-strong); backdrop-filter: saturate(180%) blur(20px); box-shadow: var(--shadow-pop); opacity: 0; transform: translateY(8px) scale(.98); transition: opacity var(--t) var(--ease), transform var(--t) var(--ease); }
.toast.is-in { opacity: 1; transform: none; } .toast.is-out { opacity: 0; transform: translateY(8px); }
.toast::before { content: ""; flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--accent); }
.toast-ok::before { background: var(--ok); } .toast-warn::before { background: var(--warn); } .toast-danger::before { background: var(--danger); }
.toast-text { flex: 1; }
.toast-close { border: 0; background: transparent; color: var(--text-3); font-size: 18px; line-height: 1; padding: 2px 6px; cursor: pointer; border-radius: 6px; }

/* Loading, empty, progress */
.skeleton { border-radius: var(--r-tile); background: linear-gradient(90deg, var(--surface-1) 25%, var(--surface-2) 50%, var(--surface-1) 75%); background-size: 200% 100%; animation: of-shimmer 1.2s linear infinite; }
.empty-state { padding: var(--sp-9) var(--sp-6); text-align: center; color: var(--text-3); border: 1px dashed var(--line); border-radius: var(--r-panel); }
.empty-state strong, .empty-state h3 { display: block; color: var(--text); font-size: var(--fs-15); margin-bottom: var(--sp-2); }
.progress { height: 4px; border-radius: 999px; background: var(--surface-2); overflow: hidden; }
.progress > span, .progress > .progress-bar { display: block; height: 100%; background: var(--accent); border-radius: inherit; transition: width var(--t) var(--ease); }
.spinner { width: 16px; height: 16px; border-radius: 50%; border: 2px solid var(--surface-3); border-top-color: var(--accent); animation: of-spin .8s linear infinite; display: inline-block; }

@keyframes of-spin { to { transform: rotate(360deg); } }
@keyframes of-shimmer { from { background-position: 200% 0; } to { background-position: -200% 0; } }
@keyframes of-pop { from { opacity: 0; transform: translateY(-4px) scale(.98); } to { opacity: 1; transform: none; } }
@keyframes of-sheet { from { opacity: 0; transform: translateY(12px) scale(.98); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) {
  .skeleton, .spinner, .btn.busy::after { animation: none; }
  .popover, dialog.sheet[open] { animation: none; }
}
```

- [ ] **Step 6: Run** TEST `tests/test_ui_tokens.py`. Expected: PASS (4 tests). If a contrast check fails, adjust only that token's hex value, staying within the spec's palette family, and keep the spec table in step. Record the change in the commit message.

- [ ] **Step 7: Commit**

```bash
git add static/ui/tokens.css static/ui/base.css static/ui/components.css tests/test_ui_tokens.py
git commit -m "Design tokens (dark and light), base and component CSS with WCAG contrast tests"
```

---

### Task 2: `ui.js`, the base shell template, the user menu, and Account on the shell

**Files:**
- Create: `static/ui/ui.js`, `static/ui/shell.css`, `templates/base.html`, `static/img/optics-si-logo.png` (copied from `docs/optics_si_logo_v1 (1).png`)
- Modify: `main.py` (Jinja global `asset_v`), `static/auth/menu.js` (renders into the avatar popover, reveals `#rail-users` for admins), `static/app.css` (delete its `:root { … }` variable block, lines 5–~37, keeping only rules that don't define legacy variables; the aliases now live in `tokens.css`), `templates/account.html` (extends `base.html`)
- Test: `tests/test_ui_integrity.py` (new)

**Interfaces — Produces:**
- **Jinja variables set in child templates before `{% extends %}`:** `active` (`library|collections|curation|frames|account|users`), `bare` (true for login, no rail or top bar).
- **Blocks:** `title`, `page_css`, `topbar`, `content`, `page_scripts`, `body_class`.
- **The `window.OF.ui` API:**
  - `theme.get()` → `'dark'|'light'|'system'`;
  - `theme.set(choice)`;
  - `theme.effective()` → `'dark'|'light'`;
  - `theme.onChange(fn)` → unsubscribe;
  - `toast(message, {kind, action:{label,onClick}, timeout})` → `{close}`;
  - `confirm({title, message, confirmLabel, cancelLabel, danger})` → `Promise<boolean>`;
  - `prompt({title, message, label, value, confirmLabel, validate})` → `Promise<string|null>`;
  - `popover(anchor, panel)` → close function, or `null` when it toggled closed;
  - `closePopover()`;
  - `palette.register({group, items(query) → array|Promise<array>, filter=true})`, where an item is `{label, hint?, icon?, run()}`;
  - `palette.open()`.
- **Elements in `base.html`:** `#rail`, `#rail-users` (hidden), `#rail-avatar`, `#user-menu` (popover panel, hidden), `#of-toasts`, `#of-palette`, `#of-palette-input`, `#of-palette-results`, `.topbar`, `[data-palette-open]`.
- **Test helpers in `tests/test_ui_integrity.py`:** `PAGES` (a list of `(path, [script files], active app key)`) and `render(client, path)`. Later tasks append their page to `PAGES`.

- [ ] **Step 1: Write the failing test** in `tests/test_ui_integrity.py`

```python
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
]

# Ids that scripts create themselves at runtime (never in the server-rendered HTML).
CREATED_AT_RUNTIME: set[str] = set()

LOOKUPS = [
    re.compile(r"""byId\(\s*['"]([A-Za-z0-9_-]+)['"]"""),
    re.compile(r"""querySelector(?:All)?\(\s*['"]#([A-Za-z0-9_-]+)['"]"""),
    re.compile(r"""\$\(\s*['"]#([A-Za-z0-9_-]+)['"]"""),
]


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
    missing = sorted(script_ids(scripts) - set(ids) - CREATED_AT_RUNTIME)
    assert not missing, f"{path}: scripts read ids that are not on the page: {missing}"


def test_login_page_is_bare_but_themed(client, monkeypatch):
    html = render(client, "/login") if client.get("/login", follow_redirects=False).status_code == 200 else None
    if html is None:
        pytest.skip("login is off in this environment, so /login redirects")
    assert 'id="rail"' not in html and "data-theme" in html
```

The login test is filled in properly by Task 3, using an auth-enabled client.

- [ ] **Step 2: Run it to verify it fails**

Run: TEST `tests/test_ui_integrity.py`
Expected: FAIL. There's no rail, no `tokens.css`, and no `?v=`.

- [ ] **Step 3: `main.py` asset version.** After `templates = Jinja2Templates(directory=TEMPLATE_DIR)`, add:

```python
# Cache-busting for first-party CSS/JS: a deploy never pairs new HTML with stale assets.
templates.env.globals["asset_v"] = os.getenv("APP_VERSION") or str(int(time.time()))
```

Add `import time` and, if missing, `import os`.

- [ ] **Step 4: Create `templates/base.html`**

```jinja
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="color-scheme" content="dark light" />
  <title>{% block title %}OceanFrame{% endblock %}</title>
  <script>(function(){var c='system';try{c=localStorage.getItem('of-theme')||'system'}catch(e){}var l=!!(window.matchMedia&&matchMedia('(prefers-color-scheme: light)').matches);document.documentElement.setAttribute('data-theme',c==='system'?(l?'light':'dark'):c)})()</script>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath d='M3 21q5-7 10 0t10-7' stroke='%230a84ff' stroke-width='3' fill='none' stroke-linecap='round'/%3E%3Ccircle cx='16' cy='11' r='4' stroke='%230a84ff' stroke-width='2.5' fill='none'/%3E%3C/svg%3E" />
  <link rel="stylesheet" href="/static/vendor/bootstrap-icons/bootstrap-icons.min.css" />
  <link rel="stylesheet" href="/static/ui/tokens.css?v={{ asset_v }}" />
  <link rel="stylesheet" href="/static/ui/base.css?v={{ asset_v }}" />
  <link rel="stylesheet" href="/static/ui/components.css?v={{ asset_v }}" />
  <link rel="stylesheet" href="/static/ui/shell.css?v={{ asset_v }}" />
  {% block page_css %}{% endblock %}
  <script src="/static/ui/ui.js?v={{ asset_v }}"></script>
</head>
<body class="of-app{% if bare %} of-bare{% endif %} {% block body_class %}{% endblock %}" {% block body_attrs %}{% endblock %}>
{% if not bare %}
  <nav class="rail" id="rail" aria-label="Apps">
    <a class="rail-logo" href="/" aria-label="OceanFrame home">
      <svg viewBox="0 0 32 32" fill="none" aria-hidden="true"><path d="M4 20 Q8 14 12 18 Q16 22 20 16 Q24 10 28 14" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/><circle cx="16" cy="12" r="4" stroke="currentColor" stroke-width="2"/><line x1="16" y1="16" x2="16" y2="22" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
    </a>
    {% for href, key, label, icon in [("/", "library", "Library", "bi-images"), ("/collections", "collections", "Collections", "bi-collection"), ("/curation", "curation", "Curation", "bi-bullseye"), ("/frames", "frames", "Frame Analyser", "bi-film")] %}
    <a class="rail-item{% if active == key %} active{% endif %}" href="{{ href }}"{% if active == key %} aria-current="page"{% endif %} title="{{ label }}">
      <i class="bi {{ icon }}" aria-hidden="true"></i><span class="rail-label">{{ label }}</span>
    </a>
    {% endfor %}
    <span class="rail-spacer"></span>
    <a class="rail-item{% if active == 'users' %} active{% endif %}" id="rail-users" href="/users" title="Users" hidden{% if active == 'users' %} aria-current="page"{% endif %}>
      <i class="bi bi-people" aria-hidden="true"></i><span class="rail-label">Users</span>
    </a>
    <button class="rail-avatar" id="rail-avatar" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="user-menu" title="Account and theme">
      <i class="bi bi-person" aria-hidden="true"></i>
    </button>
  </nav>
  <div id="user-menu" class="user-menu" role="menu" hidden></div>
{% endif %}
  <div class="app-main">
{% if not bare %}
    <header class="topbar">
      {% block topbar %}{% endblock %}
      <button class="topbar-search" type="button" data-palette-open aria-label="Search and commands (Ctrl+K)">
        <i class="bi bi-search" aria-hidden="true"></i><span class="topbar-search-text">Search</span><kbd class="kbd">⌘K</kbd>
      </button>
    </header>
{% endif %}
    <main class="app-content" id="app-content">
      {% block content %}{% endblock %}
    </main>
  </div>
  <div id="of-toasts" class="toast-host" aria-live="polite"></div>
  <dialog id="of-palette" class="palette" aria-label="Search and commands">
    <div class="palette-field"><i class="bi bi-search" aria-hidden="true"></i><input id="of-palette-input" type="text" autocomplete="off" spellcheck="false" placeholder="Go to a project, view, image or action…" aria-controls="of-palette-results" /></div>
    <div id="of-palette-results" class="palette-results" role="listbox"></div>
  </dialog>
{% if not bare %}
  <script type="module" src="/static/auth/menu.js?v={{ asset_v }}"></script>
{% endif %}
  {% block page_scripts %}{% endblock %}
</body>
</html>
```

The vendored icon CSS is under `vendor/`, so it stays unversioned on purpose. The integrity test skips `vendor/`.

- [ ] **Step 5: Create `static/ui/shell.css`**

```css
/* App shell: rail, top bar, content, palette, user menu, responsive. */
body.of-app { display: flex; min-height: 100vh; }
.rail {
  position: sticky; top: 0; height: 100vh; flex: none; width: var(--rail-w); z-index: 40;
  display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 10px 0;
  background: var(--bg-rail); border-right: 1px solid var(--line);
}
.rail-logo { display: grid; place-items: center; width: 36px; height: 36px; margin-bottom: 12px; border-radius: 10px; color: #fff; background: linear-gradient(135deg, #0a84ff, #30b0c7); }
.rail-logo svg { width: 22px; height: 22px; }
.rail-item {
  display: flex; flex-direction: column; align-items: center; gap: 2px; width: 54px; padding: 7px 0 6px; border-radius: 10px;
  color: var(--text-3); text-decoration: none; transition: background var(--t-fast) var(--ease), color var(--t-fast) var(--ease);
}
.rail-item i { font-size: 18px; line-height: 1; }
.rail-item .rail-label { font-size: 10px; font-weight: var(--fw-semibold); text-align: center; line-height: 1.1; }
.rail-item:hover { color: var(--text); background: var(--surface-1); text-decoration: none; }
.rail-item.active { color: var(--text); background: var(--surface-1); }
.rail-item.active i { color: var(--accent); }
.rail-spacer { flex: 1; }
.rail-avatar { width: 34px; height: 34px; margin-top: 6px; border: 0; border-radius: 50%; display: grid; place-items: center; background: var(--surface-2); color: var(--text); font-weight: var(--fw-bold); font-size: var(--fs-12); cursor: pointer; }
.rail-avatar:hover { background: var(--surface-3); }
.user-menu { min-width: 240px; }
.user-menu .user-head { padding: 8px 10px; }
.user-menu .user-head strong { display: block; } .user-menu .user-head small { color: var(--text-3); text-transform: capitalize; }
.user-menu .theme-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 10px; }
.user-menu .credit { display: flex; align-items: center; gap: 8px; padding: 8px 10px 4px; color: var(--text-3); font-size: var(--fs-11); }
.user-menu .credit img { height: 22px; width: auto; border-radius: 4px; background: #fff; padding: 2px 4px; }

.app-main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.topbar {
  position: sticky; top: 0; z-index: 30; height: var(--topbar-h); flex: none;
  display: flex; align-items: center; gap: var(--sp-4); padding: 0 var(--sp-5);
  background: var(--glass); backdrop-filter: saturate(180%) blur(20px); -webkit-backdrop-filter: saturate(180%) blur(20px);
  border-bottom: 1px solid var(--line);
}
@supports not (backdrop-filter: blur(1px)) { .topbar { background: var(--bg); } .popover, .toast { background: var(--surface-1); } }
.topbar-title { font-size: var(--fs-15); font-weight: var(--fw-semibold); white-space: nowrap; }
.topbar-actions { display: flex; align-items: center; gap: var(--sp-3); margin-left: auto; }
.topbar-search { display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 6px 0 10px; border: 0; border-radius: var(--r-control); background: var(--surface-1); color: var(--text-3); cursor: pointer; margin-left: var(--sp-3); }
.topbar-actions + .topbar-search { margin-left: var(--sp-3); }
.topbar > .topbar-search:first-child, .topbar > :not(.topbar-actions) + .topbar-search { margin-left: auto; }
.topbar-search:hover { color: var(--text); }
.app-content { flex: 1; min-width: 0; padding: var(--sp-6); }

/* Command palette */
dialog.palette { width: min(620px, calc(100vw - 32px)); margin-top: 12vh; padding: 0; border: 0; border-radius: var(--r-sheet); background: var(--glass-strong); backdrop-filter: saturate(180%) blur(24px); color: var(--text); box-shadow: var(--shadow-sheet); }
dialog.palette::backdrop { background: var(--scrim); }
dialog.palette[open] { animation: of-pop var(--t) var(--ease); }
.palette-field { display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-bottom: 1px solid var(--line); color: var(--text-3); }
.palette-field input { flex: 1; min-height: 28px; border: 0 !important; box-shadow: none !important; background: transparent; font-size: var(--fs-17); padding: 0; }
.palette-results { max-height: min(60vh, 460px); overflow: auto; padding: 6px; }
.palette-group { padding: 8px 10px 4px; color: var(--text-3); font-size: var(--fs-11); text-transform: uppercase; letter-spacing: .05em; }
.palette-item { display: flex; align-items: center; gap: 10px; width: 100%; padding: 8px 10px; border: 0; border-radius: var(--r-control); background: transparent; color: var(--text); text-align: left; cursor: pointer; font-size: var(--fs-13); }
.palette-item small { margin-left: auto; color: var(--text-3); }
.palette-item[aria-selected="true"] { background: var(--accent-fill); color: #fff; } .palette-item[aria-selected="true"] small { color: rgba(255,255,255,.8); }
.palette-empty { padding: 18px; text-align: center; color: var(--text-3); }

/* Bare pages (sign-in) */
body.of-bare { display: block; }
body.of-bare .app-content { padding: 0; }

/* Responsive */
@media (max-width: 900px) {
  :root { --rail-w: 52px; }
  .rail-item { width: 42px; } .rail-item .rail-label { display: none; }
  .topbar-search-text { display: none; }
}
@media (max-width: 640px) {
  body.of-app { flex-direction: column; }
  .rail { position: fixed; top: auto; bottom: 0; left: 0; right: 0; width: 100%; height: 58px; flex-direction: row; justify-content: space-around; padding: 4px 6px calc(4px + env(safe-area-inset-bottom)); border-right: 0; border-top: 1px solid var(--line); background: var(--glass); backdrop-filter: saturate(180%) blur(20px); }
  .rail-logo, .rail-spacer { display: none; }
  .rail-item { width: auto; flex: 1; } .rail-item .rail-label { display: block; font-size: 9px; }
  .rail-avatar { margin-top: 0; }
  .app-main { padding-bottom: 64px; }
  .topbar { overflow-x: auto; }
  .app-content { padding: var(--sp-4); }
}
```

- [ ] **Step 6: Create `static/ui/ui.js`**

```js
/* OceanFrame UI kit: theme, toasts, sheets, popovers and the command palette.
   A classic script loaded by base.html before page scripts, so module and classic pages share window.OF.ui. */
(function () {
  'use strict'
  const OF = window.OF = window.OF || {}
  const ui = OF.ui = OF.ui || {}

  // ── Theme ──────────────────────────────────────────────────────────────────
  const KEY = 'of-theme'
  const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null
  const themeListeners = new Set()
  function storedTheme() { try { return localStorage.getItem(KEY) || 'system' } catch (_) { return 'system' } }
  function resolveTheme(choice) { return choice === 'system' ? (media && media.matches ? 'light' : 'dark') : choice }
  function applyTheme() {
    const next = resolveTheme(storedTheme())
    document.documentElement.setAttribute('data-theme', next)
    themeListeners.forEach(fn => { try { fn(next) } catch (error) { console.warn(error) } })
  }
  ui.theme = {
    get: storedTheme,
    effective: () => document.documentElement.getAttribute('data-theme') || 'dark',
    set(choice) {
      if (!['dark', 'light', 'system'].includes(choice)) return
      try { localStorage.setItem(KEY, choice) } catch (_) { /* private mode: still applies for this page */ }
      if (choice !== 'system') document.documentElement.setAttribute('data-theme', choice)
      applyTheme()
    },
    onChange(fn) { themeListeners.add(fn); return () => themeListeners.delete(fn) },
    /** Current value of a CSS custom property, for canvases and charts. */
    token(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() },
  }
  if (media) (media.addEventListener ? media.addEventListener('change', () => { if (storedTheme() === 'system') applyTheme() }) : media.addListener(() => { if (storedTheme() === 'system') applyTheme() }))

  // ── Toasts ─────────────────────────────────────────────────────────────────
  function toastHost() {
    let host = document.getElementById('of-toasts')
    if (!host) { host = document.createElement('div'); host.id = 'of-toasts'; host.className = 'toast-host'; host.setAttribute('aria-live', 'polite'); document.body.appendChild(host) }
    return host
  }
  ui.toast = function toast(message, opts = {}) {
    const kind = opts.kind || 'info'
    const el = document.createElement('div')
    el.className = `toast toast-${kind}`
    el.setAttribute('role', kind === 'danger' ? 'alert' : 'status')
    const text = document.createElement('span')
    text.className = 'toast-text'
    text.textContent = String(message)
    el.appendChild(text)
    let timer = null
    function close() {
      clearTimeout(timer)
      el.classList.remove('is-in'); el.classList.add('is-out')
      setTimeout(() => el.remove(), 260)
    }
    if (opts.action && opts.action.label) {
      const action = document.createElement('button')
      action.type = 'button'; action.className = 'btn btn-ghost btn-sm'; action.textContent = opts.action.label
      action.addEventListener('click', () => { close(); try { opts.action.onClick() } catch (error) { console.error(error) } })
      el.appendChild(action)
    }
    const dismiss = document.createElement('button')
    dismiss.type = 'button'; dismiss.className = 'toast-close'; dismiss.setAttribute('aria-label', 'Dismiss'); dismiss.textContent = '×'
    dismiss.addEventListener('click', close)
    el.appendChild(dismiss)
    toastHost().appendChild(el)
    requestAnimationFrame(() => el.classList.add('is-in'))
    const timeout = opts.timeout != null ? opts.timeout : (kind === 'danger' ? 0 : 4000)
    if (timeout) timer = setTimeout(close, timeout)
    return { close }
  }

  // ── Sheets (confirm / prompt) ──────────────────────────────────────────────
  let sheetCount = 0
  function sheet({ title, message = '', confirmLabel = 'OK', cancelLabel = 'Cancel', danger = false, input = null }) {
    return new Promise(resolve => {
      const id = `of-sheet-${++sheetCount}`
      const dialog = document.createElement('dialog')
      dialog.className = 'sheet'
      dialog.setAttribute('aria-labelledby', `${id}-title`)
      const form = document.createElement('form')
      form.method = 'dialog'
      const heading = document.createElement('h2')
      heading.className = 'sheet-title'; heading.id = `${id}-title`; heading.textContent = title
      form.appendChild(heading)
      if (message) { const p = document.createElement('p'); p.className = 'sheet-message'; p.textContent = message; form.appendChild(p) }
      let field = null
      let error = null
      if (input) {
        const label = document.createElement('label')
        label.className = 'form-label'; label.textContent = input.label || ''
        field = document.createElement('input')
        field.type = 'text'; field.className = 'form-control'; field.value = input.value || ''; field.autocomplete = 'off'; field.spellcheck = false
        label.appendChild(field)
        form.appendChild(label)
        error = document.createElement('p')
        error.className = 'sheet-error'; error.hidden = true; error.setAttribute('role', 'alert')
        form.appendChild(error)
      }
      const actions = document.createElement('div')
      actions.className = 'sheet-actions'
      const cancel = document.createElement('button')
      cancel.type = 'button'; cancel.className = 'btn'; cancel.textContent = cancelLabel; cancel.dataset.sheet = 'cancel'
      const ok = document.createElement('button')
      ok.type = 'submit'; ok.className = danger ? 'btn btn-danger' : 'btn btn-primary'; ok.textContent = confirmLabel; ok.dataset.sheet = 'confirm'
      actions.append(cancel, ok)
      form.appendChild(actions)
      dialog.appendChild(form)
      document.body.appendChild(dialog)
      let settled = false
      function finish(value) {
        if (settled) return
        settled = true
        try { dialog.close() } catch (_) { /* already closed */ }
        dialog.remove()
        resolve(value)
      }
      cancel.addEventListener('click', () => finish(input ? null : false))
      dialog.addEventListener('cancel', event => { event.preventDefault(); finish(input ? null : false) })
      form.addEventListener('submit', event => {
        event.preventDefault()
        if (!input) return finish(true)
        const value = field.value
        const problem = typeof input.validate === 'function' ? input.validate(value) : null
        if (problem) { error.textContent = problem; error.hidden = false; field.focus(); field.select(); return }
        finish(value)
      })
      dialog.showModal()
      ;(field || (danger ? cancel : ok)).focus()
    })
  }
  ui.confirm = opts => sheet(opts)
  ui.prompt = ({ label, value, validate, ...rest }) => sheet({ ...rest, input: { label, value, validate } })

  // ── Popovers and menus ─────────────────────────────────────────────────────
  let open = null
  function focusables(panel) { return [...panel.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]')] }
  ui.closePopover = function closePopover() {
    if (!open) return
    const { anchor, panel, onDoc, onKey, onResize } = open
    open = null
    panel.hidden = true
    panel.classList.remove('popover')
    anchor.setAttribute('aria-expanded', 'false')
    document.removeEventListener('pointerdown', onDoc, true)
    document.removeEventListener('keydown', onKey, true)
    window.removeEventListener('resize', onResize)
  }
  ui.popover = function popover(anchor, panel) {
    if (open && open.panel === panel) { ui.closePopover(); return null }
    ui.closePopover()
    panel.hidden = false
    panel.classList.add('popover')
    function place() {
      const r = anchor.getBoundingClientRect()
      const w = panel.offsetWidth
      const h = panel.offsetHeight
      const rail = anchor.closest('.rail')
      let left = rail && window.innerWidth > 640 ? r.right + 8 : r.left
      let top = rail && window.innerWidth > 640 ? Math.max(8, r.bottom - h) : r.bottom + 6
      left = Math.max(8, Math.min(left, window.innerWidth - w - 8))
      if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6)
      panel.style.left = `${left}px`
      panel.style.top = `${top}px`
    }
    place()
    anchor.setAttribute('aria-expanded', 'true')
    const onDoc = event => { if (!panel.contains(event.target) && !anchor.contains(event.target)) ui.closePopover() }
    const onKey = event => {
      if (event.key === 'Escape') { event.stopPropagation(); ui.closePopover(); anchor.focus(); return }
      if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && !event.target.closest('select, input, textarea')) {
        const items = focusables(panel)
        if (!items.length) return
        const index = items.indexOf(document.activeElement)
        const next = items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]
        next.focus()
        event.preventDefault()
      }
    }
    const onResize = () => place()
    document.addEventListener('pointerdown', onDoc, true)
    document.addEventListener('keydown', onKey, true)
    window.addEventListener('resize', onResize)
    open = { anchor, panel, onDoc, onKey, onResize }
    const first = focusables(panel)[0]
    if (first) first.focus({ preventScroll: true })
    return ui.closePopover
  }

  // ── Command palette ────────────────────────────────────────────────────────
  const providers = []
  let results = []
  let cursor = 0
  let queryToken = 0
  function score(label, query) {
    if (!query) return 1
    const text = label.toLowerCase()
    const q = query.toLowerCase().trim()
    if (!q) return 1
    if (text.includes(q)) return 100 - text.indexOf(q)
    let at = 0
    for (const ch of q.replace(/\s+/g, '')) { at = text.indexOf(ch, at); if (at < 0) return 0; at += 1 }
    return 10
  }
  async function gather(query) {
    const token = ++queryToken
    const settled = await Promise.allSettled(providers.map(async provider => {
      const items = await provider.items(query)
      const ranked = (items || [])
        .map(item => ({ item, s: provider.filter === false ? 1 : score(`${item.label} ${item.hint || ''}`, query) }))
        .filter(row => row.s > 0)
        .sort((a, b) => b.s - a.s)
        .slice(0, provider.limit || 8)
        .map(row => row.item)
      return { group: provider.group, items: ranked }
    }))
    if (token !== queryToken) return null
    return settled.filter(r => r.status === 'fulfilled' && r.value.items.length).map(r => r.value)
  }
  function renderPalette(groups) {
    const list = document.getElementById('of-palette-results')
    if (!list) return
    list.textContent = ''
    results = []
    if (!groups.length) {
      const empty = document.createElement('div'); empty.className = 'palette-empty'; empty.textContent = 'No matches'
      list.appendChild(empty)
      return
    }
    for (const group of groups) {
      const heading = document.createElement('div'); heading.className = 'palette-group'; heading.textContent = group.group
      list.appendChild(heading)
      for (const item of group.items) {
        const index = results.length
        const row = document.createElement('button')
        row.type = 'button'; row.className = 'palette-item'; row.setAttribute('role', 'option')
        if (item.icon) { const i = document.createElement('i'); i.className = `bi ${item.icon}`; i.setAttribute('aria-hidden', 'true'); row.appendChild(i) }
        const label = document.createElement('span'); label.textContent = item.label; row.appendChild(label)
        if (item.hint) { const hint = document.createElement('small'); hint.textContent = item.hint; row.appendChild(hint) }
        row.addEventListener('click', () => runResult(index))
        row.addEventListener('mousemove', () => select(index))
        list.appendChild(row)
        results.push({ item, row })
      }
    }
    select(0)
  }
  function select(index) {
    if (!results.length) return
    cursor = (index + results.length) % results.length
    results.forEach((r, i) => r.row.setAttribute('aria-selected', String(i === cursor)))
    results[cursor].row.scrollIntoView({ block: 'nearest' })
  }
  function runResult(index) {
    const chosen = results[index]
    if (!chosen) return
    ui.palette.close()
    try { chosen.item.run() } catch (error) { console.error(error) }
  }
  let debounce = null
  ui.palette = {
    register(provider) { if (provider && typeof provider.items === 'function') providers.push(provider) },
    async open() {
      const dialog = document.getElementById('of-palette')
      const input = document.getElementById('of-palette-input')
      if (!dialog || !input || dialog.open) return
      ui.closePopover()
      input.value = ''
      dialog.showModal()
      input.focus()
      const groups = await gather('')
      if (groups) renderPalette(groups)
    },
    close() { const dialog = document.getElementById('of-palette'); if (dialog && dialog.open) dialog.close() },
  }

  // ── Shell wiring ───────────────────────────────────────────────────────────
  ui.palette.register({
    group: 'Go to',
    items: () => [
      { label: 'Library', icon: 'bi-images', run: () => { location.href = '/' } },
      { label: 'Collections', icon: 'bi-collection', run: () => { location.href = '/collections' } },
      { label: 'Curation', icon: 'bi-bullseye', run: () => { location.href = '/curation' } },
      { label: 'Frame Analyser', icon: 'bi-film', run: () => { location.href = '/frames' } },
      { label: 'Account', icon: 'bi-person', run: () => { location.href = '/account' } },
    ],
  })
  ui.palette.register({
    group: 'Actions',
    items: () => [
      { label: 'Use dark theme', icon: 'bi-moon', run: () => ui.theme.set('dark') },
      { label: 'Use light theme', icon: 'bi-sun', run: () => ui.theme.set('light') },
      { label: 'Match system theme', icon: 'bi-circle-half', run: () => ui.theme.set('system') },
    ],
  })

  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 'k') {
      event.preventDefault()
      ui.palette.open()
    }
  })
  document.addEventListener('click', event => {
    const opener = event.target.closest('[data-palette-open]')
    if (opener) { event.preventDefault(); ui.palette.open() }
  })
  document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('of-palette-input')
    const dialog = document.getElementById('of-palette')
    if (input && dialog) {
      input.addEventListener('input', () => {
        clearTimeout(debounce)
        debounce = setTimeout(async () => { const groups = await gather(input.value); if (groups) renderPalette(groups) }, 120)
      })
      input.addEventListener('keydown', event => {
        if (event.key === 'ArrowDown') { event.preventDefault(); select(cursor + 1) }
        else if (event.key === 'ArrowUp') { event.preventDefault(); select(cursor - 1) }
        else if (event.key === 'Enter') { event.preventDefault(); runResult(cursor) }
      })
      dialog.addEventListener('click', event => { if (event.target === dialog) ui.palette.close() })
    }
    const avatar = document.getElementById('rail-avatar')
    const menu = document.getElementById('user-menu')
    if (avatar && menu) avatar.addEventListener('click', () => ui.popover(avatar, menu))
  })
})()
```

- [ ] **Step 7: `static/auth/menu.js`.** Rewrite it so it always fills `#user-menu` with the following:
  1. When signed in: a head showing the label and role, then "Account" (`/account`), then "Users" (`/users`, admins only).
  2. A **Theme** row with a `.segmented` of Dark / Light / System buttons. They call `window.OF.ui.theme.set` and show the current choice as `aria-pressed`.
  3. "Sign out" (`/logout`), when signed in.
  4. A `.credit` row: "an app by" plus `<img src="/static/img/optics-si-logo.png" alt="OPTICS SI">`.

  For admins, also remove `hidden` from `#rail-users`. When signed in, set the avatar button's text to the user's initials (or the first letter of the label). Otherwise keep the person icon. When login is off, there's no user: show only the Theme row and the credit. Use `textContent` for all user data. Keep the export-free module shape and the `whoami` import. Copy the logo first:

  ```bash
  cp "docs/optics_si_logo_v1 (1).png" static/img/optics-si-logo.png
  ```

- [ ] **Step 8: `static/app.css`.** Delete the `:root { … }` block that defines the legacy variables (from `--bg` through `--font-mono`). Keep any `font-family`, `color` and `background` declarations by moving them to a `body { }` rule that uses `var(--font)`, `var(--text)` and `var(--bg)`. Delete the `.site-header`, `.header-*`, `.brand-*`, `.nav-*`, `.team-*` and `.user-menu`/`.user-chip`/`.user-role` rules: the shell replaces them, and `_header.html` stays only until every page is migrated. If a page that hasn't been migrated yet still includes `_header.html`, its header will look unstyled until its task runs. That's expected inside this branch, and Tasks 3–6 cover every page.

- [ ] **Step 9: `templates/account.html` extends the shell**

```jinja
{% set active = 'account' %}
{% extends "base.html" %}
{% block title %}Account — OceanFrame{% endblock %}
{% block page_css %}<link rel="stylesheet" href="/static/app.css?v={{ asset_v }}" /><link rel="stylesheet" href="/static/auth.css?v={{ asset_v }}" />{% endblock %}
{% block topbar %}<span class="topbar-title">Account</span>{% endblock %}
{% block content %}
  … the existing <main class="auth-shell auth-shell-wide"> children, unchanged, inside a <div class="auth-shell auth-shell-wide"> …
{% endblock %}
{% block page_scripts %}<script type="module" src="/static/auth/account.js?v={{ asset_v }}"></script>{% endblock %}
```

Keep every id and form name exactly as it is. Change `<main>` to `<div>`, because `base.html` provides `<main>`. Restyle `.card`/`.auth-card` in `auth.css` onto the tokens: panel look, surface-1, hairline, radius 12. Its inputs (`.auth-input`) take the same look as `.form-control`.

- [ ] **Step 10: Run**
  - TEST `tests/test_ui_integrity.py tests/test_ui_tokens.py tests/test_auth.py`, then the full suite.
  - JS SYNTAX.

  Expected: PASS. If the integrity test reports ids that `menu.js` or `account.js` create at runtime, add exactly those to `CREATED_AT_RUNTIME`, each with a comment naming the script.

- [ ] **Step 11: Commit**

```bash
git add main.py templates/base.html templates/account.html static/ui/ui.js static/ui/shell.css static/auth/menu.js static/app.css static/auth.css static/img/optics-si-logo.png tests/test_ui_integrity.py
git commit -m "Shared shell: base template with rail, top bar, palette, toasts and sheets (OF.ui); Account on the shell"
```

---

### Task 3: Sign-in and Users pages

**Files:**
- Modify: `templates/login.html`, `templates/users.html`, `static/auth.css`, `static/auth/users.js`, `static/auth/login.js` (only if markup moves)
- Test: `tests/test_ui_integrity.py`

**Interfaces — Consumes:** `base.html` (with `bare`), `OF.ui.confirm` and `OF.ui.toast`.

**Rules:**
- **Login:** set `{% set bare = true %}`, then extend `base.html`. Show a centred card (max 380 px) on `--bg`, holding:
  - the OceanFrame mark (the same SVG as the rail logo) and the title "OceanFrame";
  - the existing form, with every id and name unchanged;
  - the signup link and its states, as today;
  - beneath the card, "an app by" plus the OPTICS SI logo (`/static/img/optics-si-logo.png`, 28 px high, on a small white rounded backing so it reads in dark).
- **Users:** set `{% set active = 'users' %}`. The top bar holds the title "Users". The user table uses `.table` inside a `.panel`, role selects use `.form-select`, and buttons use the `.btn` variants. The one native dialog in `users.js` becomes `await window.OF.ui.confirm({...danger: true})`. Make the enclosing handler `async` and keep the same wording. Success and error messages that were inline status text stay, and additionally call `OF.ui.toast`.
- `auth.css` uses only tokens, with no hex colours.

- [ ] **Step 1: Extend the tests.**
  - Add `("/users", ["auth/users.js", "auth/menu.js", "ui/ui.js"], "users")` to `PAGES`.
  - Replace `test_login_page_is_bare_but_themed` with an auth-enabled version, using the `app_client` pattern from `tests/test_auth.py` (copy that fixture locally as `auth_client`):

```python
def test_login_page_is_bare_but_themed(auth_client):
    html = auth_client.get("/login").text
    assert 'id="rail"' not in html and "data-theme" in html and "optics-si-logo.png" in html
    ids = re.findall(r'(?<![\w-])id="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    missing = script_ids(["auth/login.js"]) - set(ids) - CREATED_AT_RUNTIME  # ui.js reads shell ids that a bare page omits
    assert not missing, missing
```

- [ ] **Step 2: Run** TEST `tests/test_ui_integrity.py`. It should FAIL for `/users` and `/login`.
- [ ] **Step 3: Implement** the rules above.
- [ ] **Step 4: Check** that `grep -nE "\b(confirm|prompt|alert)\(" static/auth/*.js` returns nothing except `OF.ui.*` calls. Run TEST `tests/test_ui_integrity.py tests/test_auth.py`, then the full suite and JS SYNTAX. All should PASS.
- [ ] **Step 5: Commit**

```bash
git add templates/login.html templates/users.html static/auth.css static/auth/users.js static/auth/login.js tests/test_ui_integrity.py
git commit -m "Sign-in and Users on the new design: bare themed sign-in with the OPTICS SI credit, users table and sheet confirm"
```

---

### Task 4: Library, Collections and the Collection form

**Files:**
- Modify: `templates/library.html`, `templates/collections.html`, `templates/collection_form.html`, `static/library.css`, `static/app.css` (rules used by these pages), `static/library/admin.js`, `static/library/datasets.js`, and any `static/library/*.js` whose markup strings use colours inline
- Test: `tests/test_ui_integrity.py`

**Rules:**
- **Library** (`active='library'`): the top bar holds:
  - the existing search input, styled as a search field;
  - the existing All/Datasets switch, as `.tabs`;
  - the collection actions that used to be `page_actions` (My collections, + Add collection), in `.topbar-actions`, with "+ Add collection" as `.btn-primary btn-sm`.

  The grid items use the tile look. Keep the existing grid classes and add the tile appearance through `library.css`, using token values: radius 9, hairline, hover lift, and accent outline when selected. The detail panel, lightbox and job panel use panels, progress and badges. The filter sidebar keeps its controls and ids and is restyled on the tokens.
- **Collections** (`active='collections'`) and **Collection form** (`active='collections'`): `.panel`, `.table`, `.form-control` and `.btn` variants. "Delete" actions use `OF.ui.confirm` with `danger: true`.
- **Dialogs:** the 3 dialogs in `library/admin.js` and the 1 in `library/datasets.js` become `OF.ui.confirm` or `OF.ui.prompt`, awaited inside `async` handlers. Any `alert(` becomes `OF.ui.toast(message, {kind: 'danger'})`.
- `library.css` and the library-related rules in `app.css` have no hex colours left, except image overlays that must stay dark on photos (`rgba(0,0,0,…)` is allowed).
- **Hooks:** every hook stays. The integrity test lists for these pages are built from the scripts each template loads: read the `<script src>` tags and include every first-party JS file reachable through ES `import` from them. `static/library/app.js` imports the others.

- [ ] **Step 1: Extend `PAGES`**, adding `("/", [...], "library")`, `("/collections", [...], "collections")` and `("/collections/new", [...], "collections")`. Fill each script list by following the imports, for example `library/app.js`, `library/api.js`, `library/grid.js` and the rest, plus `auth/menu.js` and `ui/ui.js`. Run TEST `tests/test_ui_integrity.py`. It should FAIL for these pages.
- [ ] **Step 2: Implement** the rules above.
- [ ] **Step 3: Check**
  - `grep -nE "\b(confirm|prompt|alert)\(" static/library/*.js` returns only `OF.ui.*` calls.
  - TEST `tests/test_ui_integrity.py tests/test_datasets.py tests/test_api.py`, then the full suite and JS SYNTAX, all PASS.
  - Any id the scripts create at runtime goes in `CREATED_AT_RUNTIME` with a comment.
- [ ] **Step 4: Commit**

```bash
git add templates/library.html templates/collections.html templates/collection_form.html static/library.css static/app.css static/library tests/test_ui_integrity.py
git commit -m "Library and collections on the new design: top bar, tiles, panels, sheet confirms"
```

---

### Task 5: Frame Analyser

**Files:**
- Modify: `templates/index.html`, `static/app.css`, `static/app.js`, `static/modules/export.js`, `static/modules/charts.js`, and any `static/modules/*.js` with inline colours
- Test: `tests/test_ui_integrity.py`

**Rules:**
- **Shell and layout:** set `active='frames'`. The top bar holds the title "Frame Analyser" and the section anchors (Upload, Parameters, Frames, Export) as `.tabs` links. Keep the `href="#…"` anchors. Each section becomes a `.panel`, and the upload drop zone uses a dashed `--line` border with a `drag-over` state in accent tint.
- **Charts:** `modules/charts.js` reads its colours from `window.OF.ui.theme.token('--text-3')`, `'--line'`, `'--accent'`, `'--ok'`, `'--warn'` and `'--danger'` when it builds each chart. It registers `OF.ui.theme.onChange(() => rebuild or update every live chart)`, so a theme switch recolours open charts.
- **Dialogs:** the 4 dialogs in `modules/export.js` become sheets or toasts.
- **Colours:** no hex colours remain in the Frame Analyser rules of `app.css`, except chart series colours, which come from tokens at runtime.

- [ ] **Step 1: Extend the tests.** Add `("/frames", ["app.js", "modules/…" every module app.js imports, "auth/menu.js", "ui/ui.js"], "frames")` to `PAGES`, and add this test:

```python
def test_charts_follow_the_theme():
    source = (STATIC / "modules" / "charts.js").read_text(encoding="utf-8")
    assert "OF.ui.theme.token(" in source and "OF.ui.theme.onChange(" in source
```

Run TEST `tests/test_ui_integrity.py`. It should FAIL.
- [ ] **Step 2: Implement** the rules above.
- [ ] **Step 3: Check**
  - `grep -nE "\b(confirm|prompt|alert)\(" static/modules/*.js static/app.js` returns only `OF.ui.*` calls.
  - TEST `tests/test_ui_integrity.py`, the full suite, and JS SYNTAX, all PASS.
- [ ] **Step 4: Commit**

```bash
git add templates/index.html static/app.css static/app.js static/modules tests/test_ui_integrity.py
git commit -m "Frame Analyser on the new design: panels, section tabs, theme-aware charts, sheets for export prompts"
```

---

### Task 6: Curation — shell, top bar, filter popover and chips, queue

**Files:**
- Modify: `templates/curation.html`, `static/curation.css`, `static/curation/curation.js`
- Test: `tests/test_ui_integrity.py`, `tests/test_curation.py` (drop the old curation-only integrity and duplicate-id tests, which are now covered by the rendered check; keep every other curation page test)

**Interfaces:**
- **Consumes:** `base.html`, `OF.ui.popover`, `OF.ui.toast`.
- **Produces:**
  - `#filters-button` and `#filters-popover` (which contains the moved filter controls);
  - `#filter-chips` (the chip row);
  - `#queue-toggle`;
  - `renderFilterChipBar()` in curation.js, called wherever filters change.

**Rules:**
1. **Base template.**
   - Set `{% set active = 'curation' %}`, then extend `base.html`.
   - Everything that was in `<body>` moves into `{% block content %}`, except the old header include.
   - Keep the script order: `curation.js`, then `hub.js`, then `collab.js`, then `editor.js`, all classic scripts with `?v=`.
   - Keep every existing id, `data-*` attribute and class.
2. **Top bar** (`{% block topbar %}`), holding these existing elements, moved without being renamed:
   - The project select, styled as a picker. Wrapping it is enough: give the existing select the picker look through CSS (`.project-picker select`).
   - The view buttons (`#source-view-button`, `#crop-view-button`, and so on) inside a `.tabs` container. Their `active` toggling stays as it is in curation.js.
   - The existing uncommitted-changes pill from collab.js, inside `.topbar-actions`.
   - Any project-level buttons that used to sit in the curation header (members, releases, hub and similar), in `.topbar-actions`, as `.btn btn-sm`, keeping their ids.
3. **Filters.**
   - Every filter control in the old filter bar moves, unchanged, into `<div id="filters-popover" hidden>`. That covers the region, island, year, site, annotator, origin, state and included selects, `#filter-image`, `#filter-label`, the taxonomy picker, code input and chips, the hide-missing and show-removed checkboxes, `#apply-filters` and the clear button.
   - A new `<button id="filters-button" class="btn btn-sm"><i class="bi bi-funnel"></i> Filters</button>` opens it with `OF.ui.popover(byId('filters-button'), byId('filters-popover'))`.
   - Under the top bar, a filter bar `<div class="filter-bar"><div id="filter-chips"></div><span id="filter-count" …></span></div>` shows one `.chip.chip-active` per active filter, labelled like "Region: Hawaii ✕". Clicking ✕ clears that control and calls the existing `cascadeFilters()`.
   - "Hide missing images" and "Show removed" render as toggle chips. "Hide missing" uses `chip-active` when checked. Clicking one flips the underlying checkbox and dispatches its `change` event.
   - `renderFilterChipBar()` builds the chips from the current control values. It's called at the end of `cascadeFilters()`, `renderFilterChips()` and `clearFilters` (whatever the clear function is called), and after project switch.
   - Use `escapeHtml` for values.
4. **Queue.**
   - `renderQueue` (the function that fills `#queue-list`) adds a 40×30 thumbnail per image. The `<img loading="lazy">` points at `proxyUrl(image.source_image_uri)`, with `onerror` hiding the image. Each row also shows the name, a points count and a reviewed-progress line.
   - A `#queue-toggle` button collapses the queue column. It toggles the class `queue-collapsed` on the workspace and remembers the choice in `localStorage` (wrapped in try/catch).
5. **`curation.css`.**
   - Delete the `:root` block of `--cur-*` colours, and replace their uses with tokens: `--cur-ink` becomes `--text`, `--cur-deep` becomes `--surface-1`, `--cur-ok` becomes `--ok`, `--cur-bad` becomes `--danger`.
   - Replace every hex colour with tokens, except the label colour palette and the matrix heat scale, which stay data colours.
   - Panels use the panel look. The old curation page header styles go.

- [ ] **Step 1: Write the failing tests.** Add `("/curation", ["curation/curation.js", "curation/hub.js", "curation/collab.js", "curation/editor.js", "auth/menu.js", "ui/ui.js"], "curation")` to `PAGES`. Add this test to `tests/test_ui_integrity.py`:

```python
def test_curation_filters_live_in_one_popover(client):
    html = render(client, "/curation")
    popover = re.search(r'<div[^>]*id="filters-popover"[^>]*>(.*?)\n\s*</div>\s*<!-- /filters-popover -->', html, re.S)
    assert popover, "filters popover missing (close it with <!-- /filters-popover -->)"
    for control in ("filter-region", "filter-year", "filter-state", "filter-included", "filter-hide-missing", "filter-show-removed", "apply-filters"):
        assert f'id="{control}"' in popover.group(1), control
    assert 'id="filters-button"' in html and 'id="filter-chips"' in html and 'id="queue-toggle"' in html
```

Delete the now-redundant curation-only id and duplicate-id tests from `tests/test_curation.py`. Search for `script_ids |= set(` and for `byId() returns the first match`. Run TEST `tests/test_ui_integrity.py`. It should FAIL.
- [ ] **Step 2: Implement** the rules above.
- [ ] **Step 3: Run** TEST `tests/test_ui_integrity.py tests/test_curation.py`, then the full suite and JS SYNTAX. All should PASS.
- [ ] **Step 4: Commit**

```bash
git add templates/curation.html static/curation.css static/curation/curation.js tests/test_ui_integrity.py tests/test_curation.py
git commit -m "Curation on the shell: top bar with picker and view tabs, filters in one popover with chips, queue thumbnails"
```

---

### Task 7: Curation — the source workspace (stage overlays, floating tray, inspector tabs)

**Files:**
- Modify: `templates/curation.html`, `static/curation.css`, `static/curation/curation.js`, `static/curation/editor.js`
- Test: `tests/test_ui_integrity.py`, `tests/browser/p3a_browser_check.py` (new copy, see Task 12)

**Interfaces — Produces:**
- `#stage-toolbar`, containing the existing add-point toggle, bulk-add button and fit/zoom buttons, moved;
- `#stage-nav`, with `#stage-prev`, `#stage-position` and `#stage-next`;
- `#inspector-tabs`, with buttons `data-inspector-tab="point|image|model"`;
- the panels `#inspector-point`, `#inspector-image` and `#inspector-model`;
- `#pending-tray`, which keeps its id but is restyled as a floating pill containing `#pending-count`, `#pending-save`, `#pending-discard` and a new `#pending-review` button that toggles `#pending-list` as a popover.

**Rules:**
1. **Stage toolbar.** It sits inside `#source-image-wrap`, absolutely positioned top-left, and holds the existing controls:
   - the add-point mode toggle;
   - "Bulk add around…" (`#bulk-add-button`, moved from the point tools);
   - the zoom and fit buttons.
2. **Image stepping.** `#stage-nav` sits top-right and shows "‹ 12 / 3,104 ›". Its buttons call the existing `stepImage(±1)` from editor.js, which already goes through the unsaved-edits guard. The position comes from `state.images.indexOf(current)` and the queue total.
3. **Tray.** `#pending-tray` becomes a glass pill at the bottom centre of the stage. It shows the count and a breakdown ("1 moved · 2 added · 1 removed"), computed in `renderPending` from `state.pending`. `#pending-reason` moves into the Review popover, next to `#pending-list`.
4. **Overlay rule.**
   - `#stage-toolbar`, `#stage-nav`, `#pending-tray` and the Review popover call `event.stopPropagation()` on `pointerdown`, `click` and `wheel`.
   - The image pointer handlers in curation.js (around the `if (event.target.closest('.source-marker')) return` lines) also return early for `event.target.closest('#stage-toolbar, #stage-nav, #pending-tray, .popover')`.
5. **Resize rule.** Add a `ResizeObserver` on `byId('source-image-wrap')`. Its callback, throttled with `requestAnimationFrame`, calls `applyImageTransform()` if that exists, then `renderMarkers()`. Register it once in init.
6. **Inspector tabs.** The right column gets `#inspector-tabs`, a `.segmented` control with Point, Image and Model.
   - **Point** holds the existing item form: crop preview, label, review state, inclusion, size field `#point-edit-size` and presets, suggestions, Remove/Restore, and the bulk-add form `#bulk-add-form`.
   - **Image** holds the annotation table panel, the image metadata and `#source-missing-notice`, which stays inside the image wrap as well. Move only the panel that's in the side column.
   - **Model** holds classify point and its results, and the evaluation context.
   - Switching tabs toggles `d-none` on the panels, remembers the choice in `localStorage`, and triggers the ResizeObserver path naturally.
   - Selecting a point switches to Point unless the user picked Model.
7. **Styling.** The stage gets radius 12 on `--surface-1`. Markers keep their label colours, and selected markers get a white border plus an accent glow.

- [ ] **Step 1: Write the failing test** (add to `tests/test_ui_integrity.py`)

```python
def test_source_workspace_structure(client):
    html = render(client, "/curation")
    for element in ("stage-toolbar", "stage-nav", "stage-prev", "stage-next", "stage-position", "inspector-tabs",
                    "inspector-point", "inspector-image", "inspector-model", "pending-review"):
        assert f'id="{element}"' in html, element
    wrap = html.split('id="source-image-wrap"', 1)[1].split("<!-- /source-image-wrap -->", 1)[0]
    assert 'id="stage-toolbar"' in wrap and 'id="stage-nav"' in wrap, "overlays belong inside the image wrap"
    script = (STATIC / "curation" / "curation.js").read_text(encoding="utf-8")
    assert "ResizeObserver" in script and "#stage-toolbar, #stage-nav, #pending-tray" in script
```

Close the wrap in the template with `<!-- /source-image-wrap -->`. Run TEST `tests/test_ui_integrity.py`. It should FAIL.
- [ ] **Step 2: Implement** rules 1–7.
- [ ] **Step 3: Run** TEST `tests/test_ui_integrity.py tests/test_curation.py tests/test_editing.py`, then the full suite and JS SYNTAX. All should PASS.
- [ ] **Step 4: Browser check (Review Focus 1 and 4).**
  1. Deploy the app container:

     ```bash
     docker compose -f docker-compose.stack.yml -f docker-compose.evaluation.yml --profile evaluation up -d --build --no-deps oceanframe-app
     ```

  2. Run `tests/browser/p3a_browser_check.py`, from Task 12; create it now if it doesn't exist yet. Copy the session-scratch version, then add these two checks:
     - **(a) Overlay click:** select a point, record `state.imageView` and `state.pending`, click `#pending-save` while there are no pending edits (the tray is hidden), and click `#stage-next`. No pan change and no new point are recorded. Then make one move and click `#pending-save`. Exactly one save request is sent, and no point is added.
     - **(b) Markers after resize:** click `#queue-toggle` and switch inspector tabs. Each marker's centre stays within 2 px of `column_px*scale` / `row_px*scale`, computed from `state`.
  3. Run the command used in P3a:

     ```bash
     docker run --rm --network oceanframe-net -e OF_PW2 -v "<repo>/tests/browser:/work" mcr.microsoft.com/playwright/python:v1.47.0-jammy …
     ```

     Pass the tester2 password through the `OF_PW2` environment variable, read from `$TMP/of-tester2-password.txt`.
- [ ] **Step 5: Commit**

```bash
git add templates/curation.html static/curation.css static/curation/curation.js static/curation/editor.js tests/test_ui_integrity.py tests/browser/p3a_browser_check.py
git commit -m "Curation source workspace: on-canvas toolbar and stepping, floating unsaved-edits pill, inspector tabs, resize-safe markers"
```

---

### Task 8: Curation — Crops, Quality, Matrix, Status, History, Compare, Releases

**Files:**
- Modify: `templates/curation.html`, `static/curation.css`, `static/curation/curation.js` (markup strings and classes only), `static/curation/collab.js`, `static/curation/hub.js`
- Test: `tests/test_ui_integrity.py`

**Rules:**
- **Crop grid and evaluation grid cards:**
  - They use the tile look. `.curation-crop-card` keeps its class, and `curation.css` gives it the tile appearance.
  - The label pill sits over the image.
  - The selection checkbox shows on hover or when selected.
  - Missing-source crops show a `.badge-danger` "Not in bucket" over a neutral placeholder (this closes the P3a follow-up).
- **Bulk action bars** (crop grid, matrix): a floating selection bar fixed to the bottom centre of the content, in glass. It appears only when something is selected and reads "N selected · Include · Exclude · Relabel to… · Clear". The existing controls are moved and keep their ids.
- **Quality, Status, History, Compare, Releases, Members and Hub panels:**
  - Panels use `.panel`.
  - Tables use `.table`.
  - Status and decision indicators use `.badge-*`.
  - Charts and split bars read token colours, through CSS variables in markup strings (`var(--ok)` and so on), never hex.
  - The `<dialog>` elements that collab.js and hub.js already use become `class="sheet"`, so they pick up the sheet look.
- **Empty states:** "No crops match", "No history yet" and similar use `.empty-state` with a clear action where there is one (Clear filters, Load images).
- **Skeletons:** loading states for the crop grid, quality list and queue show `.skeleton` blocks: 12 tiles or 6 rows, removed when the data renders.
- **No hook changes.** Markup strings in JS may gain classes, but must not drop an existing class or attribute that any script or test reads.

- [ ] **Step 1: Write the failing test**

```python
def test_curation_views_use_shared_components():
    css = (STATIC / "curation.css").read_text(encoding="utf-8")
    themed_part = css.split("/* label palette */")[0]
    assert not re.findall(r"#[0-9a-fA-F]{6}\b", re.sub(r"/\*.*?\*/", "", themed_part, flags=re.S)), \
        "curation.css uses hex colours outside the label palette section"
    script = (STATIC / "curation" / "curation.js").read_text(encoding="utf-8")
    assert "empty-state" in script and "skeleton" in script and "Not in bucket" in script
    html = (STATIC.parent / "templates" / "curation.html").read_text(encoding="utf-8")
    assert 'class="selection-bar' in html
```

Put the label colour palette and the matrix heat scale at the end of `curation.css`, under a `/* label palette */` comment. Run TEST `tests/test_ui_integrity.py`. It should FAIL.
- [ ] **Step 2: Implement** the rules above.
- [ ] **Step 3: Run** TEST `tests/test_ui_integrity.py tests/test_curation.py`, then the full suite and JS SYNTAX. All should PASS.
- [ ] **Step 4: Commit**

```bash
git add templates/curation.html static/curation.css static/curation tests/test_ui_integrity.py
git commit -m "Curation views on shared components: tiles, floating selection bar, panels, badges, empty states, skeletons"
```

---

### Task 9: Native dialogs become sheets everywhere (async guard chain)

**Files:**
- Modify: `static/curation/curation.js`, `static/curation/editor.js`, `static/curation/collab.js`, plus any remaining `static/**` dialog call
- Create: `tests/test_ui_dialogs.py`

**Interfaces — Produces:** `async function confirmDiscardPending()` returns `Promise<boolean>`. Every guard caller awaits it.

**Rules:**
1. **The discard guard.** `confirmDiscardPending()` becomes:

```js
async function confirmDiscardPending() {
  const count = pendingCount()
  if (!count) return true
  const ok = await window.OF.ui.confirm({
    title: `Discard ${count} unsaved edit${count === 1 ? '' : 's'}?`,
    message: 'Moves, resizes and new points on this image will be lost.',
    confirmLabel: 'Discard', cancelLabel: 'Keep editing', danger: true,
  })
  if (ok) resetPending()
  return ok
}
```

2. **Every caller awaits it.** Find each caller with `grep -n "confirmDiscardPending" static/curation/*.js`. Each `if (!confirmDiscardPending()) return` becomes `if (!(await confirmDiscardPending())) return`, inside a function made `async` if it wasn't already. Then check every caller of each newly-async function.
   - Fire-and-forget event handlers are fine.
   - Functions whose return value is used, such as `selectImage` returning a boolean or `setView` returning a promise, must still return the same thing, now as a promise. Their callers must `await` them where they used the result: the hashchange handler, the Back button, `stepImage`, `addManualPoint`, project create/delete, hub create/link, drilldown clear, `switchProject`, and the `typeof confirmDiscardPending === 'function'` guards in curation.js.
3. **`beforeunload`** stays native: `event.preventDefault(); event.returnValue = ''`.
4. **Project delete.** Today it uses a `prompt` asking the user to type the name. It becomes:

```js
const typed = await window.OF.ui.prompt({
  title: `Delete ${project.name}?`, message: 'This deletes the project, its items, versions and history. Type the project name to confirm.',
  label: 'Project name', confirmLabel: 'Delete project', danger: true,
  validate: value => value === project.name ? null : 'The name does not match.',
})
if (typed === null) return
```

5. **The other dialogs** (collab.js, curation.js): each `confirm(` becomes `await OF.ui.confirm({...})`, and each `alert(` becomes `OF.ui.toast(..., {kind: 'danger'})`.
6. **Native-dialog check.** No `confirm(`, `prompt(` or `alert(` calls remain anywhere in `static/**` outside `vendor/`. `OF.ui.confirm(`, `OF.ui.prompt(`, `ui.confirm` and `ui.prompt` inside `ui.js`, and `window.OF.ui.*` are fine.

- [ ] **Step 1: Write the failing test** in `tests/test_ui_dialogs.py`

```python
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
    for name in ("curation/curation.js", "curation/editor.js", "curation/collab.js"):
        source = (STATIC / name).read_text(encoding="utf-8")
        assert "async function confirmDiscardPending" in source or name != "curation/editor.js"
        bare = re.findall(r"(?<!await )(?<!function )confirmDiscardPending\(\)", source)
        bare = [b for b in bare if "typeof confirmDiscardPending" not in b]
        assert not bare, f"{name}: confirmDiscardPending() called without await"
```

Note that `NATIVE` deliberately ignores `.confirm(`, which excludes `OF.ui.confirm(` and `ui.confirm(`, and catches only bare or `window.` calls. If the "without await" check trips on a legitimate `typeof confirmDiscardPending === 'function' && !(await confirmDiscardPending())` line, the lookbehind already passes it because `await ` comes first. Fix the code, not the test.

- [ ] **Step 2: Run** TEST `tests/test_ui_dialogs.py`. It should FAIL, listing the current native calls.
- [ ] **Step 3: Implement** rules 1–5.
- [ ] **Step 4: Run** TEST `tests/test_ui_dialogs.py tests/test_ui_integrity.py tests/test_curation.py`, then the full suite and JS SYNTAX. All should PASS.
- [ ] **Step 5: Browser check (Review Focus 3).** Deploy the app container. In `tests/browser/p3a_browser_check.py`, replace `page.on("dialog", lambda d: d.accept())` with sheet clicks, and add:
  1. Make an edit, press Alt+→, and in the sheet click "Keep editing" (`[data-sheet="cancel"]`). The image is unchanged and the tray still shows 1.
  2. Press Alt+→ again and click "Discard" (`[data-sheet="confirm"]`). The image changes and the tray is empty.
  3. Repeat the cancel case through the Back button, after opening from the crop grid. The view stays `source`.

  Run it as in Task 7.
- [ ] **Step 6: Commit**

```bash
git add static tests/test_ui_dialogs.py tests/browser/p3a_browser_check.py
git commit -m "Sheets replace every native dialog; the unsaved-edits guard is async and awaited on every path"
```

---

### Task 10: Command palette providers

**Files:**
- Modify: `static/curation/curation.js` (or a small `static/curation/palette.js` loaded after collab.js, added to the curation `PAGES` script list), `static/library/app.js`
- Test: `tests/test_ui_integrity.py`

**Rules:**
- **Curation registers three providers** with `window.OF.ui.palette.register` after init:
  1. **Projects:** for each project in `state.projects`, and each view in Source / Crops / Quality / Matrix / Status / History, the label is `${project.name} · ${ViewLabel}` and the action awaits `switchProject(project.id)` (if different), then calls `setView(view)`. Limit 12.
  2. **Images** (`filter: false`):
     - When the query has 3 or more characters, call `GET /api/curation/projects/${state.projectId}/source-images?image_search=${encodeURIComponent(q)}&limit=8&source_missing=any` with the existing `request()`.
     - Each result is labelled with the image name, hinted with region and year, and runs `openSource(image.first_item_id, state.view)`.
     - For fewer than 3 characters, return `[]`.
     - Responses for stale queries are ignored by `ui.js`'s query token.
  3. **Curation actions:**
     - Check for missing images: POST availability, as the status card does. Shown only if `canDo('project-curator')`.
     - Toggle "Hide missing images".
     - Commit changes: opens the commit dialog through the existing pill click.
     - Open dataset status.
- **Library registers "Library actions":** New collection (`/collections/new`), Datasets tab, and Focus search (focuses the search input).
- **No new endpoints.** Check that `image_search` is an accepted query parameter on the source-images route (it is part of `_filters`). If its name differs, use the real one.

- [ ] **Step 1: Write the failing test**

```python
def test_pages_register_palette_providers():
    curation = "".join((STATIC / "curation" / f).read_text(encoding="utf-8") for f in ("curation.js", "collab.js", "editor.js") if (STATIC / "curation" / f).exists())
    if (STATIC / "curation" / "palette.js").exists():
        curation += (STATIC / "curation" / "palette.js").read_text(encoding="utf-8")
    assert curation.count("OF.ui.palette.register(") >= 3
    assert "image_search=" in curation
    library = "".join(p.read_text(encoding="utf-8") for p in (STATIC / "library").glob("*.js"))
    assert "OF.ui.palette.register(" in library
```

Run TEST `tests/test_ui_integrity.py`. It should FAIL.
- [ ] **Step 2: Implement** the rules above.
- [ ] **Step 3: Run** TEST `tests/test_ui_integrity.py`, then the full suite and JS SYNTAX. All should PASS.
- [ ] **Step 4: Commit**

```bash
git add static tests/test_ui_integrity.py
git commit -m "Command palette: projects and views, images by name, curation and library actions"
```

---

### Task 11: Responsive pass

**Files:**
- Modify: `static/ui/shell.css`, `static/curation.css`, `static/library.css`, `static/app.css`, `static/auth.css`
- Test: `tests/test_ui_integrity.py` (static check); the Task 12 browser tour covers the visuals

**Rules:**
- **Every page at 360 px has no horizontal page scroll.** Wide tables and grids scroll inside their own container (`overflow-x: auto`), never the page.
- **Curation, 900 px and below:**
  - the inspector becomes a bottom sheet-style panel under the stage;
  - the queue collapses by default (the `queue-collapsed` class);
  - the stage keeps at least 50 vh.
- **Curation, 640 px and below:**
  - the view tabs scroll horizontally in the top bar;
  - the filter chip row scrolls horizontally;
  - the floating selection bar and the tray pill span the width minus 16 px gutters;
  - the stage toolbar and stepping stay usable with 36 px touch targets.
- **Library and Frame Analyser:** grids reflow to 2 columns at 390 px, and panels stack.
- **Rail:** the shell rules already in `shell.css` from Task 2 (icon-only at 900 px or less, bottom tab bar at 640 px or less) are verified. The bottom bar doesn't cover content (`.app-main` bottom padding) or the floating pills (offset by the bar height).

- [ ] **Step 1: Write the failing test**

```python
def test_responsive_rules_exist():
    for name in ("ui/shell.css", "curation.css", "library.css", "frames.css"):
        css = (STATIC / name).read_text(encoding="utf-8")
        assert "@media (max-width: 900px)" in css or "@media (max-width:900px)" in css, name
        assert "@media (max-width: 640px)" in css or "@media (max-width:640px)" in css, name
```

Run TEST `tests/test_ui_integrity.py`. It should FAIL for the files that lack the breakpoints.
- [ ] **Step 2: Implement** the rules above.
- [ ] **Step 3: Run** TEST `tests/test_ui_integrity.py`, the full suite, and JS SYNTAX. All should PASS.
- [ ] **Step 4: Commit**

```bash
git add static
git commit -m "Responsive: icon rail and bottom tab bar, curation inspector under the stage, page grids reflow, no horizontal scroll at 360 px"
```

---

### Task 12: Cleanup, browser tour, docs, full verification, deployment

**Files:**
- Delete: `templates/_header.html`, `templates/_user_menu.html` (once nothing includes them), `static/img/optics-si.png` (if no longer referenced)
- Create: `tests/browser/ui_tour.py`, `tests/browser/p2_browser_check.py` (copied from the P2 session scratch, adapted to sheets), `tests/browser/README.md`
- Modify: `docs/CURATION.md` (UI section), `README.md` or `docs/IMAGE_LIBRARY.md` (a screenshot reference is optional), and every remaining stylesheet that has dead legacy rules

**Steps:**

- [ ] **Step 1: Remove the old header.**
  - `grep -rn "_header.html\|_user_menu.html\|optics-si.png" templates static` should return nothing that is still in use. Delete those files.
  - Delete the CSS rules for removed classes (`.site-header`, `.header-*`, `.nav-link` if unused, `.team-badge`) from every stylesheet. Check each with `grep -rn "<classname>" templates static` before deleting.
- [ ] **Step 2: Browser tour** (`tests/browser/ui_tour.py`). Using Playwright, signed in as tester2 with the password from `OF_PW2`:
  1. Visit `/`, `/collections`, `/collections/new`, `/curation` (NOAA project, then the source view with an image loaded), `/frames` and `/account`. Visit them in dark at 1440×900, then in light by setting theme light through the avatar menu, so the persistence path is exercised. Then visit the curation source view in dark at 390×844.
  2. On every page, assert there are no `pageerror` and no `console.error` events, ignoring 404s for crop previews of known-missing images. Screenshot each page to `tests/browser/shots/` (git-ignored).
  3. Assert that the theme persists across reload: `document.documentElement.dataset.theme === 'light'` after reload.
  4. Assert that Ctrl+K opens `#of-palette`. Typing "crops" then Enter lands on the curation crops view of a project.
  5. Assert that the login-off path is not applicable in deployment. Review Focus 5 is covered by the rendered integrity tests with login off.

  Add `tests/browser/shots/` to `.gitignore`.
- [ ] **Step 3: Docs.** Add a "Using the app" section to `docs/CURATION.md` covering:
  - the shell (rail, top bar, ⌘K palette, theme in the avatar menu);
  - the curation layout (filters popover and chips, queue, stage toolbar, floating unsaved-edits pill, inspector tabs);
  - keyboard shortcuts: ⌘K, plus the existing editor keys.

  Describe what the code does.
- [ ] **Step 4: Full verification.**
  - Run the full offline suite.
  - Run the full Oracle suite. The known environment-only `test_text_search_without_clip_falls_back_and_says_so` failure is expected and recorded.
  - Run JS SYNTAX.
- [ ] **Step 5: Deploy and check.**
  1. Deploy the app container.
  2. Run `tests/browser/ui_tour.py`, `tests/browser/p3a_browser_check.py` and `tests/browser/p2_browser_check.py` through the Playwright container. They must pass with no unexpected console errors.
  3. Read every screenshot and fix anything visibly broken: overlaps, unreadable text, misaligned rail, or controls clipped at 390 px.
- [ ] **Step 6: Commit**

```bash
git add -A templates static tests/browser docs .gitignore
git status --short   # confirm nothing from .superpowers/, .env, docs/*.png is staged
git commit -m "Remove the old header; browser tour in both themes; UI docs"
```
