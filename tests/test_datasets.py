"""Dataset selection, splitting and export."""
from __future__ import annotations

import json
import zipfile

import pytest


def _index(root):
    from library import indexer, jobs

    job = jobs.start_job("index", {}, lambda j: indexer.index_source(j, str(root)))
    for _ in jobs.stream(job, heartbeat=30):
        pass
    assert job.status == "done", job.message


def _export(dataset_id, **kwargs):
    from library import datasets, jobs

    job = jobs.start_job("export", {}, lambda j: datasets.export(j, dataset_id, **kwargs))
    for _ in jobs.stream(job, heartbeat=60):
        pass
    assert job.status == "done", job.message
    return job.result


def test_by_folder_split_has_no_leakage(library, tree):
    from library import datasets, db

    _index(tree)
    dataset = datasets.create("d1", query={"status": "ok"}, split_mode="by_folder")
    rows = db.connect().execute(
        "SELECT a.folder, di.split FROM dataset_items di JOIN assets a ON a.id = di.asset_id "
        "WHERE di.dataset_id = ?", (dataset["id"],)
    ).fetchall()

    per_folder = {}
    for row in rows:
        per_folder.setdefault(row["folder"], set()).add(row["split"])
    assert all(len(splits) == 1 for splits in per_folder.values())


def test_split_assignment_is_deterministic(library, tree):
    from library import datasets

    _index(tree)
    first = datasets.create("d1", query={"status": "ok"}, split_mode="by_folder")
    second = datasets.create("d1", query={"status": "ok"}, split_mode="by_folder")
    assert first["splits"] == second["splits"]


def test_all_train_mode(library, tree):
    from library import datasets

    _index(tree)
    dataset = datasets.create("d1", query={"status": "ok"}, split_mode="all_train")
    assert set(dataset["splits"]) == {"train"}


def test_empty_selection_is_rejected(library, tree):
    from library import datasets

    _index(tree)
    with pytest.raises(ValueError):
        datasets.create("empty", query={"tags": ["nope:none"]})


def test_csv_export_carries_provenance(library, tree):
    from library import datasets

    _index(tree)
    dataset = datasets.create("d1", query={"status": "ok"})
    result = _export(dataset["id"], kind="csv", include_images=False)

    with zipfile.ZipFile(result["path"]) as archive:
        manifest = archive.read("manifest.csv").decode()
        meta = json.loads(archive.read("manifest.json"))
        assert "README.md" in archive.namelist()
    assert "uri" in manifest and "split" in manifest and "quality" in manifest
    assert meta["dataset"]["name"] == "d1"
    assert result["written"] == 5


def test_yolo_export_writes_labels_and_data_yaml(library, tree):
    from library import datasets, db, search

    _index(tree)
    ids = search.candidate_ids({})
    with db.write() as conn:
        conn.executemany(
            "INSERT INTO detections(asset_id, label, conf, x, y, w, h, model) "
            "VALUES (?, 'fish', 0.9, 0.5, 0.5, 0.2, 0.1, 'test')", [(i,) for i in ids],
        )
    dataset = datasets.create("d1", query={"status": "ok"})
    result = _export(dataset["id"], kind="yolo-detect", include_images=False)

    assert result["classes"] == ["fish"]
    with zipfile.ZipFile(result["path"]) as archive:
        names = archive.namelist()
        assert "data.yaml" in names
        assert sum(1 for n in names if n.startswith("labels/")) == 5
        label = next(n for n in names if n.startswith("labels/"))
        assert archive.read(label).decode().startswith("0 0.5")


def test_classify_export_uses_class_tags(library, tree):
    from library import datasets, db, search, tags

    _index(tree)
    kaneohe = search.candidate_ids({"tags": ["site:kaneohe"]})
    other = search.candidate_ids({"exclude_tags": ["site:kaneohe"]})
    with db.write() as conn:
        tags.add_tags(conn, kaneohe, ["class:usable"])
        tags.add_tags(conn, other, ["class:reject"])

    dataset = datasets.create("d1", query={"status": "ok"})
    result = _export(dataset["id"], kind="yolo-classify", include_images=True)

    assert sorted(result["classes"]) == ["reject", "usable"]
    with zipfile.ZipFile(result["path"]) as archive:
        images = [n for n in archive.namelist() if n.endswith(".jpg")]
    assert len(images) == 5
    assert all("/usable/" in n or "/reject/" in n for n in images)


def test_export_filename_keeps_the_folder(library, tree):
    """Two transects can both hold img_0001.jpg; the flattened name must not collide."""
    from library.datasets import _flat_name

    assert _flat_name("2024/kaneohe/T01", "img.jpg") == "2024__kaneohe__T01__img.jpg"
    assert _flat_name("", "img.jpg") == "img.jpg"
