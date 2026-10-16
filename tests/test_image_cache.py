"""The disk cache in front of cloud image reads."""
from __future__ import annotations

import io
import threading
import time

import pytest
from PIL import Image


def _jpeg(size=(640, 480), color=(40, 120, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    from curation import image_cache, storage

    monkeypatch.setenv("CURATION_IMAGE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("CURATION_IMAGE_CACHE_MB", "50")
    calls = []
    payload = _jpeg()

    def read(uri):
        calls.append(uri)
        if uri.endswith("missing.jpg"):
            raise FileNotFoundError(uri)
        return payload

    monkeypatch.setattr(storage, "read_bytes", read)
    image_cache.clear()
    return image_cache, calls


def test_a_second_read_comes_from_disk(cache):
    image_cache, calls = cache
    first = image_cache.read_source("gs://bucket/a.jpg")
    assert image_cache.read_source("gs://bucket/a.jpg") == first
    assert calls == ["gs://bucket/a.jpg"]


def test_dimensions_and_patches(cache):
    image_cache, calls = cache
    assert image_cache.dimensions("gs://bucket/a.jpg") == (640, 480)
    patch = image_cache.patch("gs://bucket/a.jpg", 100, 200, 160)
    with Image.open(io.BytesIO(patch)) as image:
        assert image.size == (224, 224) and image.format == "PNG"
    assert image_cache.patch("gs://bucket/a.jpg", 100, 200, 160) == patch
    assert image_cache.patch("gs://bucket/a.jpg", 101, 200, 160) != b""  # a moved point is a new key
    assert calls == ["gs://bucket/a.jpg"]


def test_a_missing_image_is_not_cached(cache):
    image_cache, calls = cache
    for _ in range(2):
        with pytest.raises(FileNotFoundError):
            image_cache.read_source("gs://bucket/missing.jpg")
    assert calls == ["gs://bucket/missing.jpg"] * 2


def test_concurrent_first_reads_download_once(cache, monkeypatch):
    image_cache, calls = cache
    from curation import storage

    original = storage.read_bytes

    def slow(uri):
        time.sleep(0.2)  # the other threads arrive while this download is running
        return original(uri)

    monkeypatch.setattr(storage, "read_bytes", slow)
    errors = []

    def read():
        try:
            image_cache.read_source("gs://bucket/b.jpg")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert calls == ["gs://bucket/b.jpg"]


def test_a_cache_that_cannot_be_written_still_serves(cache, monkeypatch):
    image_cache, calls = cache
    from pathlib import Path

    def broken(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", broken)
    data = image_cache.read_source("gs://bucket/a.jpg")
    assert data
    with Image.open(io.BytesIO(image_cache.patch("gs://bucket/a.jpg", 100, 200, 160))) as image:
        assert image.size == (224, 224)


def test_eviction_tolerates_files_that_vanish(cache, monkeypatch, tmp_path):
    image_cache, _ = cache
    image_cache.read_source("gs://bucket/a.jpg")
    from pathlib import Path

    real_stat = Path.stat

    def flaky(self, *args, **kwargs):
        if self.suffix == "" and self.parent.name == "sources":
            raise FileNotFoundError(str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    image_cache._evict()  # must not raise


def test_the_cap_evicts_least_recently_used(cache, monkeypatch, tmp_path):
    image_cache, _ = cache
    monkeypatch.setenv("CURATION_IMAGE_CACHE_MB", "0")  # smaller than one image
    data = image_cache.read_source("gs://bucket/c.jpg")
    assert data  # still served even though it cannot stay
    image_cache.read_source("gs://bucket/d.jpg")
    leftovers = [p for p in (tmp_path / "cache").rglob("*") if p.is_file()]
    assert not any(p.name.endswith(".part") for p in leftovers)
    assert sum(p.stat().st_size for p in leftovers) <= len(data)  # evicted down to (at most) the newest file
