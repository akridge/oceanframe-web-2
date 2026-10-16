"""
Train/val/test assignment.

An item's split is a pure function of its *source image* (and a seed): every
point on one photo shares a split, so neighbouring patches never leak between
train and test, and the answer never changes. Items get it when they are
created; publishing only fills in any that predate this.
"""
from __future__ import annotations

import hashlib


def normalize_split(value: object) -> str | None:
    split = str(value or "").strip().lower()
    if not split:
        return None
    if split == "validation":
        split = "val"
    if split not in {"train", "val", "test"}:
        raise ValueError("Split must be train, val (or validation), or test")
    return split


def split_for(source_image_uri: str, seed: int, ratios: tuple[float, float, float]) -> str:
    digest = hashlib.sha1(f"{seed}:{source_image_uri}".encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    train, val, _ = ratios
    if fraction < train:
        return "train"
    return "val" if fraction < train + val else "test"
