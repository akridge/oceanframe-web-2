"""Design tokens: both themes exist and meet WCAG contrast."""
from __future__ import annotations

import re
from pathlib import Path

TOKENS = Path(__file__).resolve().parents[1] / "static" / "ui" / "tokens.css"


def _css() -> str:
    return re.sub(r"/\*.*?\*/", "", TOKENS.read_text(encoding="utf-8"), flags=re.S)


def _blocks(name: str) -> str:
    """Every block whose selector list ends in :root[data-theme="name"], merged."""
    found = re.findall(r':root\[data-theme="%s"\]\s*\{(.*?)\n\}' % name, _css(), re.S)
    assert found, f"no {name} theme block"
    return "\n".join(found)


def _theme(name: str) -> dict[str, str]:
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", _blocks(name)))


def _declared(name: str) -> set[str]:
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", _blocks(name)))


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


def test_every_theme_declares_every_token_including_scrim_and_shadows():
    dark, light = _declared("dark"), _declared("light")
    assert dark == light
    for name in ("--scrim", "--shadow-pop", "--shadow-sheet"):
        assert name in dark and name in light, name
    # Hex tokens must be plain #rrggbb so the contrast test cannot silently skip one.
    assert {n for n in dark if n not in _theme("dark")} == {"--scrim", "--shadow-pop", "--shadow-sheet"}


def test_dark_tokens_apply_when_data_theme_is_absent():
    selectors = re.findall(r"([^{}]+)\{[^{}]*--bg:", _css())
    dark = [s for s in selectors if 'data-theme="dark"' in s]
    assert dark and ':not([data-theme="light"])' in dark[0]


def test_all_legacy_and_alias_names_are_defined():
    css = _css()
    names = ("--border --text-muted --primary --radius-sm --surface-2 --surface --primary-light --radius --font-mono "
             "--text --primary-dark --text-light --error --success --shadow-sm --radius-lg --error-bg --success-bg "
             "--shadow-md --accent --shadow-lg --shadow --border-focus --bg --warning-bg --warning --font").split()
    assert len(names) == 27
    for n in names:
        assert re.search(r"(?<![\w-])%s\s*:" % re.escape(n), css), n
