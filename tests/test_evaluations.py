from __future__ import annotations

import hashlib
import io
import json

import pytest

from test_curation import P, _add, _item_payload, _jpeg, _join, _project, _test_buckets, client, team
from test_curation import pinned_eval
from test_versions import _publish


def _ready_model(client, monkeypatch):
    from appdb import session_scope
    from curation import model_registry
    from curation.models import CurationClassifier

    monkeypatch.setenv("CURATION_ALLOW_LOCAL_MODELS", "on")
    response = client.post(f"{P}/evaluation-models/upload", data={"name": "Fixture classifier", "trusted": "true"},
                           files={"file": ("fixture.pt", b"trusted-fixture", "application/octet-stream")})
    assert response.status_code == 201, response.text
    model_id = response.json()["id"]
    with session_scope() as db:
        model = db.get(CurationClassifier, model_id)
        model.status = "ready"
        model.class_names = ["ACBR", "POCS"]
    monkeypatch.setattr(model_registry, "worker_available", lambda: True)
    return model_id


def _queued_run(client, monkeypatch):
    from appdb import session_scope
    from curation import evaluation_runner, evaluation_runs, settings, storage
    from curation.models import CurationEvaluationRun

    pid = _project(client, "Evaluated")['id']
    items = []
    for split, label in [("train", "ACBR"), ("val", "POCS"), ("test", "ACBR")]:
        items.append(_add(client, pid, {**_item_payload(split, label), "split": split}))
    _publish(client, pid)
    model_id = _ready_model(client, monkeypatch)
    response = client.post(f"{P}/projects/{pid}/evaluations", json={"version": 1, "classifier_id": model_id})
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    return pid, run_id, items


def _completed_run(client, monkeypatch):
    from appdb import session_scope
    from curation import evaluation_runner, settings, storage
    from curation.models import CurationEvaluationRun

    pid, run_id, items = _queued_run(client, monkeypatch)
    monkeypatch.setattr(storage, "read_bytes", lambda uri: _jpeg())
    with session_scope() as db:
        run = db.get(CurationEvaluationRun, run_id)
        evaluation_runner.stage_run(db, run)
        output = settings.evaluation_spool() / "outputs" / run_id
        output.mkdir(parents=True)
        lines = [{"item_id": item["id"], "top_predictions": [
                    {"rank": 1, "label": "POCS", "confidence": 0.8},
                    {"rank": 2, "label": "ACBR", "confidence": 0.2}]} for item in items]
        path = output / "predictions.jsonl"
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        results = {split: {"images": 1} for split in ("train", "val", "test")}
        results["all"] = {"images": 3}
        evaluation_runner.ingest_result(db, run, output, {"model_sha256": run.specification["model_sha256"],
            "manifest_sha256": run.provenance["manifest_sha256"], "predictions_sha256": evaluation_runner.file_hash(path),
            "results": results, "runtime": {"evaluator_version": "test"}})
    return pid, run_id, items


def test_split_matrices_and_paging_use_saved_predictions(client, monkeypatch):
    pid, run_id, items = _completed_run(client, monkeypatch)
    base = f"{P}/projects/{pid}"
    matrices = {}
    for split in ("train", "val", "test", "all"):
        response = client.get(f"{base}/confusion-matrix", params={"matrix_source": "evaluation", "evaluation_id": run_id,
                              "split": split, "normalization": "count"})
        assert response.status_code == 200, response.text
        matrices[split] = response.json()
    assert matrices["test"]["accuracy"] == 0 and matrices["val"]["accuracy"] == 1
    assert matrices["all"]["scored_count"] == 3 and matrices["all"]["accuracy"] == pytest.approx(1 / 3)
    assert matrices["all"]["coverage"]["mixed_splits"]
    assert matrices["all"]["scored_count"] == sum(matrices[split]["scored_count"] for split in ("train", "val", "test"))
    first = client.get(f"{base}/matrix-assets", params={"evaluation_id": run_id, "split": "all", "limit": 1}).json()
    second = client.get(f"{base}/matrix-assets", params={"evaluation_id": run_id, "split": "all", "limit": 1,
                       "cursor": first["next_cursor"]}).json()
    assert first["items"][0]["asset_id"] != second["items"][0]["asset_id"]
    other = _project(client, "Unrelated")["id"]
    assert client.get(f"{P}/projects/{other}/confusion-matrix", params={"matrix_source": "evaluation", "evaluation_id": run_id}).status_code == 404


def test_test_correction_changes_only_draft_and_records_provenance(client, monkeypatch):
    from appdb import session_scope
    from curation.models import CurationEvent

    pid, run_id, items = _completed_run(client, monkeypatch)
    base = f"{P}/projects/{pid}"
    evidence = client.get(f"{base}/matrix-assets", params={"evaluation_id": run_id, "split": "test"}).json()["items"][0]
    assert evidence["mutable"]
    preview = client.get(evidence["preview_url"])
    assert preview.status_code == 200
    body = {"expected_revision": evidence["curation_link"]["revision"], "current_label": "POCS"}
    url = f"{base}/evaluations/{run_id}/predictions/{evidence['prediction_id']}/item"
    assert client.patch(url, json=body).status_code == 409
    response = client.patch(url, json={**body, "confirm_test": True})
    assert response.status_code == 200, response.text
    assert response.json()["metadata"]["test_informed"] is True
    assert client.patch(url, json={**body, "confirm_test": True}).status_code == 409
    after = client.get(f"{base}/matrix-assets", params={"evaluation_id": run_id, "split": "test"}).json()["items"][0]
    assert after["actual_label"] == "ACBR" and after["curation_link"]["current_label"] == "POCS"
    assert client.get(evidence["preview_url"]).content == preview.content
    with session_scope() as db:
        event = db.query(CurationEvent).filter_by(project_id=pid, event_type="item_updated").one()
        assert event.new_value["evaluation"]["run_id"] == run_id
    next_version = _publish(client, pid)
    assert next_version["statistics"]["test_informed"] is True


def test_upload_and_evaluation_permissions(team, monkeypatch):
    pid = _project(team["admin"], "Permissions")["id"]
    upload = lambda role: team[role].post(f"{P}/evaluation-models/upload", data={"name": "Fixture", "trusted": "true"},  # noqa: E731
                                          files={"file": ("f.pt", b"123")})
    evaluate = lambda role: team[role].post(f"{P}/projects/{pid}/evaluations", json={"version": 1, "classifier_id": "a" * 36})  # noqa: E731
    assert upload("vic").status_code == 403
    assert evaluate("vic").status_code == 403
    # Evaluating a project is project work: on a members-only project a non-member is refused
    # whatever the app-wide role.
    assert team["admin"].put(f"{P}/projects/{pid}/access", json={"restricted": True}).status_code == 200
    assert evaluate("ana").status_code == 403
    _join(team, pid, "ana")
    # Members get past the role check; this then fails for another
    # reason (no worker in tests), which is not a 403.
    assert evaluate("ana").status_code != 403
    # Uploading a model is not project work: it still follows the create role.
    monkeypatch.setenv("CURATION_CREATE_ROLE", "lead")
    assert upload("ana").status_code == 403


def test_incomplete_results_are_not_published(client, monkeypatch):
    from appdb import session_scope
    from curation import evaluation_runner, settings
    from curation.models import CurationEvaluationRun

    pid, run_id, items = _completed_run(client, monkeypatch)
    with session_scope() as db:
        run = db.get(CurationEvaluationRun, run_id)
        output = settings.evaluation_spool() / "outputs" / run_id
        run.status = "running"
        (output / "predictions.jsonl").write_text("", encoding="utf-8")
        result = {"model_sha256": run.specification["model_sha256"], "manifest_sha256": run.provenance["manifest_sha256"],
                  "predictions_sha256": evaluation_runner.file_hash(output / "predictions.jsonl")}
        with pytest.raises(ValueError, match="incomplete"):
            evaluation_runner.ingest_result(db, run, output, result)
        db.rollback()


def test_benchmark_installer_checks_predictions_and_refuses_replacement(pinned_eval, tmp_path):
    from curation import evaluation, settings
    from scripts.import_curation_evaluation import install

    source = settings.eval_dir() / "v0"
    destination = tmp_path / "installed"
    target = install(source, destination, "test", require_benchmark_shape=False)
    assert target == destination / "v0"
    assert install(source, destination, "test", require_benchmark_shape=False) == target
    with pytest.raises(ValueError, match="67 classes"):
        evaluation.validate_artifacts(source, "test")
    metrics = json.loads((source / "metrics.json").read_text())
    metrics["images"] = 999
    (source / "metrics.json").write_text(json.dumps(metrics))
    with pytest.raises(ValueError, match="counts disagree"):
        install(source, destination, "test", require_benchmark_shape=False)


def test_supplemental_benchmark_splits_have_real_matrices(client, pinned_eval):
    import shutil
    from curation import settings

    root = settings.eval_dir() / "v0"
    folder = root / "val"
    folder.mkdir()
    for name in ("metrics.json", "predictions.csv", "provenance.json", "confusion_matrix.json"):
        shutil.copyfile(root / name, folder / name)
    provenance = json.loads((folder / "provenance.json").read_text())
    provenance["dataset_split"] = "val"
    (folder / "provenance.json").write_text(json.dumps(provenance))
    pid = _project(client, "Supplementary benchmark")["id"]
    response = client.get(f"{P}/projects/{pid}/confusion-matrix", params={"matrix_source": "t3_v0_test", "split": "all"})
    assert response.status_code == 200, response.text
    assert response.json()["scored_count"] == 24
    assert response.json()["coverage"]["mixed_splits"] is True
    assert client.get(f"{P}/projects/{pid}/confusion-matrix", params={"matrix_source": "t3_v0_test", "split": "train"}).status_code == 409
    page = client.get(f"{P}/projects/{pid}/matrix-assets", params={"evaluation_id": "t3-v0-test", "split": "val"}).json()
    assert page["returned"] == 12 and all(item["predicted_label"] for item in page["items"])


def test_offline_worker_produces_metrics_and_complete_predictions(client, monkeypatch):
    import sys
    from types import SimpleNamespace
    from appdb import session_scope
    from curation import evaluation_runner, evaluation_worker, settings, storage
    from curation.models import CurationEvaluationRun

    pytest.importorskip("sklearn.metrics")

    class FakeClassifier:
        task = "classify"
        names = {0: "ACBR", 1: "POCS"}

        def predict(self, source, **kwargs):
            probabilities = SimpleNamespace(top5=[1, 0], top5conf=SimpleNamespace(tolist=lambda: [0.8, 0.2]))
            return [SimpleNamespace(probs=probabilities) for path in source]

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(__version__="fixture", version=SimpleNamespace(cuda=None),
                                                            Tensor=type("FixtureTensor", (), {})))
    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(__version__="fixture", YOLO=lambda path: FakeClassifier()))
    monkeypatch.setenv("CURATION_TRUSTED_CHECKPOINTS", "on")
    monkeypatch.setattr(storage, "read_bytes", lambda uri: _jpeg())
    pid, run_id, items = _queued_run(client, monkeypatch)
    with session_scope() as db:
        run = db.get(CurationEvaluationRun, run_id)
        evaluation_runner.stage_run(db, run)
        folder = settings.evaluation_spool() / "inputs" / run_id
        output = settings.evaluation_spool() / "outputs" / run_id
        output.mkdir(parents=True)
        task = json.loads((folder / "task.json").read_text())
        evaluation_worker.evaluate_task(folder, output, task)
        result = json.loads((output / "result.json").read_text())
        assert result["results"]["all"]["top1_accuracy"] == pytest.approx(1 / 3)
        assert result["results"]["all"]["top5_accuracy"] == 1
        assert result["results"]["all"]["balanced_accuracy"] == 0.5
        assert result["results"]["test"]["top1_accuracy"] == 0
        evaluation_runner.ingest_result(db, run, output, result)
        assert run.status == "completed" and run.scored_count == 3
    matrix = client.get(f"{P}/projects/{pid}/confusion-matrix", params={"matrix_source": "evaluation",
                        "evaluation_id": run_id, "split": "all"}).json()
    assert matrix["metrics"]["macro_f1"] == pytest.approx(0.25)


def test_inventory_is_version_membership_not_predicted_class_filter(client, monkeypatch):
    pid, run_id, items = _completed_run(client, monkeypatch)
    response = client.get(f"{P}/projects/{pid}/matrix-assets", params={"evaluation_id": run_id, "split": "train",
                          "evidence_kind": "inventory", "actual_label": "ACBR", "predicted_label": "POCS"})
    assert response.status_code == 200, response.text
    evidence = response.json()["items"][0]
    assert evidence["actual_label"] == "ACBR" and evidence["predicted_label"] is None
    assert evidence["input_item_id"] == items[0]["id"]
    corrected = client.patch(f"{P}/projects/{pid}/evaluations/{run_id}/inputs/{evidence['input_item_id']}/item",
        json={"expected_revision": evidence["curation_link"]["revision"], "current_label": "POCS"})
    assert corrected.status_code == 200
    after = client.get(f"{P}/projects/{pid}/matrix-assets", params={"evaluation_id": run_id, "split": "train",
                       "evidence_kind": "inventory"}).json()["items"][0]
    assert after["actual_label"] == "ACBR" and after["curation_link"]["current_label"] == "POCS"


def test_cancelled_staging_cannot_publish_a_task(client, monkeypatch):
    from appdb import session_scope
    from curation import evaluation_runner, settings, storage
    from curation.models import CurationEvaluationRun

    pid, run_id, items = _queued_run(client, monkeypatch)
    response = client.post(f"{P}/projects/{pid}/evaluations/{run_id}/cancel")
    assert response.status_code == 200 and response.json()["cancel_requested"]
    monkeypatch.setattr(storage, "read_bytes", lambda uri: _jpeg())
    with session_scope() as db:
        run = db.get(CurationEvaluationRun, run_id)
        evaluation_runner.stage_run(db, run)
        assert run.status == "cancelled"
    assert not (settings.evaluation_spool() / "inputs" / run_id / "task.json").exists()


def test_completed_result_cannot_be_replaced(client, monkeypatch):
    from appdb import session_scope
    from curation import evaluation_runner, settings
    from curation.models import CurationEvaluationRun

    pid, run_id, items = _completed_run(client, monkeypatch)
    with session_scope() as db:
        run = db.get(CurationEvaluationRun, run_id)
        with pytest.raises(ValueError, match="immutable"):
            evaluation_runner.ingest_result(db, run, settings.evaluation_spool() / "outputs" / run_id, {})
    assert client.post(f"{P}/projects/{pid}/evaluations/{run_id}/cancel").status_code == 409


def test_model_registration_is_disabled_for_untrusted_auth_off(client, monkeypatch):
    monkeypatch.delenv("CURATION_ALLOW_LOCAL_MODELS", raising=False)
    response = client.post(f"{P}/evaluation-models/upload", data={"name": "Untrusted", "trusted": "true"},
                           files={"file": ("checkpoint.pt", b"not-loaded")})
    assert response.status_code == 403


def test_pinned_test_correction_requires_confirmation_and_preserves_baseline(client, pinned_eval):
    pid = _project(client, "Pinned corrections")["id"]
    item = _add(client, pid, {**_item_payload("match", "CCAH"), "file_name": "CCAH_0.png"})
    response = client.get(f"{P}/projects/{pid}/matrix-assets", params={"evaluation_id": "t3-v0-test", "split": "test",
                          "actual_label": "CCAH", "sort_by": "dataset"})
    evidence = response.json()["items"][0]
    assert evidence["mutable"] and evidence["curation_link"]["item_id"] == item["id"]
    url = f"{P}/projects/{pid}/evaluations/t3-v0-test/predictions/{evidence['prediction_id']}/item"
    body = {"expected_revision": item["revision"], "current_label": "TURFH"}
    assert client.patch(url, json=body).status_code == 409
    assert client.patch(url, json={**body, "confirm_test": True}).status_code == 200
    matrix = client.get(f"{P}/projects/{pid}/confusion-matrix", params={"matrix_source": "t3_v0_test"}).json()
    assert matrix["scored_count"] == 12 and matrix["correct_count"] == 8


def test_staging_and_missing_submitted_tasks_are_recovered(client, monkeypatch):
    from appdb import session_scope
    from curation import evaluation_runner
    from curation.models import CurationEvaluationRun

    pid, run_id, items = _queued_run(client, monkeypatch)
    with session_scope() as db:
        run = db.get(CurationEvaluationRun, run_id)
        run.status = "staging"
        evaluation_runner.recover_staging(db)
        assert run.status == "queued" and run.completed_at is None
        run.status = "running"
        db.commit()
        evaluation_runner.sync_outputs(db)
        assert run.status == "interrupted" and "submitting" in run.error_message


def test_staging_cut_off_by_a_restart_resumes_without_fetching_again(client, monkeypatch):
    from appdb import session_scope
    from curation import evaluation_runner, settings, storage
    from curation.models import CurationEvaluationRun

    pid, run_id, items = _queued_run(client, monkeypatch)
    monkeypatch.setattr(storage, "read_bytes", lambda uri: _jpeg())
    with session_scope() as db:
        run = db.get(CurationEvaluationRun, run_id)
        evaluation_runner.stage_run(db, run)
        first = json.loads((settings.evaluation_spool() / "inputs" / run_id / "manifest.json").read_text())
        # As if the server stopped before the run was handed to the worker.
        run.status, run.provenance = "staging", {"staged_count": len(first)}
        db.commit()
        evaluation_runner.recover_staging(db)
        assert run.status == "queued"

        def no_fetch(uri):
            raise AssertionError("patch fetched again")
        monkeypatch.setattr(storage, "read_bytes", no_fetch)
        evaluation_runner.stage_run(db, run)
        assert run.status == "running" and run.expected_count == len(items)
        again = json.loads((settings.evaluation_spool() / "inputs" / run_id / "manifest.json").read_text())
        assert again == first


def test_startup_removes_inputs_left_by_finished_runs(client, monkeypatch):
    from appdb import session_scope
    from curation import evaluation_runner, settings
    from curation.models import CurationEvaluationRun

    pid, run_id, items = _queued_run(client, monkeypatch)
    root = settings.evaluation_spool() / "inputs"
    for name in (run_id, "gone-run", "model-7"):
        (root / name / "images").mkdir(parents=True, exist_ok=True)
    with session_scope() as db:
        evaluation_runner.recover_staging(db)
        assert {path.name for path in root.iterdir()} >= {run_id, "model-7"}
        assert not (root / "gone-run").exists()
        db.get(CurationEvaluationRun, run_id).status = "interrupted"
        db.commit()
        assert evaluation_runner.sweep_inputs(db) == 1 and not (root / run_id).exists()


def test_active_evaluation_blocks_project_deletion(client, monkeypatch):
    pid, run_id, items = _queued_run(client, monkeypatch)
    response = client.delete(f"{P}/projects/{pid}", params={"confirm_name": "Evaluated"})
    assert response.status_code == 409 and "evaluation" in response.json()["detail"]


def test_bulk_test_edits_cannot_avoid_test_informed_provenance(client):
    pid = _project(client, "Direct test corrections")["id"]
    item = _add(client, pid, {**_item_payload("heldout", "ACBR"), "split": "test"})
    _publish(client, pid)
    response = client.post(f"{P}/projects/{pid}/items/bulk", json={"items": [
        {"id": item["id"], "expected_revision": item["revision"]}], "current_label": "POCS"})
    assert response.status_code == 200
    version = _publish(client, pid)
    assert version["statistics"]["test_informed"] is True

def test_a_moved_patch_is_evaluated_on_fresh_pixels_never_its_old_crop(client, monkeypatch, tmp_path):
    from PIL import Image
    from appdb import session_scope
    from curation import evaluation_runner, evaluation_runs, image_cache, storage
    from curation.models import CurationEvaluationRun

    buffer = io.BytesIO()
    Image.new("RGB", (1000, 800), (30, 140, 90)).save(buffer, format="JPEG")
    source = buffer.getvalue()
    reads = []

    def read(uri):  # records every read; a crop under /crops/ is the stored (old) crop
        reads.append(uri)
        return _jpeg() if "/crops/" in uri else source

    monkeypatch.setenv("CURATION_IMAGE_CACHE", str(tmp_path / "image-cache"))
    monkeypatch.setenv("CURATION_PATCH_CACHE", str(tmp_path / "patch-cache"))
    monkeypatch.setattr(storage, "read_bytes", read)
    image_cache.clear()
    pid = _project(client, "Moved and evaluated")["id"]
    items = [_add(client, pid, {**_item_payload(split, "ACBR"), "split": split}) for split in ("train", "val", "test")]
    moved = items[2]
    response = client.patch(f"{P}/projects/{pid}/items/{moved['id']}", json={
        "expected_revision": moved["revision"], "row_px": 300, "column_px": 500, "crop_size": 160})
    assert response.status_code == 200, response.text
    _publish(client, pid)
    model_id = _ready_model(client, monkeypatch)
    response = client.post(f"{P}/projects/{pid}/evaluations", json={"version": 1, "classifier_id": model_id})
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    with session_scope() as db:
        evaluation_runner.stage_run(db, db.get(CurationEvaluationRun, run_id))

    assert moved["crop_uri"] not in reads  # the stored crop of the old position is never read
    assert items[0]["crop_uri"] in reads  # unedited patches still use their stored crops
    staged = (evaluation_runs.directory(run_id) / "images" / f"{moved['id']}.png").read_bytes()
    fresh = image_cache.patch(moved["source_image_uri"], 300, 500, 160)
    assert staged == fresh
    assert json.loads((evaluation_runs.directory(run_id) / "missing.json").read_text()) == []

    # The version-member evidence card shows the same fresh cut, not the old crop.
    page = client.get(f"{P}/projects/{pid}/matrix-assets", params={
        "evaluation_id": run_id, "split": "test", "evidence_kind": "inventory"}).json()
    card = next(item for item in page["items"] if item["input_item_id"] == moved["id"])
    assert f"/inputs/{moved['id']}/image" in card["preview_url"]
    preview = client.get(card["preview_url"])
    assert preview.status_code == 200 and preview.content == fresh
    assert moved["crop_uri"] not in reads


def test_a_moved_patch_whose_source_is_gone_is_reported_missing(client, monkeypatch, tmp_path):
    from appdb import session_scope
    from curation import evaluation_runner, evaluation_runs, image_cache, storage
    from curation.models import CurationEvaluationRun

    gone = []  # source images vanish after the point was moved and published

    def read(uri):
        if gone and "/images/" in uri:
            raise FileNotFoundError(uri)
        return _jpeg((1000, 800)) if "/images/" in uri else _jpeg()

    monkeypatch.setenv("CURATION_IMAGE_CACHE", str(tmp_path / "image-cache"))
    monkeypatch.setenv("CURATION_PATCH_CACHE", "off")
    monkeypatch.setenv("CURATION_EVAL_MAX_MISSING", "0.5")
    monkeypatch.setattr(storage, "read_bytes", read)
    image_cache.clear()
    pid = _project(client, "Moved, source gone")["id"]
    items = [_add(client, pid, {**_item_payload(split, "ACBR"), "split": split}) for split in ("train", "val", "test")]
    moved = client.patch(f"{P}/projects/{pid}/items/{items[2]['id']}", json={
        "expected_revision": items[2]["revision"], "row_px": 300, "column_px": 500})
    assert moved.status_code == 200, moved.text
    _publish(client, pid)
    model_id = _ready_model(client, monkeypatch)
    run_id = client.post(f"{P}/projects/{pid}/evaluations", json={"version": 1, "classifier_id": model_id}).json()["id"]
    gone.append(True)
    image_cache.clear()
    with session_scope() as db:
        evaluation_runner.stage_run(db, db.get(CurationEvaluationRun, run_id))
    missing = json.loads((evaluation_runs.directory(run_id) / "missing.json").read_text())
    assert [(entry["item_id"], entry["reason"]) for entry in missing] == [(items[2]["id"], "not_found")]


def test_evidence_for_removed_points_is_hidden_unless_asked_for(client, monkeypatch):
    pid, run_id, items = _completed_run(client, monkeypatch)
    removed = items[2]  # the test split's only member
    response = client.patch(f"{P}/projects/{pid}/items/{removed['id']}",
                            json={"expected_revision": removed["revision"], "removed": True})
    assert response.status_code == 200, response.text
    base = f"{P}/projects/{pid}/matrix-assets"
    for kind in ("predictions", "inventory"):
        params = {"evaluation_id": run_id, "split": "all", "evidence_kind": kind}
        shown = {item["curation_link"]["item_id"] for item in client.get(base, params=params).json()["items"]}
        assert removed["id"] not in shown and items[0]["id"] in shown
        everything = client.get(base, params={**params, "removed": "any"}).json()["items"]
        assert removed["id"] in {item["curation_link"]["item_id"] for item in everything}


def test_a_matrix_cell_filters_the_project_items(client, monkeypatch):
    """Every item view can show the items behind one evaluation cell, per split, with a total."""
    pid, run_id, items = _completed_run(client, monkeypatch)
    base = f"{P}/projects/{pid}/items"
    cell = {"eval_run": run_id, "eval_actual": "ACBR", "eval_predicted": "POCS"}
    both = client.get(base, params={**cell, "with_total": "true"}).json()
    assert {item["id"] for item in both["items"]} == {items[0]["id"], items[2]["id"]} and both["total"] == 2
    train = client.get(base, params={**cell, "eval_split": "train"}).json()
    assert [item["id"] for item in train["items"]] == [items[0]["id"]] and train["total"] is None
    assert client.get(base, params={**cell, "eval_predicted": "ACBR"}).json()["returned"] == 0
    assert client.get(base, params={"item_split": "val"}).json()["items"][0]["id"] == items[1]["id"]
    assert client.get(base, params={"item_split": "everything"}).status_code == 422
