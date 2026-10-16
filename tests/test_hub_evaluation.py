"""A Hugging Face model scored on the val and test splits of a Hugging Face project."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from test_curation import _jpeg, _test_buckets, team  # noqa: F401 — fixtures
from test_registry import (  # noqa: F401 — fixtures and helpers
    DATASET, MODEL, P, _drain, _registered_dataset, hub_client, hub_files,
)
from test_versions import _publish


def _hf_project(hub_client, mode="T3"):
    entry = _registered_dataset(hub_client)
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": f"HF {mode}", "dataset_mode": mode}).json()
    _drain(created["operation"]["id"])
    _publish(hub_client, created["project"]["id"])
    return created["project"]["id"], entry


def _hf_model(hub_client, monkeypatch, class_names=("ACAS", "POCS")):
    from appdb import session_scope
    from curation import model_registry
    from curation import registry_worker
    from curation.models import CurationClassifier

    entry = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                    "weight_file": "weights/best.pt", "trusted": True}).json()
    registry_worker.run_pending()
    entry = next(item for item in hub_client.get(f"{P}/hub/entries?kind=model").json() if item["id"] == entry["id"])
    with session_scope() as db:
        model = db.get(CurationClassifier, entry["classifier_id"])
        model.status = "ready"
        model.class_names = list(class_names)
    monkeypatch.setattr(model_registry, "worker_available", lambda: True)
    return entry["classifier_id"]


def _start(hub_client, pid, classifier_id, splits=("val", "test")):
    response = hub_client.post(f"{P}/projects/{pid}/evaluations",
                               json={"version": 1, "classifier_id": classifier_id, "splits": list(splits)})
    return response


def _stage(run_id):
    from appdb import session_scope
    from curation import evaluation_runner
    from curation.models import CurationEvaluationRun

    with session_scope() as db:
        evaluation_runner.stage_run(db, db.get(CurationEvaluationRun, run_id))


def _finish(run_id, label="ACAS"):
    """Write what the worker would, for every staged image, and ingest it."""
    from appdb import session_scope
    from curation import evaluation_runner, evaluation_runs, settings
    from curation.models import CurationEvaluationRun

    with session_scope() as db:
        run = db.get(CurationEvaluationRun, run_id)
        manifest = json.loads((evaluation_runs.directory(run_id) / "manifest.json").read_text(encoding="utf-8"))
        output = settings.evaluation_spool() / "outputs" / run_id
        output.mkdir(parents=True, exist_ok=True)
        lines = [{"item_id": entry["item_id"], "top_predictions": [
            {"rank": 1, "label": label, "confidence": 0.9}, {"rank": 2, "label": "POCS", "confidence": 0.1}]}
            for entry in manifest]
        path = output / "predictions.jsonl"
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        counts = {}
        for entry in manifest:
            counts[entry["split"]] = counts.get(entry["split"], 0) + 1
        results = {split: {"images": count} for split, count in counts.items()} | {"all": {"images": len(manifest)}}
        evaluation_runner.ingest_result(db, run, output, {
            "model_sha256": run.specification["model_sha256"], "manifest_sha256": run.provenance["manifest_sha256"],
            "predictions_sha256": evaluation_runner.file_hash(path), "results": results,
            "runtime": {"evaluator_version": "test"}})


def test_hf_model_scores_val_and_test_of_an_hf_project(hub_client, monkeypatch, tmp_path):
    from curation import evaluation_runs, settings, storage

    monkeypatch.setenv("CURATION_PATCH_CACHE", str(tmp_path / "patches"))
    fetched = []
    monkeypatch.setattr(storage, "read_bytes", lambda uri: fetched.append(uri) or _jpeg((224, 224)))
    pid, _ = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch)
    response = _start(hub_client, pid, classifier_id)
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["expected_count"] == 6 and run["splits"] == ["val", "test"]
    _stage(run["id"])
    manifest = json.loads((evaluation_runs.directory(run["id"]) / "manifest.json").read_text(encoding="utf-8"))
    assert {entry["split"] for entry in manifest} == {"val", "test"}
    assert [entry["item_id"] for entry in manifest] == sorted(entry["item_id"] for entry in manifest)
    private = evaluation_runs.directory(run["id"]) / "images" / f"{manifest[0]['item_id']}.png"
    spooled = settings.evaluation_spool() / "inputs" / run["id"] / "images" / f"{manifest[0]['item_id']}.png"
    assert os.path.samefile(private, spooled)  # linked, not written twice
    _finish(run["id"])
    base = f"{P}/projects/{pid}/confusion-matrix"
    for split in ("val", "test"):
        matrix = hub_client.get(base, params={"matrix_source": "evaluation", "evaluation_id": run["id"],
                                              "split": split, "normalization": "count"}).json()
        assert matrix["scored_count"] == 3 and matrix["unscored_count"] == 0
        assert matrix["model"]["provenance_complete"] is True
        assert matrix["accuracy"] == pytest.approx(2 / 3)  # two ACAS, one POCS, all predicted ACAS

    # A second evaluation of the same version downloads nothing: every patch is in the cache.
    fetched.clear()
    _stage(_start(hub_client, pid, classifier_id).json()["id"])
    assert fetched == []


def test_a_t1_project_cannot_be_scored_by_a_t3_model(hub_client, monkeypatch):
    from curation import storage

    monkeypatch.setattr(storage, "read_bytes", lambda uri: _jpeg((224, 224)))
    pid, _ = _hf_project(hub_client, mode="T1")
    response = _start(hub_client, pid, _hf_model(hub_client, monkeypatch))
    assert response.status_code == 422
    assert "Classifier does not contain dataset labels" in response.json()["detail"]


def test_missing_patches_are_left_unscored_and_reported(hub_client, monkeypatch, tmp_path):
    from curation import storage

    monkeypatch.setenv("CURATION_PATCH_CACHE", "off")
    monkeypatch.setenv("CURATION_EVAL_MAX_MISSING", "0.5")
    pid, _ = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch)

    def read(uri):
        if uri.endswith("ACAS_IMG-test-0_1.png"):
            raise FileNotFoundError(uri)
        return _jpeg((224, 224))

    monkeypatch.setattr(storage, "read_bytes", read)
    run_id = _start(hub_client, pid, classifier_id).json()["id"]
    _stage(run_id)
    summary = hub_client.get(f"{P}/projects/{pid}/evaluations/{run_id}").json()
    assert summary["expected_count"] == 5 and summary["unscored_count"] == 1
    assert summary["specification"]["requested_split_counts"] == {"val": 3, "test": 3}
    missing = hub_client.get(f"{P}/projects/{pid}/evaluations/{run_id}/missing.csv")
    assert missing.status_code == 200 and "ACAS_IMG-test-0_1.png" in missing.text
    _finish(run_id)
    matrix = hub_client.get(f"{P}/projects/{pid}/confusion-matrix", params={
        "matrix_source": "evaluation", "evaluation_id": run_id, "split": "test", "normalization": "count"}).json()
    assert matrix["scored_count"] == 2 and matrix["unscored_count"] == 1
    assert matrix["model"]["provenance_complete"] is False   # a patch is missing from this split
    assert matrix["coverage"]["expected"] == 3


def test_too_many_missing_patches_still_fail(hub_client, monkeypatch):
    from curation import storage

    monkeypatch.setenv("CURATION_PATCH_CACHE", "off")
    pid, _ = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch)

    def gone(uri):
        raise FileNotFoundError(uri)

    monkeypatch.setattr(storage, "read_bytes", gone)
    run_id = _start(hub_client, pid, classifier_id).json()["id"]
    with pytest.raises(ValueError, match=r"6 of 6 patches are missing"):
        _stage(run_id)


def test_parallel_staging_keeps_item_order_across_chunks(hub_client, monkeypatch):
    from curation import evaluation_runner, evaluation_runs, storage

    monkeypatch.setenv("CURATION_PATCH_CACHE", "off")
    monkeypatch.setenv("CURATION_EVAL_FETCH_WORKERS", "4")
    monkeypatch.setattr(evaluation_runner, "CHUNK", 2)
    pid, _ = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch)
    monkeypatch.setattr(storage, "read_bytes",
                        lambda uri: time.sleep(0.01 * (hash(uri) % 5)) or _jpeg((224, 224)))
    run_id = _start(hub_client, pid, classifier_id, splits=("train", "val", "test")).json()["id"]
    _stage(run_id)
    manifest = json.loads((evaluation_runs.directory(run_id) / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest) > 4  # several chunks of two
    assert [entry["item_id"] for entry in manifest] == sorted(entry["item_id"] for entry in manifest)


def test_cancel_during_staging_stops_between_chunks(hub_client, monkeypatch):
    from appdb import session_scope
    from curation import evaluation_runner, settings, storage
    from curation.models import CurationEvaluationRun

    monkeypatch.setenv("CURATION_PATCH_CACHE", "off")
    monkeypatch.setenv("CURATION_EVAL_FETCH_WORKERS", "2")
    monkeypatch.setattr(evaluation_runner, "CHUNK", 2)
    pid, _ = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch)
    run_id = _start(hub_client, pid, classifier_id).json()["id"]
    calls, lock = [], threading.Lock()

    def read(uri):
        with lock:
            calls.append(uri)
            count = len(calls)
        if count == 3:  # mid-run, from another session, as the cancel route would
            with session_scope() as other:
                other.get(CurationEvaluationRun, run_id).cancel_requested = True
        return _jpeg((224, 224))

    monkeypatch.setattr(storage, "read_bytes", read)
    _stage(run_id)
    with session_scope() as db:
        assert db.get(CurationEvaluationRun, run_id).status == "cancelled"
    assert 3 <= len(calls) < 6
    assert not (settings.evaluation_spool() / "inputs" / run_id / "task.json").exists()


def test_rgb_png_patches_pass_through_unchanged(hub_client, monkeypatch, tmp_path):
    import io

    from PIL import Image
    from curation import evaluation_runs, settings, storage

    monkeypatch.setenv("CURATION_PATCH_CACHE", "off")
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color=(5, 6, 7)).save(buffer, format="PNG", compress_level=1)
    original = buffer.getvalue()
    monkeypatch.setattr(storage, "read_bytes", lambda uri: original)
    pid, _ = _hf_project(hub_client)
    run_id = _start(hub_client, pid, _hf_model(hub_client, monkeypatch)).json()["id"]
    _stage(run_id)
    manifest = json.loads((evaluation_runs.directory(run_id) / "manifest.json").read_text(encoding="utf-8"))
    staged = settings.evaluation_spool() / "inputs" / run_id / "images" / f"{manifest[0]['item_id']}.png"
    assert staged.read_bytes() == original


def test_missing_share_boundary_is_exact(hub_client, monkeypatch):
    from curation import storage

    monkeypatch.setenv("CURATION_PATCH_CACHE", "off")
    monkeypatch.setenv("CURATION_EVAL_MAX_MISSING", "0.34")  # 6 patches: 2 may go missing, 3 may not
    pid, _ = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch)

    def gone(count):
        seen = {}

        def read(uri):
            name = uri.rsplit("/", 1)[-1]
            seen.setdefault(name, len(seen))
            if seen[name] < count:
                raise FileNotFoundError(uri)
            return _jpeg((224, 224))
        return read

    monkeypatch.setattr(storage, "read_bytes", gone(2))
    _stage(_start(hub_client, pid, classifier_id).json()["id"])
    monkeypatch.setattr(storage, "read_bytes", gone(3))
    run_id = _start(hub_client, pid, classifier_id).json()["id"]
    with pytest.raises(ValueError, match=r"3 of 6 patches are missing"):
        _stage(run_id)
    # the list of gaps is kept for the failed run, so the CSV still downloads
    assert hub_client.get(f"{P}/projects/{pid}/evaluations/{run_id}/missing.csv").text.count("not_found") == 3


def test_undecodable_patch_is_unscored_with_a_reason(hub_client, monkeypatch):
    from curation import storage

    monkeypatch.setenv("CURATION_PATCH_CACHE", "off")
    monkeypatch.setenv("CURATION_EVAL_MAX_MISSING", "0.5")
    pid, _ = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch)
    monkeypatch.setattr(storage, "read_bytes",
                        lambda uri: b"not an image" if uri.endswith("ACAS_IMG-test-0_1.png") else _jpeg((224, 224)))
    run_id = _start(hub_client, pid, classifier_id).json()["id"]
    _stage(run_id)
    text = hub_client.get(f"{P}/projects/{pid}/evaluations/{run_id}/missing.csv").text
    assert text.splitlines()[0] == "item_id,split,crop_uri,reason" and "undecodable" in text


def test_spool_inputs_are_removed_after_ingest_but_evidence_stays(hub_client, monkeypatch, tmp_path):
    from curation import evaluation_runs, settings, storage

    cache = tmp_path / "patches"
    monkeypatch.setenv("CURATION_PATCH_CACHE", str(cache))
    monkeypatch.setattr(storage, "read_bytes", lambda uri: _jpeg((224, 224)))
    pid, _ = _hf_project(hub_client)
    run_id = _start(hub_client, pid, _hf_model(hub_client, monkeypatch)).json()["id"]
    _stage(run_id)
    assert (settings.evaluation_spool() / "inputs" / run_id).is_dir()
    _finish(run_id)
    assert not (settings.evaluation_spool() / "inputs" / run_id).exists()
    assert list((evaluation_runs.directory(run_id) / "images").glob("*.png"))
    assert list(cache.rglob("*.png"))


def test_label_report_says_which_labels_the_model_knows(hub_client, monkeypatch):
    pid, entry = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch, class_names=("ACAS",))
    report = hub_client.get(f"{P}/hub/datasets/{entry['id']}/labels",
                            params={"dataset_mode": "T3", "classifier_id": classifier_id}).json()
    assert {row["label"]: row["in_model"] for row in report} == {"ACAS": True, "POCS": False}


def test_a_failed_run_still_reports_its_missing_patches(hub_client, monkeypatch):
    from appdb import session_scope
    from curation import storage
    from curation.models import CurationEvaluationRun

    monkeypatch.setenv("CURATION_PATCH_CACHE", "off")
    pid, _ = _hf_project(hub_client)
    classifier_id = _hf_model(hub_client, monkeypatch)

    def gone(uri):
        raise FileNotFoundError(uri)

    monkeypatch.setattr(storage, "read_bytes", gone)
    run_id = _start(hub_client, pid, classifier_id).json()["id"]
    with pytest.raises(ValueError):
        _stage(run_id)
    with session_scope() as db:  # what the coordinator records for a staging failure
        run = db.get(CurationEvaluationRun, run_id)
        run.status = "failed"
    summary = hub_client.get(f"{P}/projects/{pid}/evaluations/{run_id}").json()
    assert summary["status"] == "failed"
    assert summary["has_missing"] is True and summary["unscored_count"] == 6
    listed = hub_client.get(f"{P}/projects/{pid}/evaluations").json()
    assert any(run["id"] == run_id and run["has_missing"] for run in listed["evaluations"])
