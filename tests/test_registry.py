# tests/test_registry.py
"""Hugging Face registry: datasets and models pinned to a commit, pulled into curation."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hub_fakes import dataset_info, install_fake_hub, model_info
from test_curation import _test_buckets, db_session, team  # noqa: F401 — fixtures

DATASET = "NMFS-OSI/noaa-pacific-benthic-cover-t3"
MODEL = "NMFS-OSI/yolo11m-cls-noaa-pacific-benthic-cover-t3"
DATASET_SHA = "b47bff4ecdcdb026d31d1a154ff267227ff0f1b6"
MODEL_SHA = "77e10ec10d33dfb7f0a9ebabc542b2008bb96637"


# ── Parsing references ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,kind,expected", [
    (f"https://huggingface.co/datasets/{DATASET}", None, ("dataset", DATASET, "main")),
    (f"https://huggingface.co/{MODEL}", None, ("model", MODEL, "main")),
    (f"https://huggingface.co/{MODEL}/tree/{MODEL_SHA}", None, ("model", MODEL, MODEL_SHA)),
    (f"https://huggingface.co/datasets/{DATASET}/blob/main/README.md", None, ("dataset", DATASET, "main")),
    (f"{DATASET}", "dataset", ("dataset", DATASET, "main")),
    (f"{MODEL}@v1.0", "model", ("model", MODEL, "v1.0")),
])
def test_parse_ref_accepts_links_and_ids(text, kind, expected):
    from curation.huggingface import parse_ref

    ref = parse_ref(text, kind)
    assert (ref.kind, ref.repo_id, ref.ref) == expected


@pytest.mark.parametrize("text,kind", [
    ("https://example.com/NMFS-OSI/x", None),
    ("https://huggingface.co/spaces/owner/app", None),
    ("not a repo", "model"),
    (DATASET, None),  # a bare id needs a kind
])
def test_parse_ref_rejects_everything_else(text, kind):
    from curation.huggingface import parse_ref

    with pytest.raises(HTTPException) as caught:
        parse_ref(text, kind)
    assert caught.value.status_code == 422


def test_parse_ref_rejects_a_link_of_the_other_kind():
    from curation.huggingface import parse_ref

    with pytest.raises(HTTPException, match="dataset, not a model"):
        parse_ref(f"https://huggingface.co/datasets/{DATASET}", "model")


def test_allowed_orgs_limit_registration(monkeypatch):
    from curation.huggingface import parse_ref

    monkeypatch.setenv("CURATION_HF_ALLOWED_ORGS", "NMFS-OSI, noaa")
    assert parse_ref(DATASET, "dataset").repo_id == DATASET
    with pytest.raises(HTTPException) as caught:
        parse_ref("someone/else", "dataset")
    assert caught.value.status_code == 403


# ── Mapping hub metadata ──────────────────────────────────────────────────────

def test_repo_info_pins_the_sha_and_never_sends_a_token(monkeypatch):
    from curation import huggingface

    calls = {}

    class FakeApi:
        def __init__(self, endpoint=None, token=None):
            calls["token"] = token

        def repo_info(self, repo_id, repo_type, revision, files_metadata):
            calls["args"] = (repo_id, repo_type, revision, files_metadata)
            return SimpleNamespace(
                sha=MODEL_SHA, gated=False, private=False, pipeline_tag="image-classification",
                library_name="ultralytics",
                card_data=SimpleNamespace(to_dict=lambda: {"license": "agpl-3.0"}),
                siblings=[SimpleNamespace(rfilename="weights/best.pt", size=41)],
            )

    monkeypatch.setattr(huggingface, "_api_class", lambda: FakeApi)
    info = huggingface.repo_info(huggingface.HubRef("model", MODEL, "main"))
    assert calls["token"] is False
    assert calls["args"] == (MODEL, "model", "main", True)
    assert info.sha == MODEL_SHA
    assert info.card == {"license": "agpl-3.0", "pipeline_tag": "image-classification", "library_name": "ultralytics"}
    assert info.files == [{"path": "weights/best.pt", "size": 41}]


def test_repo_info_refuses_gated_repositories(monkeypatch):
    from curation import huggingface

    class FakeApi:
        def __init__(self, **_):
            pass

        def repo_info(self, *_args, **_kwargs):
            return SimpleNamespace(sha=MODEL_SHA, gated="manual", private=False, pipeline_tag=None,
                                   library_name=None, card_data=None, siblings=[])

    monkeypatch.setattr(huggingface, "_api_class", lambda: FakeApi)
    with pytest.raises(HTTPException) as caught:
        huggingface.repo_info(huggingface.HubRef("model", MODEL, "main"))
    assert caught.value.status_code == 422
    assert caught.value.detail == huggingface.PUBLIC_ONLY


def test_repo_info_reports_an_unreachable_hub(monkeypatch):
    from curation import huggingface

    class FakeApi:
        def __init__(self, **_):
            pass

        def repo_info(self, *_args, **_kwargs):
            raise ConnectionError("name resolution failed")

    monkeypatch.setattr(huggingface, "_api_class", lambda: FakeApi)
    with pytest.raises(HTTPException) as caught:
        huggingface.repo_info(huggingface.HubRef("model", MODEL, "main"))
    assert caught.value.status_code == 502


# ── Dataset rows ──────────────────────────────────────────────────────────────

def test_parquet_files_follow_the_card_configs():
    from curation.hub_rows import parquet_by_split

    info = dataset_info()
    assert parquet_by_split(info.card, info.files) == {
        "train": ["data/train.parquet"], "val": ["data/val.parquet"], "test": ["data/test.parquet"]}


def test_parquet_files_fall_back_to_file_names():
    from curation.hub_rows import parquet_by_split

    files = [{"path": "data/train-00000-of-00002.parquet"}, {"path": "data/train-00001-of-00002.parquet"},
             {"path": "data/validation-00000-of-00001.parquet"}, {"path": "README.md"}]
    assert parquet_by_split({}, files) == {
        "train": ["data/train-00000-of-00002.parquet", "data/train-00001-of-00002.parquet"],
        "val": ["data/validation-00000-of-00001.parquet"]}


def test_columns_are_detected_on_the_noaa_schema():
    from curation.hub_rows import detect_columns, features

    assert detect_columns(features(dataset_info().card)) == {
        "image": "gcs_uri", "label": "label_name", "file_name": "file_name", "split": "split"}


def test_embedded_images_are_out_of_scope():
    from curation.hub_rows import detect_columns

    with pytest.raises(HTTPException, match="embeds image bytes"):
        detect_columns({"image": "image", "label": "int64"})


def test_missing_label_column_names_what_was_found():
    from curation.hub_rows import detect_columns

    with pytest.raises(HTTPException, match="gcs_uri, notes"):
        detect_columns({"gcs_uri": "string", "notes": "string"})


ROW = {"image_url": "https://storage.googleapis.com/nmfs/t3/test/ACAS/ACAS_AGU-641_2025_A_09_9.png",
       "gcs_uri": "gs://nmfs/t3/test/ACAS/ACAS_AGU-641_2025_A_09_9.png", "label": 0,
       "label_name": "ACAS", "file_name": "ACAS_AGU-641_2025_A_09_9.png", "split": "test"}
MAP = {"image": "gcs_uri", "label": "label_name", "file_name": "file_name", "split": "split"}


def test_crop_uri_for_each_image_location(monkeypatch):
    from curation.hub_rows import crop_uri

    monkeypatch.setenv("CURATION_SPLIT_ROOT", "gs://nmfs/splits")
    args = (ROW, "test", "ACAS", ROW["file_name"], MAP)
    assert crop_uri(*args, {"mode": "column"}) == ROW["gcs_uri"]
    assert crop_uri(*args, {"mode": "env_root"}) == "gs://nmfs/splits/test/ACAS/ACAS_AGU-641_2025_A_09_9.png"
    assert crop_uri(*args, {"mode": "custom", "root": "gs://bucket/mine/"}) == \
        "gs://bucket/mine/test/ACAS/ACAS_AGU-641_2025_A_09_9.png"
    https_map = {**MAP, "image": "image_url"}
    assert crop_uri(ROW, "test", "ACAS", ROW["file_name"], https_map, {"mode": "column"}) == ROW["gcs_uri"]


def test_crop_uri_refuses_other_web_addresses():
    from curation.hub_rows import crop_uri

    row = {**ROW, "image_url": "https://example.com/a.png"}
    with pytest.raises(HTTPException) as caught:
        crop_uri(row, "test", "ACAS", "a.png", {**MAP, "image": "image_url"}, {"mode": "column"})
    assert caught.value.status_code == 422


def test_row_fields_use_class_names_for_integer_labels():
    from curation.hub_rows import row_fields

    split, label, file_name = row_fields({**ROW, "split": "validation"}, "test", {**MAP, "label": "label"}, ["ACAS"])
    assert (split, label, file_name) == ("val", "ACAS", ROW["file_name"])


def test_source_image_stem_follows_the_crop_naming():
    from curation.hub_rows import source_image_stem

    assert source_image_stem("ACAS", "ACAS_AGU-641_2025_A_09_9.png") == "AGU-641_2025_A_09"
    assert source_image_stem("ACAS", "other.png") is None


def _parquet(path, rows):
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


def test_iter_rows_reads_the_pinned_parquet(tmp_path, monkeypatch):
    from curation.hub_rows import iter_rows

    info = dataset_info()
    test_rows = [{**ROW, "file_name": f"ACAS_IMG-{n}_1.png"} for n in range(3)]
    install_fake_hub(monkeypatch, {DATASET: info}, {
        f"{DATASET}/data/test.parquet": _parquet(tmp_path / "test.parquet", test_rows)})
    entry = SimpleNamespace(kind="dataset", repo_id=DATASET, revision=DATASET_SHA, card=info.card, files=info.files)
    rows = list(iter_rows(entry, ["test"], ["gcs_uri", "label_name", "file_name", "split"]))
    assert [split for split, _ in rows] == ["test"] * 3
    assert rows[0][1]["file_name"] == "ACAS_IMG-0_1.png"
    assert "image_url" not in rows[0][1]


# Added by controller ruling R1: splits outside train/val/test are kept, not refused.

def test_parquet_split_named_unlabeled_maps_to_its_own_key():
    from curation.hub_rows import parquet_by_split

    card = {"configs": [{"config_name": "default", "data_files": [
        {"split": "unlabeled", "path": "data/unlabeled.parquet"}, {"split": "train", "path": "data/train.parquet"}]}]}
    files = [{"path": "data/unlabeled.parquet"}, {"path": "data/train.parquet"}]
    assert parquet_by_split(card, files) == {"unlabeled": ["data/unlabeled.parquet"], "train": ["data/train.parquet"]}


def test_row_split_outside_the_standard_three_falls_back_to_the_file_split():
    from curation.hub_rows import row_fields

    split, label, _ = row_fields({**ROW, "split": "holdout"}, "test", MAP, ["ACAS"])
    assert split == "test"


# ── Review fixes: globs, config names, class names, limits ───────────────────

def test_glob_patterns_in_the_card_expand_to_every_shard():
    from curation.hub_rows import parquet_by_split

    card = {"configs": [{"config_name": "default", "data_files": [
        {"split": "train", "path": "data/train-*"}, {"split": "test", "path": "data/test-00000-of-00001.parquet"}]}]}
    files = [{"path": "data/train-00000-of-00002.parquet"}, {"path": "data/train-00001-of-00002.parquet"},
             {"path": "data/test-00000-of-00001.parquet"}]
    assert parquet_by_split(card, files) == {
        "train": ["data/train-00000-of-00002.parquet", "data/train-00001-of-00002.parquet"],
        "test": ["data/test-00000-of-00001.parquet"]}


def test_a_path_list_may_hold_globs_and_exact_paths():
    from curation.hub_rows import parquet_by_split

    card = {"configs": [{"config_name": "default", "data_files": [
        {"split": "val", "path": ["data/val-*.parquet", "data/extra.parquet"]}]}]}
    files = [{"path": "data/val-00000.parquet"}, {"path": "data/extra.parquet"}, {"path": "data/other.parquet"}]
    assert parquet_by_split(card, files) == {"val": ["data/val-00000.parquet", "data/extra.parquet"]}


def test_a_declared_split_that_resolves_to_nothing_falls_back_to_file_names():
    from curation.hub_rows import parquet_by_split

    card = {"configs": [{"config_name": "default", "data_files": [
        {"split": "train", "path": "data/train.parquet"}, {"split": "test", "path": "data/nothing-*.parquet"}]}]}
    files = [{"path": "data/train.parquet"}, {"path": "data/test-00000-of-00001.parquet"}]
    assert parquet_by_split(card, files) == {"train": ["data/train.parquet"],
                                             "test": ["data/test-00000-of-00001.parquet"]}


def test_a_card_with_only_a_non_default_config_is_still_read():
    from curation.hub_rows import parquet_by_split

    card = {"configs": [{"config_name": "t3", "data_files": [{"split": "test", "path": "data/shard.parquet"}]}]}
    assert parquet_by_split(card, [{"path": "data/shard.parquet"}]) == {"test": ["data/shard.parquet"]}


def test_class_names_read_the_hub_dict_shape():
    from curation.hub_rows import class_names

    card = {"dataset_info": {"features": [
        {"name": "label", "dtype": {"class_label": {"names": {"0": "ACAS", "1": "POCS"}}}}]}}
    assert class_names(card, "label") == ["ACAS", "POCS"]


def test_class_names_fill_gaps_and_ignore_non_numeric_keys():
    from curation.hub_rows import class_names

    card = {"dataset_info": {"features": [
        {"name": "label", "dtype": {"class_label": {"names": {"0": "ACAS", "2": "POCS", "x": "junk"}}}}]}}
    assert class_names(card, "label") == ["ACAS", "1", "POCS"]


def test_class_names_read_the_list_shape():
    from curation.hub_rows import class_names

    card = {"dataset_info": {"features": [{"name": "label", "class_label": {"names": ["ACAS", "POCS"]}}]}}
    assert class_names(card, "label") == ["ACAS", "POCS"]


def test_iter_rows_refuses_files_over_the_hub_limit(monkeypatch):
    from curation.hub_rows import iter_rows

    monkeypatch.setenv("CURATION_HUB_MAX_BYTES", "1")
    info = dataset_info()
    entry = SimpleNamespace(kind="dataset", repo_id=DATASET, revision=DATASET_SHA, card=info.card, files=info.files)
    with pytest.raises(HTTPException) as caught:
        list(iter_rows(entry, ["test"], ["gcs_uri"]))
    assert caught.value.status_code == 413


def test_features_map_an_image_dtype_to_image():
    from curation.hub_rows import features

    card = {"dataset_info": {"features": [{"name": "img", "dtype": "image"}]}}
    assert features(card) == {"img": "image"}


# ── Registering ───────────────────────────────────────────────────────────────

P = "/api/curation"


@pytest.fixture
def hub_files(tmp_path):
    rows = {"train": [], "val": [], "test": []}
    for split in rows:
        for n, label in enumerate(["ACAS", "ACAS", "POCS"]):
            file_name = f"{label}_IMG-{split}-{n}_{n + 1}.png"
            rows[split].append({"image_url": f"https://storage.googleapis.com/nmfs/t3/{split}/{label}/{file_name}",
                                "gcs_uri": f"gs://nmfs/t3/{split}/{label}/{file_name}", "label": 0,
                                "label_name": label, "label_desc": label, "file_name": file_name, "split": split})
    weights = tmp_path / "best.pt"
    weights.write_bytes(b"hf-trusted-weights")
    return {
        f"{DATASET}/data/{name}.parquet": _parquet(tmp_path / f"{name}.parquet", rows[split])
        for name, split in (("train", "train"), ("val", "val"), ("test", "test"))
    } | {f"{MODEL}/weights/best.pt": weights, f"{MODEL}/yolo11m_cls_noaa-pacific-benthic-t3.pt": weights}


@pytest.fixture
def hub_client(library, monkeypatch, hub_files):
    from fastapi.testclient import TestClient

    import main
    from curation import storage

    monkeypatch.setenv("CURATION_IMAGE_BUCKETS", "nmfs,bucket")
    monkeypatch.setenv("CURATION_ALLOW_LOCAL_MODELS", "on")
    install_fake_hub(monkeypatch, {DATASET: dataset_info(), MODEL: model_info()}, hub_files)
    monkeypatch.setattr(storage, "read_bytes", lambda uri: b"png")
    return TestClient(main.app)


COLUMN = {"mode": "column"}


def test_lookup_previews_a_dataset(hub_client):
    response = hub_client.post(f"{P}/hub/lookup", json={"ref": f"https://huggingface.co/datasets/{DATASET}"})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["kind"] == "dataset" and preview["revision"] == DATASET_SHA
    assert preview["runnable"] is True
    assert preview["column_map"] == MAP
    assert preview["splits"] == {"train": 265046, "val": 56770, "test": 56864}
    assert preview["matches_t3_baseline"] is True
    assert preview["existing_entry_id"] is None


def test_lookup_lists_model_weights_and_flags_runnability(hub_client):
    preview = hub_client.post(f"{P}/hub/lookup", json={"ref": MODEL, "kind": "model"}).json()
    assert preview["weight_files"] == ["weights/best.pt", "yolo11m_cls_noaa-pacific-benthic-t3.pt"]
    assert preview["runnable"] is True
    assert preview["linked_datasets"] == [DATASET]


def test_register_dataset_pins_the_revision_and_is_idempotent(hub_client):
    body = {"ref": f"https://huggingface.co/datasets/{DATASET}", "location": COLUMN}
    first = hub_client.post(f"{P}/hub/datasets", json=body)
    assert first.status_code == 201, first.text
    entry = first.json()
    assert entry["revision"] == DATASET_SHA and entry["status"] == "registered"
    assert entry["sample"] == {"checked": 5, "found": 5}
    assert entry["url"] == f"https://huggingface.co/datasets/{DATASET}/tree/{DATASET_SHA}"
    again = hub_client.post(f"{P}/hub/datasets", json=body)
    assert again.status_code == 201 and again.json()["id"] == entry["id"]
    other = hub_client.post(f"{P}/hub/datasets", json={**body, "ref": f"{DATASET}@{'1' * 40}" , "kind": "dataset"})
    # the fake resolves any other ref to a different sha: a new revision is a new entry
    assert other.status_code == 201 and other.json()["id"] != entry["id"]
    listed = hub_client.get(f"{P}/hub/entries?kind=dataset").json()
    assert {item["id"] for item in listed} == {entry["id"], other.json()["id"]}


def test_register_dataset_refuses_a_bucket_outside_the_allow_list(hub_client):
    response = hub_client.post(f"{P}/hub/datasets", json={
        "ref": DATASET, "kind": "dataset", "location": {"mode": "custom", "root": "gs://someone-elses-bucket/x"}})
    assert response.status_code == 422
    assert "CURATION_IMAGE_BUCKETS" in response.json()["detail"]
    assert hub_client.get(f"{P}/hub/entries").json() == []


def test_register_dataset_fails_when_no_sample_patch_exists(hub_client, monkeypatch):
    from curation import storage

    def missing(uri):
        raise FileNotFoundError(uri)

    monkeypatch.setattr(storage, "read_bytes", missing)
    response = hub_client.post(f"{P}/hub/datasets", json={"ref": DATASET, "kind": "dataset", "location": COLUMN})
    assert response.status_code == 422
    assert "None of 5 sample patches" in response.json()["detail"]


def test_register_model_queues_then_the_worker_registers_the_weights(hub_client):
    from appdb import session_scope
    from curation import registry_worker
    from curation.models import CurationClassifier

    response = hub_client.post(f"{P}/hub/models", json={
        "ref": f"https://huggingface.co/{MODEL}", "weight_file": "weights/best.pt", "trusted": True})
    assert response.status_code == 202, response.text
    entry = response.json()
    assert entry["status"] == "queued" and entry["classifier_id"] is None and entry["runnable"] is True

    assert registry_worker.run_pending() == 1
    listed = next(item for item in hub_client.get(f"{P}/hub/entries?kind=model").json() if item["id"] == entry["id"])
    assert listed["status"] == "ready" and listed["classifier_id"]
    assert listed["trained_on"] == [DATASET]
    models = hub_client.get(f"{P}/evaluation-models").json()["models"]
    assert next(m for m in models if m["id"] == listed["classifier_id"])["name"] == "yolo11m-cls-noaa-pacific-benthic-cover-t3"
    with session_scope() as db:
        provenance = db.get(CurationClassifier, listed["classifier_id"]).provenance
    assert provenance == {"source": "huggingface", "repo_id": MODEL, "revision": MODEL_SHA,
                          "file": "weights/best.pt", "license": "agpl-3.0", "trusted": True}


def test_registering_the_same_commit_twice_makes_one_entry_and_one_classifier(hub_client):
    from appdb import session_scope
    from curation import registry_worker
    from curation.models import CurationClassifier

    body = {"ref": MODEL, "kind": "model", "weight_file": "weights/best.pt", "trusted": True}
    first = hub_client.post(f"{P}/hub/models", json=body).json()
    second = hub_client.post(f"{P}/hub/models", json=body)
    assert second.status_code == 202 and second.json()["id"] == first["id"]
    registry_worker.run_pending()
    with session_scope() as db:
        assert db.query(CurationClassifier).count() == 1


def test_a_failed_download_is_kept_on_the_entry_and_can_be_retried(hub_client, monkeypatch):
    from fastapi import HTTPException

    from curation import huggingface, registry_worker

    entry = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                     "weight_file": "weights/best.pt", "trusted": True}).json()
    real = huggingface.download

    def down(*args):
        raise HTTPException(502, "Hugging Face is unreachable")

    monkeypatch.setattr(huggingface, "download", down)
    registry_worker.run_pending()
    failed = next(i for i in hub_client.get(f"{P}/hub/entries?kind=model").json() if i["id"] == entry["id"])
    assert failed["status"] == "failed" and "unreachable" in failed["error_message"]

    monkeypatch.setattr(huggingface, "download", real)
    retried = hub_client.post(f"{P}/hub/entries/{entry['id']}/retry")
    assert retried.status_code == 202 and retried.json()["status"] == "queued"
    registry_worker.run_pending()
    ready = next(i for i in hub_client.get(f"{P}/hub/entries?kind=model").json() if i["id"] == entry["id"])
    assert ready["status"] == "ready" and ready["error_message"] is None
    assert hub_client.post(f"{P}/hub/entries/{entry['id']}/retry").status_code == 409


def test_a_download_interrupted_by_a_restart_is_queued_again(hub_client):
    from appdb import session_scope
    from curation import registry_worker
    from curation.models import CurationHubEntry

    entry = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                     "weight_file": "weights/best.pt", "trusted": True}).json()
    with session_scope() as db:
        db.get(CurationHubEntry, entry["id"]).status = "downloading"
    assert registry_worker.recover() == 1
    with session_scope() as db:
        assert db.get(CurationHubEntry, entry["id"]).status == "queued"


def test_a_registrant_who_no_longer_exists_fails_the_entry_clearly(hub_client):
    from appdb import session_scope
    from curation import registry_worker
    from curation.models import CurationHubEntry

    entry = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                     "weight_file": "weights/best.pt", "trusted": True}).json()
    with session_scope() as db:
        db.get(CurationHubEntry, entry["id"]).created_by_user_id = None
    registry_worker.run_pending()
    with session_scope() as db:
        stored = db.get(CurationHubEntry, entry["id"])
        assert stored.status == "failed" and "register it again" in stored.error_message


def test_retry_by_another_user_adopts_an_orphaned_failed_entry(hub_client):
    from appdb import session_scope
    from curation import registry_worker
    from curation.models import CurationHubEntry

    entry = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                     "weight_file": "weights/best.pt", "trusted": True}).json()
    with session_scope() as db:
        db.get(CurationHubEntry, entry["id"]).created_by_user_id = None  # the registrant's account was deleted
    registry_worker.run_pending()
    retried = hub_client.post(f"{P}/hub/entries/{entry['id']}/retry")
    assert retried.status_code == 202 and retried.json()["status"] == "queued"
    registry_worker.run_pending()
    with session_scope() as db:
        stored = db.get(CurationHubEntry, entry["id"])
        assert stored.status == "ready" and stored.created_by_user_id is not None and stored.error_message is None


def test_registering_a_failed_model_again_queues_it_under_the_new_user(hub_client):
    from appdb import session_scope
    from curation import registry_worker
    from curation.models import CurationHubEntry

    body = {"ref": MODEL, "kind": "model", "weight_file": "weights/best.pt", "trusted": True}
    entry = hub_client.post(f"{P}/hub/models", json=body).json()
    with session_scope() as db:
        db.get(CurationHubEntry, entry["id"]).created_by_user_id = None
    registry_worker.run_pending()
    again = hub_client.post(f"{P}/hub/models", json=body)
    assert again.status_code == 202, again.text
    assert again.json()["id"] == entry["id"] and again.json()["status"] == "queued"
    registry_worker.run_pending()
    with session_scope() as db:
        assert db.get(CurationHubEntry, entry["id"]).status == "ready"


def test_retry_needs_the_create_role(team):
    assert team["vic"].post(f"{P}/hub/entries/1/retry").status_code == 403
    assert team["ana"].post(f"{P}/hub/entries/1/retry").status_code == 404


def test_register_model_needs_trust_and_a_pt_file(hub_client):
    untrusted = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                        "weight_file": "weights/best.pt", "trusted": False})
    assert untrusted.status_code == 422
    onnx = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model", "trusted": True,
                                                   "weight_file": "yolo11m_cls_noaa-pacific-benthic-t3.onnx"})
    assert onnx.status_code == 422
    assert untrusted.json()["detail"] != onnx.json()["detail"]


def test_non_classifier_models_are_catalog_only(hub_client, monkeypatch, hub_files):
    from dataclasses import replace

    detector = replace(model_info(), repo_id="NMFS-OSI/detector",
                       card={"pipeline_tag": "object-detection", "library_name": "ultralytics"})
    install_fake_hub(monkeypatch, {DATASET: dataset_info(), MODEL: model_info(), "NMFS-OSI/detector": detector},
                     hub_files)
    from curation import huggingface

    monkeypatch.setattr(huggingface, "download", lambda *a: pytest.fail("a catalog-only model must not download"))
    response = hub_client.post(f"{P}/hub/models", json={"ref": "NMFS-OSI/detector", "kind": "model"})
    assert response.status_code == 201, response.text
    entry = response.json()
    assert entry["runnable"] is False and entry["classifier_id"] is None
    assert entry["task"] == "object-detection"
    assert "classification" in entry["not_runnable_reason"]


def test_gated_repositories_are_refused(hub_client, monkeypatch, hub_files):
    from dataclasses import replace

    install_fake_hub(monkeypatch, {DATASET: replace(dataset_info(), gated="auto")}, hub_files)
    response = hub_client.post(f"{P}/hub/lookup", json={"ref": DATASET, "kind": "dataset"})
    assert response.status_code == 422
    assert response.json()["detail"] == "Only public Hugging Face repositories are supported"


def test_hub_roles(team, monkeypatch, hub_files):
    from curation import storage

    install_fake_hub(monkeypatch, {DATASET: dataset_info(), MODEL: model_info()}, hub_files)
    monkeypatch.setenv("CURATION_IMAGE_BUCKETS", "nmfs,bucket")
    monkeypatch.setattr(storage, "read_bytes", lambda uri: b"png")
    body = {"ref": DATASET, "kind": "dataset", "location": COLUMN}
    # Viewers read the registry but register nothing.
    assert team["vic"].get(f"{P}/hub/entries").status_code == 200
    assert team["vic"].post(f"{P}/hub/lookup", json={"ref": DATASET, "kind": "dataset"}).status_code == 403
    assert team["vic"].post(f"{P}/hub/datasets", json=body).status_code == 403
    assert team["vic"].post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model"}).status_code == 403
    # Curators register by default.
    assert team["ana"].post(f"{P}/hub/lookup", json={"ref": DATASET, "kind": "dataset"}).status_code == 200
    assert team["ana"].post(f"{P}/hub/datasets", json=body).status_code == 201
    # CURATION_CREATE_ROLE=lead takes that back.
    monkeypatch.setenv("CURATION_CREATE_ROLE", "lead")
    assert team["ana"].post(f"{P}/hub/lookup", json={"ref": DATASET, "kind": "dataset"}).status_code == 403
    assert team["ana"].post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model"}).status_code == 403


def test_a_curator_previews_a_link_but_only_a_lead_applies_it(team):
    # The role check comes before the lookup, so a missing entry shows who got past it.
    link = f"{P}/hub/datasets/999/links"
    assert team["ana"].post(link, json={"project_id": "p", "dry_run": True}).status_code == 404
    response = team["ana"].post(link, json={"project_id": "p", "dry_run": False})
    assert response.status_code == 403 and "lead" in response.json()["detail"]
    assert team["vic"].post(link, json={"project_id": "p", "dry_run": True}).status_code == 403


def test_a_partial_column_map_must_still_name_image_and_label(hub_client, monkeypatch, hub_files):
    from dataclasses import replace

    bare = replace(dataset_info(), card={**dataset_info().card, "dataset_info": {}})
    install_fake_hub(monkeypatch, {DATASET: bare}, hub_files)
    response = hub_client.post(f"{P}/hub/datasets", json={
        "ref": DATASET, "kind": "dataset", "column_map": {"label": "label_name"}, "location": COLUMN})
    assert response.status_code == 422
    assert "image" in response.json()["detail"]


def test_an_unrelated_integrity_error_is_not_swallowed(library, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from curation import registry

    info = dataset_info()
    user = SimpleNamespace(id=None, label="x")
    class Failing:
        def add(self, _): pass

        def rollback(self): pass

        def commit(self):
            raise IntegrityError("s", {}, Exception("x"))

    monkeypatch.setattr(registry, "_existing", lambda db, info: None)
    with pytest.raises(IntegrityError):
        registry._create(Failing(), info, "main", user, runnable=True)


def test_registering_needs_at_least_one_readable_sample_row(hub_client, monkeypatch):
    from curation import hub_rows

    monkeypatch.setattr(hub_rows, "sample_rows", lambda entry, n=5: [])
    response = hub_client.post(f"{P}/hub/datasets", json={"ref": DATASET, "kind": "dataset", "location": COLUMN})
    assert response.status_code == 422
    assert "No sample rows" in response.json()["detail"]


def test_a_malformed_project_config_does_not_break_the_listing(hub_client):
    from appdb import session_scope
    from curation.models import CurationProject

    hub_client.post(f"{P}/hub/datasets", json={"ref": DATASET, "kind": "dataset", "location": COLUMN})
    with session_scope() as db:
        db.add(CurationProject(name="odd", source_config={"hub": "x", "hub_links": ["y", 3]}))
        db.commit()
    assert hub_client.get(f"{P}/hub/entries").status_code == 200


# ── Dataset → new project ─────────────────────────────────────────────────────

def _registered_dataset(hub_client, location=COLUMN):
    response = hub_client.post(f"{P}/hub/datasets", json={"ref": DATASET, "kind": "dataset", "location": location})
    assert response.status_code == 201, response.text
    return response.json()


def _drain(operation_id):
    import threading

    from curation import importer

    importer.run_import(operation_id, threading.Event())


def test_dataset_becomes_a_patch_only_project(hub_client):
    entry = _registered_dataset(hub_client)
    response = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                               json={"name": "T3 from HF", "dataset_mode": "T3", "splits": ["test", "val"]})
    assert response.status_code == 202, response.text
    project, operation = response.json()["project"], response.json()["operation"]
    assert project["source_kind"] == "huggingface"
    assert project["my_role"] == "lead"
    assert project["source_config"]["hub"] == {"entry_id": entry["id"], "repo_id": DATASET, "revision": DATASET_SHA}
    _drain(operation["id"])

    done = hub_client.get(f"{P}/projects/{project['id']}/imports/{operation['id']}").json()
    assert done["status"] == "completed", done
    assert (done["imported_rows"], done["skipped_rows"], done["rejected_rows"]) == (6, 0, 0)
    items = hub_client.get(f"{P}/projects/{project['id']}/items?limit=50").json()["items"]
    assert {item["split"] for item in items} == {"test", "val"}
    first = next(item for item in items if item["file_name"] == "ACAS_IMG-test-0_1.png")
    assert first["crop_uri"] == "gs://nmfs/t3/test/ACAS/ACAS_IMG-test-0_1.png"
    assert first["source_image_uri"] == first["crop_uri"]
    assert first["row_px"] is None and first["column_px"] is None
    assert first["source_label"] == "ACAS"


def test_reimporting_a_dataset_skips_what_is_there(hub_client):
    entry = _registered_dataset(hub_client)
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": "Twice", "dataset_mode": "T3", "splits": ["test"]}).json()
    _drain(created["operation"]["id"])
    from appdb import session_scope
    from curation.models import CurationImportOperation

    with session_scope() as db:
        again = CurationImportOperation(project_id=created["project"]["id"],
                                        sources=created["operation"]["sources"], batch_size=2000)
        db.add(again)
        db.flush()
        again_id = again.id
    _drain(again_id)
    second = hub_client.get(f"{P}/projects/{created['project']['id']}/imports/{again_id}").json()
    assert (second["imported_rows"], second["skipped_rows"]) == (0, 3)


def test_hub_failure_mid_import_keeps_what_landed(hub_client, monkeypatch):
    from fastapi import HTTPException as Hx

    from curation import huggingface

    entry = _registered_dataset(hub_client)
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": "Flaky", "dataset_mode": "T3", "splits": ["train", "test"]}).json()
    real = huggingface.download

    def flaky(kind, repo_id, sha, path):
        if path.endswith("test.parquet"):
            raise Hx(502, "Could not download data/test.parquet from Hugging Face: timed out")
        return real(kind, repo_id, sha, path)

    monkeypatch.setattr(huggingface, "download", flaky)
    _drain(created["operation"]["id"])
    failed = hub_client.get(f"{P}/projects/{created['project']['id']}/imports/{created['operation']['id']}").json()
    assert failed["status"] == "failed"
    assert "timed out" in failed["error_message"]
    # Splits are read in the card's order (train, val, test): train landed before test failed.
    assert failed["imported_rows"] == 3


def test_label_report_flags_labels_outside_the_tier(hub_client, monkeypatch):
    from curation import taxonomy

    monkeypatch.setattr(taxonomy, "available", lambda: True)
    monkeypatch.setattr(taxonomy, "project_code", lambda code, mode: code if code == "ACAS" else None)
    entry = _registered_dataset(hub_client)
    report = hub_client.get(f"{P}/hub/datasets/{entry['id']}/labels?dataset_mode=T3").json()
    assert report == [{"label": "ACAS", "count": 6, "effective": "ACAS", "known": True},
                      {"label": "POCS", "count": 3, "effective": "POCS", "known": False}]


def test_any_failure_mid_import_keeps_what_landed(hub_client, monkeypatch):
    from curation import hub_rows

    entry = _registered_dataset(hub_client)
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": "Corrupt", "dataset_mode": "T3", "splits": ["train"]}).json()
    real = hub_rows.iter_rows

    def corrupt(*args, **kwargs):
        yield from real(*args, **kwargs)
        raise OSError("parquet footer is corrupt")

    monkeypatch.setattr(hub_rows, "iter_rows", corrupt)
    _drain(created["operation"]["id"])
    project_id = created["project"]["id"]
    failed = hub_client.get(f"{P}/projects/{project_id}/imports/{created['operation']['id']}").json()
    assert failed["status"] == "failed"
    assert "corrupt" in failed["error_message"]
    assert failed["imported_rows"] == 3
    assert hub_client.get(f"{P}/projects/{project_id}").json()["item_count"] == 3


def test_unknown_split_is_rejected(hub_client):
    entry = _registered_dataset(hub_client)
    response = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                               json={"name": "Typo", "dataset_mode": "T3", "splits": ["tset", "val"]})
    assert response.status_code == 422
    assert "tset" in response.json()["detail"] and "val" in response.json()["detail"]


def test_a_bad_row_is_rejected_and_hub_origin_is_recorded(hub_client, monkeypatch):
    from curation import hub_rows

    entry = _registered_dataset(hub_client)
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": "Odd row", "dataset_mode": "T3", "splits": ["test"]}).json()
    real = hub_rows.iter_rows

    def one_bad(*args, **kwargs):
        for n, (split, row) in enumerate(real(*args, **kwargs)):
            yield split, ({**row, "gcs_uri": "/local/path.png"} if n == 0 else row)

    monkeypatch.setattr(hub_rows, "iter_rows", one_bad)
    _drain(created["operation"]["id"])
    project_id = created["project"]["id"]
    done = hub_client.get(f"{P}/projects/{project_id}/imports/{created['operation']['id']}").json()
    assert done["status"] == "completed", done
    assert (done["imported_rows"], done["rejected_rows"]) == (2, 1)
    items = hub_client.get(f"{P}/projects/{project_id}/items?limit=50").json()["items"]
    assert items and all(item["metadata"]["split_origin"] == "hub" for item in items)


# ── Dataset → existing project ────────────────────────────────────────────────

def _csv_project(hub_client):
    """A project whose items carry the crop names the HF rows use (as a CSV import makes them)."""
    from test_curation import _add, _item_payload, _project

    project = _project(hub_client, "CSV project")
    made = {}
    for split, n, label, origin_split in (("test", 0, "ACAS", None), ("test", 2, "POCS", "train"),
                                          ("val", 1, "ACAS", None)):
        file_name = f"{label}_IMG-{split}-{n}_{n + 1}.png"
        payload = {**_item_payload(f"{split}-{n}", label), "file_name": file_name}
        if origin_split:
            payload["split"] = origin_split  # a split that came from the CSV
        made[file_name] = _add(hub_client, project["id"], payload)
    return project, made


def _link(hub_client, entry_id, project_id, *, dry_run, adopt=True):
    response = hub_client.post(f"{P}/hub/datasets/{entry_id}/links",
                               json={"project_id": project_id, "adopt_splits": adopt, "dry_run": dry_run})
    assert response.status_code == 202, response.text
    _drain(response.json()["id"])
    return hub_client.get(f"{P}/projects/{project_id}/imports/{response.json()['id']}").json()


def test_link_dry_run_counts_without_changing_anything(hub_client):
    from appdb import session_scope
    from curation.models import CurationEvent, CurationItem

    entry = _registered_dataset(hub_client)
    project, made = _csv_project(hub_client)
    with session_scope() as db:
        before = {item.id: (item.split, item.revision) for item in db.query(CurationItem)}
        events_before = db.query(CurationEvent).count()
    done = _link(hub_client, entry["id"], project["id"], dry_run=True)
    assert done["status"] == "completed", done
    result = done["sources"][0]["result"]
    assert result["matched"] == 3 and result["unmatched"] == 6 and result["ambiguous"] == 0
    assert result["kept_csv_split"] == 1   # the POCS item's CSV said train; HF says test
    with session_scope() as db:
        assert {item.id: (item.split, item.revision) for item in db.query(CurationItem)} == before
        assert db.query(CurationEvent).count() == events_before


def test_link_apply_records_provenance_and_events(hub_client):
    from appdb import session_scope
    from curation.models import CurationEvent, CurationItem, CurationProject

    entry = _registered_dataset(hub_client)
    project, made = _csv_project(hub_client)
    done = _link(hub_client, entry["id"], project["id"], dry_run=False)
    assert done["status"] == "completed", done
    with session_scope() as db:
        items = {item.file_name: item for item in db.query(CurationItem)}
        acas = items["ACAS_IMG-test-0_1.png"]
        assert acas.metadata_json["hub"] == {"repo_id": DATASET, "revision": DATASET_SHA, "split": "test"}
        assert acas.revision == made["ACAS_IMG-test-0_1.png"]["revision"] + 1
        assert items["POCS_IMG-test-2_3.png"].split == "train"   # CSV split wins
        every = db.query(CurationEvent).filter(CurationEvent.operation_id == done["id"]).all()
        assert [event.event_type for event in every].count("source_import_completed") == 1
        events = [event for event in every if event.event_type in {"hub_link", "split_adopted"}]
        assert {event.event_type for event in events} <= {"hub_link", "split_adopted"}
        assert len(events) == 3
        links = db.get(CurationProject, project["id"]).source_config["hub_links"]
        assert links[0]["entry_id"] == entry["id"] and links[0]["revision"] == DATASET_SHA
    again = _link(hub_client, entry["id"], project["id"], dry_run=False)
    assert again["sources"][0]["result"]["already_linked"] == 3


def _frozen_splits(project_id, version):
    from appdb import session_scope
    from sqlalchemy import select

    from curation.models import CurationDatasetVersion, CurationVersionInput, CurationVersionItem

    with session_scope() as db:
        version_id = db.execute(select(CurationDatasetVersion.id).where(
            CurationDatasetVersion.project_id == project_id, CurationDatasetVersion.version == version)).scalar_one()
        return ({row.item_id: row.split for row in db.query(CurationVersionItem).filter_by(version_id=version_id)},
                {row.item_id: row.split for row in db.query(CurationVersionInput).filter_by(version_id=version_id)})


def test_published_versions_keep_their_frozen_splits(hub_client):
    from test_versions import _publish

    entry = _registered_dataset(hub_client)
    project, made = _csv_project(hub_client)
    version = _publish(hub_client, project["id"])
    before = _frozen_splits(project["id"], version["version"])
    _link(hub_client, entry["id"], project["id"], dry_run=False)
    frozen = hub_client.get(f"{P}/projects/{project['id']}/versions/{version['version']}").json()
    assert frozen["statistics"] == version["statistics"]
    assert _frozen_splits(project["id"], version["version"]) == before
    later = _publish(hub_client, project["id"])   # a new version sees the hub splits, as their own bucket
    assert later["statistics"]["split_origins"]["hub"] == 2


def _rows_then(monkeypatch, *, twice=False, error=None):
    from curation import hub_rows

    original = hub_rows.iter_rows

    def patched(*args, **kwargs):
        for split, row in original(*args, **kwargs):
            yield split, row
            if twice:
                yield split, row
        if error:
            raise error

    monkeypatch.setattr(hub_rows, "iter_rows", patched)


def test_a_crop_listed_twice_changes_its_item_once(hub_client, monkeypatch):
    from appdb import session_scope
    from curation.models import CurationEvent

    entry = _registered_dataset(hub_client)
    project, _ = _csv_project(hub_client)
    _rows_then(monkeypatch, twice=True)
    done = _link(hub_client, entry["id"], project["id"], dry_run=False)
    assert done["status"] == "completed", done
    assert done["sources"][0]["result"]["matched"] == 6
    with session_scope() as db:
        assert db.query(CurationEvent).filter(CurationEvent.operation_id == done["id"],
                                               CurationEvent.event_type != "source_import_completed").count() == 3


def test_a_failed_link_keeps_what_it_applied_and_says_so(hub_client, monkeypatch):
    from appdb import session_scope
    from curation.models import CurationItem, CurationProject

    entry = _registered_dataset(hub_client)
    project, _ = _csv_project(hub_client)
    _rows_then(monkeypatch, error=OSError("connection lost"))
    done = _link(hub_client, entry["id"], project["id"], dry_run=False)
    assert done["status"] == "failed" and "connection lost" in done["error_message"]
    with session_scope() as db:
        item = db.query(CurationItem).filter_by(file_name="ACAS_IMG-test-0_1.png").one()
        assert item.metadata_json["hub"]["revision"] == DATASET_SHA
        links = db.get(CurationProject, project["id"]).source_config["hub_links"]
        assert links[0]["partial"] is True



def test_match_items_is_shared_with_matrix_links(db_session):
    from curation import service

    assert service.match_items(db_session, "no-such-project", []) == {}


# ── Versions remember where they came from ────────────────────────────────────

def test_versions_record_hugging_face_provenance(hub_client):
    from test_versions import _publish

    from curation import settings

    entry = _registered_dataset(hub_client)
    project, _ = _csv_project(hub_client)
    _link(hub_client, entry["id"], project["id"], dry_run=False)
    version = _publish(hub_client, project["id"])
    manifest = hub_client.get(f"{P}/projects/{project['id']}/versions/{version['version']}/files/manifest.json").json()
    assert manifest["derived_from"] == [{"repo_id": DATASET, "revision": DATASET_SHA, "relation": "linked"}]
    changelog = hub_client.get(f"{P}/projects/{project['id']}/versions/{version['version']}/files/CHANGELOG.md").text
    assert f"Derived from: {DATASET}@{DATASET_SHA[:8]} (linked)" in changelog


def test_malformed_hub_config_gives_no_provenance():
    from types import SimpleNamespace

    from curation import versions

    assert versions.hub_provenance(SimpleNamespace(source_config={"hub": "junk", "hub_links": [None, {"repo_id": DATASET}]})) == []
    partial = {"repo_id": DATASET, "revision": DATASET_SHA, "partial": True}
    assert versions.hub_provenance(SimpleNamespace(source_config={"hub_links": [partial]})) == [
        {"repo_id": DATASET, "revision": DATASET_SHA, "relation": "linked", "partial": True}]


# ── Final review fixes ────────────────────────────────────────────────────────

def _with_unlabeled(monkeypatch, hub_files, tmp_path):
    """The NOAA card plus a fourth split, "unlabeled", that is none of train/val/test."""
    from dataclasses import replace

    info = dataset_info()
    card = {**info.card, "configs": [{"config_name": "default", "data_files": [
        *info.card["configs"][0]["data_files"], {"split": "unlabeled", "path": "data/unlabeled.parquet"}]}]}
    files = [*info.files, {"path": "data/unlabeled.parquet", "size": 1000}]
    rows = [{"image_url": "", "gcs_uri": f"gs://nmfs/t3/unlabeled/ACAS/ACAS_IMG-u-{n}_{n + 1}.png", "label": 0,
             "label_name": "ACAS", "label_desc": "ACAS", "file_name": f"ACAS_IMG-u-{n}_{n + 1}.png",
             "split": "unlabeled"} for n in range(2)]
    extra = {f"{DATASET}/data/unlabeled.parquet": _parquet(tmp_path / "unlabeled.parquet", rows)}
    install_fake_hub(monkeypatch, {DATASET: replace(info, card=card, files=files), MODEL: model_info()},
                     {**hub_files, **extra})


def test_a_split_outside_train_val_test_imports_unassigned_and_publishes(hub_client, monkeypatch, hub_files, tmp_path):
    from test_versions import _publish

    _with_unlabeled(monkeypatch, hub_files, tmp_path)
    entry = _registered_dataset(hub_client)
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": "With unlabeled", "dataset_mode": "T3", "splits": ["unlabeled", "test"]})
    assert created.status_code == 202, created.text
    project_id = created.json()["project"]["id"]
    _drain(created.json()["operation"]["id"])
    items = hub_client.get(f"{P}/projects/{project_id}/items?limit=50").json()["items"]
    odd = [item for item in items if "IMG-u-" in item["file_name"]]
    assert len(odd) == 2 and all(item["split"] is None for item in odd), odd
    assert all(item["metadata"]["hub"]["split"] == "unlabeled" for item in odd)
    assert {item["split"] for item in items if item not in odd} == {"test"}
    version = _publish(hub_client, project_id)
    assert version["status"] == "published"
    assert sum(version["statistics"]["splits"].values()) == 5


def test_linking_never_adopts_a_split_outside_train_val_test(hub_client, monkeypatch):
    from appdb import session_scope
    from curation import hub_rows
    from curation.models import CurationItem

    entry = _registered_dataset(hub_client)
    project, made = _csv_project(hub_client)
    name = "ACAS_IMG-val-1_2.png"   # no split of its own yet
    row = {"gcs_uri": f"gs://nmfs/t3/unlabeled/ACAS/{name}", "label_name": "ACAS", "file_name": name,
           "split": "unlabeled"}
    monkeypatch.setattr(hub_rows, "iter_rows", lambda *args, **kwargs: iter([("unlabeled", row)]))
    done = _link(hub_client, entry["id"], project["id"], dry_run=False, adopt=True)
    assert done["status"] == "completed", done
    assert done["sources"][0]["result"]["matched"] == 1
    assert done["sources"][0]["result"]["split_changes"] == 0
    with session_scope() as db:
        item = db.get(CurationItem, made[name]["id"])
        assert item.split == made[name]["split"]
        assert item.metadata_json["hub"]["split"] == "unlabeled"


def test_imported_items_record_their_parquet_row(hub_client):
    entry = _registered_dataset(hub_client)
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": "Rows", "dataset_mode": "T3", "splits": ["test"]}).json()
    _drain(created["operation"]["id"])
    items = hub_client.get(f"{P}/projects/{created['project']['id']}/items?limit=50").json()["items"]
    rows = {item["file_name"]: item["metadata"]["hub"]["row"] for item in items}
    assert rows == {"ACAS_IMG-test-0_1.png": 0, "ACAS_IMG-test-1_2.png": 1, "POCS_IMG-test-2_3.png": 2}


def test_the_sample_check_reads_the_smallest_parquet_first(hub_client, monkeypatch):
    from curation import huggingface

    real = huggingface.download
    fetched = []

    def recording(kind, repo_id, sha, path):
        fetched.append(path)
        return real(kind, repo_id, sha, path)

    monkeypatch.setattr(huggingface, "download", recording)
    entry = _registered_dataset(hub_client)
    assert entry["sample"] == {"checked": 5, "found": 5}
    assert "data/train.parquet" not in fetched   # 9 MB; val and test are 2 MB
    assert fetched[0] in {"data/val.parquet", "data/test.parquet"}


def test_reregistering_with_another_location_or_column_map_is_a_conflict(hub_client):
    body = {"ref": DATASET, "kind": "dataset", "location": COLUMN}
    first = hub_client.post(f"{P}/hub/datasets", json=body)
    assert first.status_code == 201, first.text
    same = hub_client.post(f"{P}/hub/datasets", json={**body, "column_map": dict(MAP)})
    assert same.status_code == 201 and same.json()["id"] == first.json()["id"]
    moved = hub_client.post(f"{P}/hub/datasets", json={**body, "location": {"mode": "custom", "root": "gs://nmfs/t3"}})
    assert moved.status_code == 409
    assert moved.json()["detail"].startswith("This commit is already registered with location")
    remapped = hub_client.post(f"{P}/hub/datasets", json={**body, "column_map": {"image": "image_url"}})
    assert remapped.status_code == 409
    assert "columns" in remapped.json()["detail"]


def test_resuming_a_failed_import_skips_the_rows_that_landed(hub_client, monkeypatch):
    from fastapi import HTTPException as Hx

    from curation import huggingface

    entry = _registered_dataset(hub_client)
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": "Resumable", "dataset_mode": "T3", "splits": ["train", "test"]}).json()
    project_id = created["project"]["id"]
    real = huggingface.download

    def flaky(kind, repo_id, sha, path):
        if path.endswith("test.parquet"):
            raise Hx(502, "timed out")
        return real(kind, repo_id, sha, path)

    monkeypatch.setattr(huggingface, "download", flaky)
    _drain(created["operation"]["id"])
    monkeypatch.setattr(huggingface, "download", real)
    resume = f"{P}/hub/datasets/{entry['id']}/projects/{project_id}/imports"
    response = hub_client.post(resume)
    assert response.status_code == 202, response.text
    operation = response.json()
    assert operation["sources"][0]["kind"] == "hub_dataset"
    assert operation["sources"][0]["splits"] == ["train", "test"]
    busy = hub_client.post(resume)
    assert busy.status_code == 409
    _drain(operation["id"])
    done = hub_client.get(f"{P}/projects/{project_id}/imports/{operation['id']}").json()
    assert done["status"] == "completed", done
    assert (done["imported_rows"], done["skipped_rows"]) == (3, 3)
    assert hub_client.get(f"{P}/projects/{project_id}").json()["item_count"] == 6


def test_resume_needs_the_dataset_the_project_came_from(hub_client):
    entry = _registered_dataset(hub_client)
    other = hub_client.post(f"{P}/hub/datasets", json={"ref": f"{DATASET}@{'1' * 40}", "kind": "dataset",
                                                      "location": COLUMN}).json()
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": "Mine", "dataset_mode": "T3", "splits": ["test"]}).json()
    _drain(created["operation"]["id"])
    response = hub_client.post(f"{P}/hub/datasets/{other['id']}/projects/{created['project']['id']}/imports")
    assert response.status_code == 422
    assert "not created from this dataset" in response.json()["detail"]


def test_resume_follows_project_membership(team, monkeypatch, hub_files):
    install_fake_hub(monkeypatch, {DATASET: dataset_info()}, hub_files)
    pid = team["ana"].post(f"{P}/projects", json={"name": "Resume roles"}).json()["id"]
    resume = f"{P}/hub/datasets/1/projects/{pid}/imports"
    assert team["vic"].post(resume).status_code == 403
    assert team["ben"].post(resume).status_code == 404  # an app-wide curator: an open project is his to curate
    team["ana"].put(f"{P}/projects/{pid}/access", json={"restricted": True})
    assert team["ben"].post(resume).status_code == 403  # members-only, and he is not a member
    assert team["ana"].post(resume).status_code == 404  # a member: past the role check; there is no entry 1
    team["ana"].post(f"{P}/projects/{pid}/members", json={"username": "vic", "role": "curator"})
    assert team["vic"].post(resume).status_code == 404  # a project curator, though an app-wide viewer


def test_model_policy_is_checked_before_any_weights_download(hub_client, monkeypatch):
    from curation import huggingface

    monkeypatch.delenv("CURATION_ALLOW_LOCAL_MODELS", raising=False)
    monkeypatch.setattr(huggingface, "download", lambda *a: pytest.fail("weights must not be downloaded"))
    response = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                       "weight_file": "weights/best.pt", "trusted": True})
    assert response.status_code == 403


def test_a_long_task_name_fits_the_column(hub_client, monkeypatch, hub_files):
    from dataclasses import replace

    long_task = "image-classification-with-a-very-long-made-up-suffix"
    odd = replace(model_info(), repo_id="NMFS-OSI/odd", card={"pipeline_tag": long_task, "library_name": "x"})
    install_fake_hub(monkeypatch, {"NMFS-OSI/odd": odd}, hub_files)
    response = hub_client.post(f"{P}/hub/models", json={"ref": "NMFS-OSI/odd", "kind": "model"})
    assert response.status_code == 201, response.text
    assert response.json()["task"] == long_task[:40]


def test_the_requested_ref_is_the_ref_not_the_pasted_link(hub_client):
    entry = hub_client.post(f"{P}/hub/datasets", json={
        "ref": f"https://huggingface.co/datasets/{DATASET}/tree/main", "location": COLUMN}).json()
    assert entry["requested_ref"] == "main"


def test_manual_points_are_refused_on_a_patch_with_no_source_image(hub_client):
    entry = _registered_dataset(hub_client)
    created = hub_client.post(f"{P}/hub/datasets/{entry['id']}/projects",
                              json={"name": "Patches", "dataset_mode": "T3", "splits": ["test"]}).json()
    _drain(created["operation"]["id"])
    project_id = created["project"]["id"]
    anchor = hub_client.get(f"{P}/projects/{project_id}/items?limit=1").json()["items"][0]
    response = hub_client.post(f"{P}/projects/{project_id}/source-images/{anchor['id']}/points",
                               json={"row_px": 10, "column_px": 10, "label": "ACAS"})
    assert response.status_code == 422
    assert response.json()["detail"] == "This patch has no source image; add points on an item linked to its photo"
def test_changelog_sets_derived_from_apart_and_marks_partial_links():
    from types import SimpleNamespace

    from curation import versions

    project = SimpleNamespace(name="P", source_config={"hub_links": [
        {"repo_id": DATASET, "revision": DATASET_SHA, "partial": True}]})
    version = SimpleNamespace(version=1, changes={}, statistics={}, notes=None, created_by_name="Ana",
                              item_count=0, contributors=[])
    lines = versions.changelog_markdown(project, version).splitlines()
    index = next(n for n, line in enumerate(lines) if line.startswith("Derived from:"))
    assert lines[index - 1] == ""
    assert lines[index] == f"Derived from: {DATASET}@{DATASET_SHA[:8]} (linked) (partial)."




def test_a_database_error_while_recording_a_failure_does_not_kill_the_worker(hub_client, monkeypatch):
    from curation import huggingface, registry_worker

    entry = hub_client.post(f"{P}/hub/models", json={"ref": MODEL, "kind": "model",
                                                     "weight_file": "weights/best.pt", "trusted": True}).json()

    def down(*args):
        raise RuntimeError("download broke")

    real_scope = registry_worker.session_scope
    calls = []

    def flaky_scope():
        calls.append(1)
        if len(calls) == 2:  # the session that records the failure
            raise RuntimeError("database gone")
        return real_scope()

    assert registry_worker.claim_next() == entry["id"]
    monkeypatch.setattr(huggingface, "download", down)
    monkeypatch.setattr(registry_worker, "session_scope", flaky_scope)
    registry_worker.process(entry["id"])  # must not raise


def test_the_worker_loop_survives_a_crashing_registration(monkeypatch):
    from curation import registry_worker

    def crash(entry_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(registry_worker, "process", crash)
    registry_worker.registry_worker._run_one(1)  # must not raise
