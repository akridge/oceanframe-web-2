#!/usr/bin/env python3
"""
Generate a synthetic image tree that looks like a survey dataset.

Useful for trying the library without a bucket, and for the smoke tests:

    python scripts/make_demo_library.py /tmp/demo-survey --count 240
    LIB_SOURCE=/tmp/demo-survey python launch.py
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SITES = ["kaneohe", "hanauma", "molokini", "waikiki"]
HABITATS = ["reef-flat", "reef-slope", "sand-channel"]


def _water(rng: random.Random, size: tuple[int, int], depth: float) -> Image.Image:
    """Blue-green gradient standing in for the water column."""
    width, height = size
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / max(height - 1, 1)
        r = int(18 + 40 * (1 - depth) * (1 - t))
        g = int(90 + 70 * (1 - depth) - 30 * t)
        b = int(120 + 90 * (1 - depth * 0.6) - 20 * t)
        draw.line([(0, y), (width, y)], fill=(max(r, 0), max(g, 0), max(b, 0)))
    return img


def _scene(rng: random.Random, size: tuple[int, int], n_fish: int, n_coral: int, depth: float) -> Image.Image:
    img = _water(rng, size, depth)
    draw = ImageDraw.Draw(img)
    width, height = size

    for _ in range(n_coral):
        cx, cy = rng.randint(0, width), rng.randint(height // 2, height)
        radius = rng.randint(18, 55)
        tint = rng.choice([(196, 148, 120), (210, 200, 190), (150, 110, 90), (235, 232, 228)])
        draw.ellipse([cx - radius, cy - radius // 2, cx + radius, cy + radius], fill=tint)
        for _ in range(8):
            ox, oy = rng.randint(-radius, radius), rng.randint(-radius // 2, radius // 2)
            draw.ellipse([cx + ox - 6, cy + oy - 6, cx + ox + 6, cy + oy + 6],
                         fill=tuple(min(255, c + rng.randint(-25, 25)) for c in tint))

    for _ in range(n_fish):
        cx, cy = rng.randint(20, width - 20), rng.randint(10, height - 40)
        length = rng.randint(14, 34)
        colour = rng.choice([(250, 210, 60), (240, 130, 40), (60, 200, 220), (235, 235, 240)])
        draw.ellipse([cx - length, cy - length // 3, cx + length, cy + length // 3], fill=colour)
        draw.polygon([(cx + length, cy), (cx + length + 10, cy - 8), (cx + length + 10, cy + 8)], fill=colour)

    # A little sensor noise keeps the blur metric from collapsing to zero.
    noise = Image.effect_noise(size, 12).convert("L").convert("RGB")
    return Image.blend(img, noise, 0.06)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dest", help="Directory to create the tree in")
    parser.add_argument("--count", type=int, default=180, help="Number of images (default 180)")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    dest = Path(args.dest)
    written = 0

    for i in range(args.count):
        year = rng.choice([2023, 2024, 2025])
        site = rng.choice(SITES)
        transect = f"T{rng.randint(1, 4):02d}"
        habitat = rng.choice(HABITATS)
        folder = dest / str(year) / site / transect
        folder.mkdir(parents=True, exist_ok=True)

        depth = rng.random()
        img = _scene(rng, (640, 480), n_fish=rng.randint(0, 9), n_coral=rng.randint(1, 6), depth=depth)

        roll = rng.random()
        if roll < 0.18:                       # motion blur / out-of-focus frames
            img = img.filter(ImageFilter.GaussianBlur(rng.uniform(2.5, 6.0)))
        elif roll < 0.28:                     # badly underexposed
            img = img.point(lambda v: int(v * 0.22))

        path = folder / f"{habitat}_{i:04d}.jpg"
        img.save(path, quality=88)
        written += 1

        # Every 10th image gets a near-duplicate, the way burst-mode transect
        # photography actually looks — this is what the dedupe view is for.
        if i % 10 == 0:
            twin = img.filter(ImageFilter.GaussianBlur(0.4)).point(lambda v: min(255, int(v * 1.03)))
            twin.save(folder / f"{habitat}_{i:04d}_dup.jpg", quality=88)
            written += 1

    print(f"Wrote {written} images under {dest}")


if __name__ == "__main__":
    main()
