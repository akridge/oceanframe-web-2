"""
The benthic taxonomy tree, flattened for filtering and label projection.

The tree runs TIER0 → TIER_1 → TIER_2 → TIER_2b → TIER_3 → CODE. A project is
curated at one tier — T3 (fine-grained classes) or T1 (broad functional
groups) — and every raw annotation code is projected to its single ancestor at
that tier.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from curation import settings


def _split_name(value: str) -> tuple[str | None, str]:
    text = value.strip()
    if " - " not in text:
        return (text if text and " " not in text else None), text
    code, label = text.split(" - ", 1)
    return code.strip() or None, label.strip()


def _descendant_codes(node: dict[str, Any]) -> list[str]:
    if node.get("tier") == "CODE":
        code = str(node.get("name") or "").strip().lstrip("*")
        return [code] if code else []
    codes: list[str] = []
    for child in node.get("children") or []:
        codes.extend(_descendant_codes(child))
    return list(dict.fromkeys(codes))


def _flatten(node: dict[str, Any], path: tuple[int, ...], depth: int) -> list[dict]:
    tier = str(node.get("tier") or "UNKNOWN")
    raw_name = str(node.get("name") or tier)
    code, label = _split_name(raw_name)
    rows = []
    if tier != "CODE":
        rows.append({
            "key": ".".join(map(str, path)) or "root",
            "tier": tier,
            "code": code,
            "name": raw_name,
            "label": label,
            "codes": _descendant_codes(node),
            "depth": depth,
        })
    for index, child in enumerate(node.get("children") or []):
        rows.extend(_flatten(child, (*path, index), depth + 1))
    return rows


@lru_cache(maxsize=4)
def _load_cached(path: str, modified_ns: int) -> list[dict]:
    del modified_ns  # part of the cache key, so an edited file is re-read
    with open(path, "r", encoding="utf-8") as stream:
        return _flatten(json.load(stream), (), 0)


def load_taxonomy_nodes() -> list[dict]:
    path = settings.taxonomy_file()
    if not path.is_file():
        return []
    return _load_cached(str(path.resolve()), path.stat().st_mtime_ns)


def available() -> bool:
    return bool(load_taxonomy_nodes())


def project_code(code: str, dataset_mode: str) -> str | None:
    """Project a raw annotation code to its T1 or T3 ancestor; None if absent or ambiguous."""
    normalized = code.strip().lstrip("*")
    target_tier = "TIER_1" if dataset_mode.upper() == "T1" else "TIER_3"
    nodes = load_taxonomy_nodes()
    direct = [node for node in nodes if node["tier"] == target_tier and node["code"] == normalized]
    if len(direct) == 1:
        return normalized
    matches = [
        node for node in nodes
        if node["tier"] == target_tier and normalized in node["codes"] and node["code"]
    ]
    codes = list(dict.fromkeys(str(node["code"]) for node in matches))
    return codes[0] if len(codes) == 1 else None


def effective_label(raw: str, dataset_mode: str) -> str:
    """The label an imported annotation starts with in a project of this tier."""
    projected = project_code(raw, dataset_mode)
    if projected:
        return projected
    # T1 is a closed set of broad groups; anything unplaceable is UC
    # (unclassified). T3 keeps the raw code rather than inventing one.
    return "UC" if dataset_mode.upper() == "T1" else raw.strip().lstrip("*")


def validate_label(raw: str, dataset_mode: str) -> str:
    """A curator-entered label, normalised; ValueError if the taxonomy rejects it.

    With no taxonomy file installed there is nothing to check against, so the
    label is accepted as typed rather than rejecting every edit.
    """
    label = raw.strip().lstrip("*").strip()
    if not label:
        raise ValueError("Label is required")
    if not available():
        return label
    projected = project_code(label, dataset_mode)
    if projected is None:
        raise ValueError(f"{label!r} is not a {dataset_mode.upper()} label in the taxonomy")
    return projected
