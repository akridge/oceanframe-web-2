"""Model weights are cached where they survive a container being recreated."""
from __future__ import annotations


def test_a_bare_ultralytics_name_resolves_into_the_cache(tmp_path, monkeypatch):
    from library import modelcache, settings

    monkeypatch.setattr(settings, "MODEL_DIR", tmp_path / "models")
    assert modelcache.resolve("yolo11n.pt") == str(tmp_path / "models" / "yolo11n.pt")


def test_an_unwritable_model_dir_falls_back_to_the_data_volume(tmp_path, monkeypatch):
    from library import modelcache, settings

    blocked = tmp_path / "models"
    blocked.write_text("a file where the directory should be")  # mkdir fails
    monkeypatch.setattr(settings, "MODEL_DIR", blocked)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    assert modelcache.resolve("yolo11n.pt") == str(tmp_path / "data" / "models" / "yolo11n.pt")


def test_weights_on_the_data_volume_are_recognised_as_cached(tmp_path, monkeypatch):
    from library import modelcache, settings

    empty = tmp_path / "models"
    empty.mkdir()
    data_models = tmp_path / "data" / "models"
    data_models.mkdir(parents=True)
    (data_models / "yolo11n.pt").write_bytes(b"weights")
    monkeypatch.setattr(settings, "MODEL_DIR", empty)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")

    assert modelcache._is_cached("yolo11n.pt")
    assert modelcache.resolve("yolo11n.pt") == str(data_models / "yolo11n.pt")
    assert str(data_models / "yolo11n.pt") in [found["path"] for found in modelcache.cached_models()]


def test_cached_and_absolute_references_are_unchanged(tmp_path, monkeypatch):
    from library import modelcache, settings

    monkeypatch.setattr(settings, "MODEL_DIR", tmp_path)
    (tmp_path / "sam3.pt").write_bytes(b"weights")
    assert modelcache.resolve("sam3.pt") == str(tmp_path / "sam3.pt")
    absolute = str(tmp_path / "elsewhere" / "custom.pt")
    assert modelcache.resolve(absolute) == absolute
