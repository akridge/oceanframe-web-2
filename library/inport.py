"""
NOAA InPort metadata lookup.

Only ever fetches ``https://www.fisheries.noaa.gov/inport/item/<id>/inport-xml``:
the user supplies an item URL or id, never a URL we follow, so this cannot be
pointed at an internal address.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as StdET

from defusedxml import ElementTree as ET

HOST = "www.fisheries.noaa.gov"
ITEM_URL = f"https://{HOST}/inport/item/{{id}}"
XML_URL = f"https://{HOST}/inport/item/{{id}}/inport-xml"
TIMEOUT = 15
MAX_BYTES = 8 * 1024 * 1024
MAX_ABSTRACT = 8000

_URL_RE = re.compile(
    r"^https://(?:www\.)?fisheries\.noaa\.gov/inport/item/(\d{1,9})(?:/[A-Za-z0-9\-]*)?/?(?:[?#].*)?$"
)
_ID_RE = re.compile(r"^\d{1,9}$")
_YEAR_RE = re.compile(r"\b(1[89]\d\d|2[01]\d\d)\b")


class InportError(Exception):
    """A lookup failed in a way worth showing the user verbatim."""


class InportUrlError(InportError):
    """The user's input is not an InPort item URL or id."""


def parse_item_id(value: str) -> int:
    value = (value or "").strip()
    if _ID_RE.match(value):
        return int(value)
    match = _URL_RE.match(value)
    if not match:
        raise InportUrlError(
            "Expected an InPort item URL like https://www.fisheries.noaa.gov/inport/item/76360 "
            "or just the item number."
        )
    return int(match.group(1))


def item_url(item_id: int) -> str:
    return ITEM_URL.format(id=int(item_id))


def fetch(item_id: int) -> bytes:
    url = XML_URL.format(id=int(item_id))
    request = urllib.request.Request(url, headers={"User-Agent": "OceanFrame/1.0", "Accept": "application/xml"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 - fixed https host
            if urllib.parse.urlparse(response.geturl()).hostname != HOST:
                raise InportError("InPort redirected somewhere unexpected; refusing to follow.")
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise InportError(f"InPort has no item {item_id}.") from exc
        raise InportError(f"InPort returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise InportError(f"Could not reach InPort: {exc}") from exc
    if len(body) > MAX_BYTES:
        raise InportError("InPort record is unexpectedly large.")
    return body


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(node) -> str:
    return " ".join((node.text or "").split()) if node is not None else ""


def _child(node, name: str):
    if node is None:
        return None
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _children(node, name: str) -> list:
    return [c for c in node if _local(c.tag) == name] if node is not None else []


def _child_text(node, name: str) -> str:
    return _text(_child(node, name))


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse(xml_bytes: bytes) -> dict:
    """Pull the fields a collection page cares about out of an InPort XML record."""
    try:
        root = ET.fromstring(xml_bytes)
    except (StdET.ParseError, ValueError) as exc:
        raise InportError(f"InPort returned XML that could not be parsed: {exc}") from exc

    ident = _child(root, "item-identification")
    item_id = _child_text(ident, "catalog-item-id")

    keywords: list[str] = []
    for wrapper in _children(_child(root, "keywords"), "keyword"):
        inner = _child(wrapper, "keyword")
        text = _text(inner if inner is not None else wrapper)
        # GCMD keywords are "A > B > C"; the leaf is the useful bit.
        text = text.split(" > ")[-1].strip() if " > " in text else text
        if text and text not in keywords:
            keywords.append(text)

    time_frames: list[dict] = []
    bbox: dict | None = None
    areas: list[str] = []
    extents = _child(root, "extents")
    for node in (extents if extents is not None else []):
        for sub in node.iter():
            name = _local(sub.tag)
            if name == "time-frame":
                start = _child_text(sub, "start-date-time") or _child_text(sub, "time-frame-date")
                end = _child_text(sub, "end-date-time")
                if start or end:
                    time_frames.append({
                        "start": start, "end": end, "description": _child_text(sub, "description"),
                    })
            elif name == "geographic-area":
                bounds = {k: _float(_child_text(sub, f"{k}-bound")) for k in ("west", "east", "north", "south")}
                if bbox is None and all(v is not None for v in bounds.values()):
                    bbox = bounds
                description = _child_text(sub, "description")
                if description and description not in areas:
                    areas.append(description)

    contacts: list[dict] = []
    for role in _children(_child(root, "support-roles"), "support-role"):
        name = _child_text(role, "contact-name")
        if name:
            contacts.append({
                "role": _child_text(role, "support-role-type"),
                "name": name,
                "email": _child_text(role, "contact-email"),
            })

    urls = [
        {"name": _child_text(u, "name") or _child_text(u, "url"), "url": _child_text(u, "url")}
        for u in _children(_child(root, "urls"), "url")
        if _child_text(u, "url").startswith("https://")
    ]
    downloads = [
        {"name": _child_text(d, "file-name"), "url": _child_text(d, "download-url"),
         "format": _child_text(d, "distribution-format")}
        for d in _children(_child(root, "distribution-information"), "distribution")
        if _child_text(d, "download-url").startswith("https://")
    ]

    access = _child(root, "access-information")
    abstract = _child_text(ident, "abstract")
    return {
        "id": int(item_id) if item_id.isdigit() else None,
        "url": item_url(int(item_id)) if item_id.isdigit() else "",
        "title": _child_text(ident, "title"),
        "short_name": _child_text(ident, "short-name"),
        "item_type": _child_text(ident, "catalog-item-type"),
        "status": _child_text(ident, "status"),
        "parent_title": _child_text(ident, "parent-title"),
        "abstract": abstract[:MAX_ABSTRACT],
        "purpose": _child_text(ident, "purpose")[:MAX_ABSTRACT],
        "doi": _child_text(ident, "doi"),
        "use_constraints": _child_text(access, "data-use-constraints"),
        "keywords": keywords[:60],
        "time_frames": time_frames,
        "bbox": bbox,
        "areas": areas,
        "contacts": contacts,
        "urls": urls[:20],
        "downloads": downloads[:20],
    }


def suggest(summary: dict) -> dict:
    """Map an InPort summary onto collection form fields."""
    years: list[int] = []
    for frame in summary.get("time_frames", []):
        years += [int(y) for y in _YEAR_RE.findall(f"{frame.get('start', '')} {frame.get('end', '')}")]
    contact = next(
        (c for c in summary.get("contacts", []) if c.get("role", "").lower() == "point of contact"), None
    )
    return {
        "label": summary.get("short_name") or summary.get("title", ""),
        "year_start": min(years) if years else None,
        "year_end": max(years) if years else None,
        "survey": summary.get("short_name") or summary.get("parent_title", ""),
        "region": (summary.get("areas") or [""])[0],
        "contact": (f"{contact['name']} <{contact['email']}>" if contact and contact.get("email")
                    else (contact or {}).get("name", "")),
        "description": summary.get("abstract", ""),
    }


def lookup(url_or_id: str) -> dict:
    item_id = parse_item_id(url_or_id)
    summary = parse(fetch(item_id))
    summary["url"] = item_url(item_id)
    return {"summary": summary, "suggested": suggest(summary)}
