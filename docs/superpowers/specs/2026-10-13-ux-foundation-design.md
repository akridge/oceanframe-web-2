# UX foundation: a polished, Apple-grade shell and design system across OceanFrame

**Status:** design approved (2026-10-13; shell, curation workspace and components were reviewed visually in the brainstorm companion). The user delegated the spec and plan review to the advisor and asked for automatic continuation.
**Roadmap position:** first of the remaining phases. P3b mining comes next and is built on these components. P4 library↔curation, P3c FiftyOne and P5 detection/segmentation follow. This phase takes over P4's "one navigation" item.

## Goal

Every OceanFrame page should look and behave like one high-polish product. It gets a dark "pro workspace" look (graphite surfaces, system-blue accent) with a matching light theme, one shared shell, one component library and native-feeling feedback. No feature or workflow is removed, and every existing JavaScript hook keeps working.

## Decisions

| Topic | Decision |
|---|---|
| Direction | **Dark pro workspace** (Final Cut / Lightroom feel): icon rail for apps, tabs for views, inspector panels. |
| Palette | **Graphite + system blue.** Neutral greys carry the chrome so the imagery and the label colours carry the screen. |
| Themes | **Dark by default.** A light theme uses the same tokens. Follows the OS until the user picks Dark / Light / System in the user menu, and the choice is remembered. |
| Scope | **Shell plus every page:** Library, Collections, Collection form, Curation, Frame Analyser, Account, Users, Login. Curation gets the deepest pass. Other pages get the shell and components without workflow redesign. |
| Architecture | **Own design system in plain CSS and JS**, no framework and no build step. A shared base template, classic scripts as today. |
| Branding | The app icon stays the **OceanFrame wave mark**, refined for the dark rail. The **OPTICS SI logo** (`docs/optics_si_logo_v1 (1).png`, copied into `static/img/`) is the "an app by OPTICS SI" credit, shown on the sign-in page and at the foot of the user menu. It replaces `static/img/optics-si.png` in that role. |

## Non-negotiable constraints

1. **No JS hook is renamed or removed.** Markup may move, be wrapped and be restyled. Every `id`, `data-*` attribute, `name` and class that scripts read or toggle keeps its name and meaning:
   - about 364 ids read through `byId`/`getElementById` (342 in curation, 22 in auth);
   - about 233 `$()`/`querySelector('#…')` lookups in library, modules and app.js;
   - 47 distinct `[data-…]` selectors;
   - the toggled utility classes `d-none`, `hidden`, `active`, `selected`, `is-error`, `is-invalid`, `is-loading`, `loaded`, `busy`, `stuck`, `role-locked`, `required`, `lasso-hit`, `lasso-active`, `drag-over`, `text-danger`.

   `d-none` and `hidden` keep meaning `display: none !important`. The Bootstrap-like class names (`btn`, `btn-sm`, `btn-primary`, `btn-outline-*`, `form-select`, `form-control`, `form-label`) are restyled by the component layer; no Bootstrap stylesheet is loaded.
2. **Integrity is checked on rendered pages.** Every page is rendered through the TestClient. Its HTML is checked for:
   - every id its scripts look up;
   - no duplicate ids, including ids that come from partials and the base template.
3. **No native `confirm()`, `prompt()` or `alert()`** remains in `static/**` outside `vendor/`; the only exception is the `beforeunload` guard, which must stay native.
4. **Theme before paint.** An inline script in `<head>`, placed before any stylesheet, sets `data-theme` on `<html>`, so there is no flash of the wrong theme.
5. **Cache-busting.** Every first-party CSS and JS URL carries `?v=<app version>`, so a deploy never pairs new HTML with stale assets.
6. **Accessibility:**
   - text and icon contrast of at least 4.5:1 (3:1 for large text and UI borders) in both themes, checked by a test that reads the tokens;
   - a visible focus ring on every interactive element;
   - menus, sheets and the palette fully keyboard-operable;
   - all motion disabled under `prefers-reduced-motion`.
7. **Oracle, Docker, safety rules unchanged.** This phase adds no database schema changes and no new endpoints.

## 1. Design tokens and base (`static/ui/tokens.css`, `static/ui/base.css`)

**Tokens:** CSS custom properties on `:root[data-theme="dark"]` and `:root[data-theme="light"]`.

**Colour:**

| Token | Dark | Light |
|---|---|---|
| `--bg` | `#1c1c1e` | `#f5f5f7` |
| `--bg-rail` | `#111113` | `#e8e8ed` |
| `--surface-1` | `#2c2c2e` | `#ffffff` |
| `--surface-2` | `#3a3a3c` | `#e8e8ed` |
| `--surface-3` | `#48484a` | `#d2d2d7` |
| `--line` | `#38383a` | `#d2d2d7` |
| `--text` | `#f5f5f7` | `#1d1d1f` |
| `--text-2` | `#c7c7cc` | `#424245` |
| `--text-3` | `#98989f` | `#6e6e73` |
| `--accent` (selection, focus, outlines) | `#0a84ff` | `#0071e3` |
| `--accent-fill` (primary button fill, white text) | `#0071e3` | `#0071e3` |
| `--accent-text` | `#64b5ff` | `#0066cc` |
| `--ok` | `#30d158` | `#1f8a3a` |
| `--warn` | `#ff9f0a` | `#b25e00` |
| `--danger` | `#ff453a` | `#d70015` |
| `--danger-fill` (danger button fill, white text) | `#d70015` | `#d70015` |
| `--focus` | `color-mix(in srgb, var(--accent) 45%, transparent)` | same formula |
| `--scrim` | `rgba(0,0,0,.5)` | `rgba(0,0,0,.3)` |

**Type:**
- `--font: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable", "Segoe UI", Inter, system-ui, sans-serif`
- `--font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace`
- sizes 11 / 12 / 13 (body) / 15 / 17 / 22 / 28 px; weights 400 / 590 / 700; `font-variant-numeric: tabular-nums` on tables and counters.

**Space, shape, elevation, motion:**
- space: 2, 4, 6, 8, 12, 16, 20, 24, 32, 48
- radii: `--r-control: 7px`, `--r-tile: 9px`, `--r-panel: 12px`, `--r-sheet: 14px`, `--r-pill: 999px`
- elevation: `--shadow-pop` (menus, toasts), `--shadow-sheet`, plus hairline borders, with no heavy shadows on resting surfaces
- motion: `--ease: cubic-bezier(.2,.8,.2,1)`, `--t-fast: 150ms`, `--t: 200ms`, `--t-slow: 250ms`; all set to `0ms` under reduced motion
- glass: `--glass: color-mix(in srgb, var(--bg) 72%, transparent)` with `backdrop-filter: saturate(180%) blur(20px)` for the top bar, rail and floating trays; there is a solid fallback where `backdrop-filter` is unsupported

**Base:**
- a modern reset
- body type, links and `::selection`
- scrollbars styled thin and theme-aware
- the `:focus-visible` ring (`box-shadow: 0 0 0 3px var(--focus)`)
- utilities `d-none` and `hidden` (`display: none !important`), `visually-hidden`, `text-danger`, `muted`
- the existing label colours are untouched

## 2. Components (`static/ui/components.css`)

| Component | Notes |
|---|---|
| **Button** | `.btn`, `.btn-sm`, `.btn-primary`, `.btn-outline-*` mapped to: neutral (surface-2), primary (`--accent-fill` with white text), ghost (accent text), danger (danger text, or a danger fill in sheets). Disabled at 40% opacity. Busy state shows a spinner and keeps the width. |
| **Segmented control** | `.segmented`, with buttons inside. The current view tabs and grid-size toggles become segmented controls or tabs. |
| **Tabs** | `.tabs`, used for app views in the top bar, with `aria-selected`. |
| **Inputs** | `.form-control`, `.form-select`, `input`, `select`, `textarea`: surface-1, hairline border, accent focus ring. Checkboxes and radios use `accent-color`. A switch style is available for boolean settings. |
| **Chip** | `.chip` (neutral), `.chip-active` (accent tint, with ✕ to remove). Used for active filters. |
| **Badge** | `.badge-ok`, `.badge-warn`, `.badge-danger`, `.badge-neutral`: status only. |
| **Tile** | `.tile` (crop and image tiles): radius 9, hairline border, `.selected` gets an accent outline, label pill over the image. |
| **Panel / card** | `.panel` (surface-1, radius 12) and `.panel-title`. |
| **Table** | `.table`: sticky header, tabular numbers, row hover, zebra off. |
| **Menu / popover** | `.popover`: glass, radius 12, `--shadow-pop`, arrow-key navigation, closes on Esc or an outside click. |
| **Sheet** | A modal dialog (`<dialog class="sheet">`): radius 14, scrim, focus trapped, Esc cancels, the primary button is the default. |
| **Toast** | Bottom-centre stack, glass, auto-dismisses after 4 s (errors stay until closed), an optional action button. |
| **Skeleton** | `.skeleton` shimmer blocks for tiles, rows and the image stage while loading; no shimmer under reduced motion. |
| **Empty state** | `.empty-state`: title, one line of help, an optional action. |
| **Progress** | Thin linear bar and a spinner. |

## 3. Shell (`templates/base.html`, `static/ui/shell.css`)

**Base template.** Every page extends `base.html`. It provides:
- the head: the theme script, then tokens, base, components and shell CSS, then the page CSS and `ui.js`, all versioned;
- the **rail**;
- the **top bar** slot;
- the main content block;
- the toast host;
- the sheet host;
- the palette.

Blocks: `title`, `page_css`, `topbar`, `content`, `page_scripts`, plus `body_class`. The sign-in page uses a minimal variant with no rail.

**Rail.** It is 64 px wide, `--bg-rail`, and always in this order:
1. Library
2. Collections
3. Curation
4. Frame Analyser
5. a spacer
6. Users (admins only, the same rule as today)
7. the avatar button

The active app shows an accent icon on a surface-1 pill, with `aria-current="page"`. Icons come from the vendored bootstrap-icons. The app icon at the top is the OceanFrame wave mark (inline SVG) on an accent gradient tile.

**User menu.** A popover from the avatar. It shows:
- name and role;
- Account;
- Theme (Dark / Light / System, as a segmented control);
- Sign out;
- at the foot, "an app by" with the OPTICS SI logo.

`static/auth/menu.js` keeps its role, and `#user-menu` stays the mount point. It renders into the new popover.

**Top bar.** It belongs to each page, is 44 px high, glass, with a hairline bottom border. Page-specific actions that used to sit in the header move here:
- Library: Datasets, My collections, + Add collection;
- Frame Analyser: its section anchors;
- Curation: project picker, view tabs, uncommitted pill.

**Responsive:**
- below 900 px the rail shows icons only, with labels in tooltips;
- below 640 px the rail becomes a bottom tab bar, and the top bar wraps its tabs into a scrollable row;
- the layout never scrolls horizontally at 360 px width.

## 4. `ui.js` (`window.OF.ui`)

A classic script, loaded by the base template before page scripts, so both module pages (library, auth) and classic pages (curation, frame analyser) can use it. It provides:

| API | What it does |
|---|---|
| `OF.ui.theme.get()` / `set('dark'\|'light'\|'system')` | Persists in `localStorage` (wrapped in try/catch) and updates `data-theme` live. In `system` mode it listens to `prefers-color-scheme`. |
| `OF.ui.toast(message, {kind: 'ok'\|'warn'\|'danger'\|'info', action: {label, onClick}, timeout})` | Shows a toast. |
| `OF.ui.confirm({title, message, confirmLabel, cancelLabel, danger}) → Promise<boolean>` | A confirmation sheet. |
| `OF.ui.prompt({title, message, label, value, confirmLabel, validate}) → Promise<string\|null>` | A sheet with a text field. Supports a "type the project name to delete" check through `validate`. |
| `OF.ui.popover(anchor, contentEl)` | Menus and popovers. |
| `OF.ui.palette.register(provider)` | Command palette providers, described below. |

**Command palette.** It opens with ⌘K / Ctrl+K anywhere (not while typing in a field unless the palette is already open) and from a top-bar button. It shows fuzzy-matched groups:

| Group | Provider | Source |
|---|---|---|
| Go to | shell | the apps |
| Projects | curation | each project × view, from the already-loaded project list |
| Images | curation | images by name, using the existing `image_search` item filter on the current project, debounced, at most 8 results |
| Actions | each page | e.g. curation: Check missing images, Commit changes, Toggle "Hide missing"; shell: Switch theme, Sign out |

It uses no new endpoints.

## 5. Native dialogs become sheets

Every `confirm`/`prompt`/`alert` in `static/**` changes. Today there are 16 calls: `curation.js` 4, `collab.js` 1, `editor.js` 1, `library/admin.js` 3, `library/datasets.js` 1, `modules/export.js` 4, `auth/users.js` 1, plus any `alert`. Each becomes an `OF.ui` call, with the same wording, rewritten to read well as a sheet.

**The guard chain becomes async.**
- `confirmDiscardPending()` becomes `async` and returns `Promise<boolean>`.
- Every caller awaits it: `setView`, `selectImage`, `switchProject`, the hashchange handler, `addManualPoint`, project create and delete, hub create and link, drilldown clear, image stepping, and the Back button.
- Where a caller was synchronous, it becomes async, and its own callers are checked in turn.
- `setView` keeps returning a promise, now resolving after the guard.

The `beforeunload` guard stays native, because browsers allow nothing else. The project-delete name confirmation becomes `OF.ui.prompt` with a `validate` that requires the exact name.

**Browser-check scripts.** The P2 and P3a browser-check scripts click sheet buttons instead of auto-accepting native dialogs. Copies of both scripts live in `tests/browser/`, so the repo keeps them.

## 6. Curation workspace

These are the deepest changes. The hooks stay; the layout and visuals change.

| Area | Change |
|---|---|
| **Top bar** | Project picker (the existing project select, styled as a picker button with a popover list and search), view tabs (the existing view buttons), the uncommitted-changes pill (existing), and the palette button. |
| **Filters** | The filter controls move into one **Filters popover**, opened from a "Filters" button. The controls keep their existing elements and ids, just moved. Active filters show as removable chips in a filter bar under the top bar: region, island, year, site, annotator, origin, state, inclusion, taxonomy and codes. The "Hide missing images" and "Show removed" checkboxes become chips that toggle the same checkboxes. A count sits on the right. |
| **Queue** | The left column gets thumbnails (the image proxy at a small size, lazy-loaded), name, points count and progress, with a collapse button. |
| **Image stage** | Gets an **on-canvas toolbar**: add point, bulk add, fit and zoom, and image stepping "‹ 12 / 3,104 ›" in the top-right corner. |
| **Unsaved-edits tray** | A floating **pill** at the bottom centre of the stage: "N unsaved edits · 1 moved · 2 added · 1 removed", with Review, Discard and Save N. Review expands the existing tray list as a popover above the pill. |
| **Inspector** | The right column gets three tabs:<br>**Point:** crop preview, label, patch size and presets, state, suggestions, Exclude / Remove / Restore, bulk-add form.<br>**Image:** metadata, missing notice, annotation table.<br>**Model:** classify point and results, evaluation context. |
| **Crops, Quality, Matrix, Status, History, Compare, Releases** | Same top bar. Content uses tiles, tables, chips, panels and sheets, with bulk bars restyled as a floating selection bar ("3 selected · Include · Exclude · Relabel to…"). Charts and split bars use theme tokens. |

**Overlay rule.** The on-canvas toolbar, image stepping and the floating pill are excluded from the image pointer handlers, which today skip only `.source-marker`. They also stop pointer events from propagating, so clicking Save can never pan the image or add a point.

**Resize rule.** A `ResizeObserver` on `#source-image-wrap` calls `renderMarkers()` and re-applies the transform whenever the stage changes size. That covers queue collapse, inspector tab changes and window resize, so markers never drift off their points.

## 7. Other pages

| Page | Change |
|---|---|
| **Library** (`library.html`, `library.css`) | Top bar with search, All/Datasets tabs and collection actions. Filters in a popover with chips. The grid uses `.tile`. Detail and lightbox use a sheet-style overlay on the dark surface. The job panel uses progress components. |
| **Collections, Collection form** | Panels, tables and inputs. Destructive actions use sheets. |
| **Frame Analyser** (`index.html`, `app.css`, `static/modules/*`) | Top bar with its section anchors as tabs. Upload, parameter, frame and export sections become panels. Chart.js gets theme-aware colours read from the tokens, re-rendered on theme change. |
| **Account, Users** | Panels, tables and inputs. User-management confirms become sheets. |
| **Login** | Minimal centred card on the graphite background, the OceanFrame mark, the OPTICS SI credit below the form, and theme-aware styling. |

`app.css`, `library.css`, `curation.css` and `auth.css` are rewritten onto the tokens. Hard-coded colours are replaced, and dead rules are removed.

## 8. Testing and verification

| Kind | What it checks |
|---|---|
| **Rendered integrity** (new `tests/test_ui_integrity.py`) | For each page route (`/`, `/collections`, `/collections/new`, `/curation`, `/frames`, `/account`, `/users` as admin, `/login`): render through the TestClient, then assert:<br>• the base shell is present (rail, the five apps in order, the correct `aria-current`);<br>• no duplicate ids;<br>• every id that page's scripts look up through `byId`, `getElementById`, `$('#…')` or `querySelector('#…')` exists in the rendered HTML, except ids the scripts create themselves, which are listed in an explicit allow-list in the test.<br>This replaces the curation-only checks and keeps their intent. |
| **No native dialogs** | A test greps `static/**/*.js`, excluding `vendor/`, for `confirm(`, `prompt(` and `alert(` calls. Only the `OF.ui` methods and the `beforeunload` line are allowed. |
| **Theme bootstrap** | Every rendered page has the inline theme script before its first stylesheet, and versioned asset URLs. |
| **Contrast** | A test parses `tokens.css` for both themes and asserts the WCAG ratios: text on bg and surface-1 ≥ 4.5; text-3 on bg ≥ 4.5; accent-text on bg ≥ 4.5; line on bg ≥ 1.3 (decorative); `--accent-fill` with white text ≥ 4.5; `--accent` against bg ≥ 3 (UI component contrast). |
| **JS syntax** | `node --check` on every first-party JS file. |
| **Existing suites** | Offline and Oracle suites stay green. |
| **Browser tour** (Playwright, run at deploy time and copied to `tests/browser/`) | Every page in dark and light at 1440×900, plus the curation source view at 390×844. Checks: no `pageerror`, no `console.error`, screenshots reviewed by eye, the P3a editor flow (move, resize, remove, bulk add, save through the sheet guard), the P2 collaboration checks, theme switching persisting across reload, ⌘K opening the palette and jumping to a project view. |

## Out of scope

- Workflow redesign of Library and Frame Analyser, beyond the shell and components.
- Any new data features, which belong to P3b and later.
- Internationalisation.
- A CSS framework or build step.
- Self-hosted fonts: the system font stack covers macOS, Windows and Linux.
