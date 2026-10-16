# tests/test_starter_data.py
"""Starter data on a new install: the regional project, the Hugging Face dataset and model."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from hub_fakes import dataset_info, install_fake_hub, model_info
from test_curation import ADMIN, _test_buckets, db_session, team  # noqa: F401 — fixtures
from test_registry import DATASET, MODEL, hub_files  # noqa: F401 — fixtures

HUB_NAME = "NOAA Pacific T3 (Hugging Face)"
REGIONAL_NAME = "NOAA Pacific T3"


@pytest.fixture
def starter(team, monkeypatch, hub_files):  # noqa: F811
    from curation import storage

    monkeypatch.setenv("CURATION_STARTER_DATA", "noaa-t3")
    monkeypatch.setenv("CURATION_IMAGE_BUCKETS", "nmfs,bucket")
    monkeypatch.setenv("CURATION_ALLOW_LOCAL_MODELS", "on")
    monkeypatch.setenv("CURATION_IMPORT_SOURCES",
                       '[{"name": "R", "root_uri": "gs://nmfs/r/", "csv_uri": "gs://nmfs/r/a.csv"}]')
    install_fake_hub(monkeypatch, {DATASET: dataset_info(), MODEL: model_info()}, hub_files)
    monkeypatch.setattr(storage, "read_bytes", lambda uri: b"png")
    return team


def _run():
    from appdb import session_scope
    from curation import starter_data

    with session_scope() as db:
        return starter_data.ensure_once(db)


def _counts():
    from appdb import session_scope
    from curation.models import CurationHubEntry, CurationImportOperation, CurationProject

    with session_scope() as db:
        return {"projects": db.scalar(select(func.count()).select_from(CurationProject)),
                "operations": db.scalar(select(func.count()).select_from(CurationImportOperation)),
                "entries": db.scalar(select(func.count()).select_from(CurationHubEntry))}


def _project(name):
    from appdb import session_scope
    from curation.models import CurationProject

    with session_scope() as db:
        project = db.scalar(select(CurationProject).where(CurationProject.name == name))
        return project and (project.id, dict(project.source_config), project.owner_user_id)


def _admin_user():
    from appdb import session_scope
    from auth.models import User
    from auth.service import snapshot

    with session_scope() as db:
        return snapshot(db.scalar(select(User).where(User.username == ADMIN[0])))


def _delete(name):
    from appdb import session_scope
    from curation import service
    from curation.models import CurationImportOperation, CurationProject

    with session_scope() as db:
        project = db.scalar(select(CurationProject).where(CurationProject.name == name))
        for operation in db.scalars(select(CurationImportOperation).where(CurationImportOperation.project_id == project.id)):
            operation.status = "completed"
        db.commit()
        service.delete_project(db, project, name, _admin_user())


def _set_operations(project_id, status):
    from appdb import session_scope
    from curation.models import CurationImportOperation

    with session_scope() as db:
        for operation in db.scalars(select(CurationImportOperation).where(CurationImportOperation.project_id == project_id)):
            operation.status = status


def test_first_run_creates_both_projects_registers_both_and_queues_imports(starter):
    from appdb import session_scope
    from curation.models import CurationHubEntry, CurationImportOperation

    result = _run()
    assert result["pending"] is True and result["waiting"] is True  # the baseline waits for the imports
    assert result["parts"] == {"regional": "created", "dataset": "created", "model": "registered", "context": "waiting", "baseline": "waiting"}
    regional, hub = _project(REGIONAL_NAME), _project(HUB_NAME)
    assert regional[1]["starter"] == "noaa-t3-regional" and hub[1]["starter"] == "noaa-t3-hub"
    assert regional[2] == hub[2] and regional[2] is not None
    with session_scope() as db:
        ops = {op.project_id: op for op in db.scalars(select(CurationImportOperation))}
        assert [source["name"] for source in ops[regional[0]].sources] == ["R"]
        assert ops[hub[0]].sources[0]["kind"] == "hub_dataset" and ops[hub[0]].status == "queued"
        entries = {entry.kind: entry for entry in db.scalars(select(CurationHubEntry))}
        assert entries["dataset"].options["location"] == {"mode": "column"}
        assert entries["model"].status == "queued"
        assert entries["model"].options["weight_file"] == "weights/best.pt"


def test_second_run_changes_nothing(starter):
    _run()
    before = _counts()
    again = _run()
    assert again["parts"] == {"regional": "ok", "dataset": "ok", "model": "ok", "context": "waiting", "baseline": "waiting"}
    assert _counts() == before


def test_an_empty_regional_project_whose_import_failed_is_queued_again(starter):
    from appdb import session_scope
    from curation.models import CurationImportOperation

    _run()
    regional_id = _project(REGIONAL_NAME)[0]
    _set_operations(regional_id, "failed")
    assert _run()["parts"]["regional"] == "requeued"
    assert _run()["parts"]["regional"] == "ok"  # the new one is queued: left alone
    with session_scope() as db:
        statuses = sorted(db.scalars(select(CurationImportOperation.status).where(
            CurationImportOperation.project_id == regional_id)))
    assert statuses == ["failed", "queued"]


def test_an_empty_hub_project_whose_import_was_interrupted_is_resumed(starter):
    from appdb import session_scope
    from curation.models import CurationImportOperation

    _run()
    hub_id = _project(HUB_NAME)[0]
    _set_operations(hub_id, "interrupted")
    assert _run()["parts"]["dataset"] == "requeued"
    with session_scope() as db:
        queued = db.scalars(select(CurationImportOperation).where(
            CurationImportOperation.project_id == hub_id, CurationImportOperation.status == "queued")).all()
    assert len(queued) == 1 and queued[0].sources[0]["splits"]


def test_a_failed_model_registration_is_queued_again(starter):
    from appdb import session_scope
    from curation.models import CurationHubEntry

    _run()
    with session_scope() as db:
        entry = db.scalar(select(CurationHubEntry).where(CurationHubEntry.kind == "model"))
        entry.status, entry.error_message = "failed", "download broke"
    assert _run()["parts"]["model"] == "requeued"
    with session_scope() as db:
        entry = db.scalar(select(CurationHubEntry).where(CurationHubEntry.kind == "model"))
        assert (entry.status, entry.error_message) == ("queued", None)


def test_a_deleted_starter_project_is_not_created_again(starter):
    _run()
    _delete(REGIONAL_NAME)
    _delete(HUB_NAME)
    result = _run()
    assert result["parts"] == {"regional": "removed", "dataset": "removed", "model": "ok", "context": "removed", "baseline": "removed"}
    assert _project(REGIONAL_NAME) is None and _project(HUB_NAME) is None
    assert _counts()["projects"] == 0


def test_deleting_an_ordinary_project_records_nothing(starter):
    from appdb import session_scope
    from appdb.bootstrap import _ledger
    from curation import service
    from curation.models import CurationProject

    with session_scope() as db:
        db.add(CurationProject(name="mine"))
        db.commit()
        service.delete_project(db, db.scalar(select(CurationProject)), "mine", _admin_user())
        assert not [row for row in db.execute(select(_ledger.c.migration_id)).scalars() if row.startswith("starter-")]


def test_policy_off_skips_the_model_without_error(starter, monkeypatch):
    monkeypatch.delenv("CURATION_ALLOW_LOCAL_MODELS")
    monkeypatch.setenv("OCEANFRAME_AUTH", "off")
    result = _run()
    assert result["parts"] == {"regional": "created", "dataset": "created", "model": "skipped", "context": "waiting", "baseline": "skipped"}
    assert result["pending"] is True  # no model, but the patches still get their source images once imported
    assert _counts()["entries"] == 1


def test_an_unreachable_hub_leaves_dataset_and_model_pending_and_raises_nothing(starter, monkeypatch):
    from curation import huggingface

    def down(ref):
        raise HTTPException(502, "Hugging Face did not answer: offline")

    monkeypatch.setattr(huggingface, "repo_info", down)
    result = _run()
    assert result["pending"] is True
    assert result["parts"] == {"regional": "created", "dataset": "pending", "model": "pending", "context": "waiting", "baseline": "waiting"}
    assert _counts()["entries"] == 0


def test_pending_parts_finish_when_the_hub_comes_back(starter, monkeypatch):
    from curation import huggingface

    real = huggingface.repo_info

    def down(ref):
        raise HTTPException(502, "offline")

    monkeypatch.setattr(huggingface, "repo_info", down)
    assert _run()["pending"] is True
    monkeypatch.setattr(huggingface, "repo_info", real)
    result = _run()
    assert result["parts"] == {"regional": "ok", "dataset": "created", "model": "registered", "context": "waiting", "baseline": "waiting"}


def test_a_project_seeded_by_the_old_code_is_marked_not_duplicated(starter):
    from appdb import session_scope
    from appdb.base import utcnow
    from appdb.bootstrap import _ledger
    from curation.models import CurationImportOperation, CurationProject

    with session_scope() as db:
        project = CurationProject(name=REGIONAL_NAME, source_config={"dataset_mode": "T3"}, item_count=900)
        db.add(project)
        db.flush()
        db.add(CurationImportOperation(project_id=project.id, status="completed", sources=[]))
        db.execute(_ledger.insert().values(migration_id="seed-noaa-pacific", description="x", applied_at=utcnow()))
    assert _run()["parts"]["regional"] == "ok"
    assert _project(REGIONAL_NAME)[1]["starter"] == "noaa-t3-regional"
    assert _run()["parts"]["regional"] == "ok"
    assert _counts()["projects"] == 2  # the old one plus the Hugging Face project


def test_an_old_seed_ledger_without_its_project_counts_as_deleted(starter):
    from appdb import session_scope
    from appdb.base import utcnow
    from appdb.bootstrap import _ledger

    with session_scope() as db:
        db.execute(_ledger.insert().values(migration_id="seed-noaa-pacific", description="x", applied_at=utcnow()))
    assert _run()["parts"]["regional"] == "removed"
    assert _project(REGIONAL_NAME) is None


def test_off_does_nothing(starter, monkeypatch):
    monkeypatch.setenv("CURATION_STARTER_DATA", "off")
    assert _run() == {"mode": "off", "parts": {}, "pending": False}
    assert _counts() == {"projects": 0, "operations": 0, "entries": 0}


def test_the_old_seed_setting_still_seeds_the_regional_project(starter, monkeypatch):
    monkeypatch.delenv("CURATION_STARTER_DATA")
    monkeypatch.setenv("CURATION_SEED", "noaa-pacific")
    result = _run()
    assert result["mode"] == "regional" and result["parts"] == {"regional": "created"}
    assert _project(REGIONAL_NAME) is not None and _project(HUB_NAME) is None
    assert _counts()["entries"] == 0


def test_hub_mode_leaves_the_regional_project_out(starter, monkeypatch):
    monkeypatch.setenv("CURATION_STARTER_DATA", "hub")
    assert set(_run()["parts"]) == {"dataset", "model", "baseline"}
    assert _project(REGIONAL_NAME) is None


def test_default_setting_is_off_and_unknown_values_warn_and_mean_off(monkeypatch, caplog):
    from curation import settings

    monkeypatch.delenv("CURATION_STARTER_DATA")
    monkeypatch.delenv("CURATION_SEED", raising=False)
    assert settings.starter_data() == "off"
    monkeypatch.setenv("CURATION_SEED", "")  # an older installer's "no"
    assert settings.starter_data() == "off"
    monkeypatch.setenv("CURATION_SEED", "noaa-pacific")
    assert settings.starter_data() == "regional"
    for value, expected in (("noaa-t3", "noaa-t3"), ("REGIONAL", "regional"), ("hub", "hub"), ("off", "off")):
        monkeypatch.setenv("CURATION_STARTER_DATA", value)
        assert settings.starter_data() == expected  # the new setting beats the old alias
    monkeypatch.setenv("CURATION_STARTER_DATA", "nonsense")
    with caplog.at_level("WARNING"):
        assert settings.starter_data() == "off"
        assert settings.starter_data() == "off"
    assert len([r for r in caplog.records if "nonsense" in r.getMessage()]) == 1

def test_the_background_loop_retries_until_nothing_is_pending(library, monkeypatch):
    import threading

    from curation import starter_data

    calls = []

    def fake(db, gave_up=None):
        calls.append(1)
        return {"pending": len(calls) < 3}

    monkeypatch.setattr(starter_data, "ensure_once", fake)
    starter_data._loop(threading.Event(), 0.01, 0.02)  # noqa: SLF001
    assert len(calls) == 3


def test_bootstrap_never_starts_the_thread(library, monkeypatch):
    from curation import importer, starter_data

    monkeypatch.setenv("CURATION_STARTER_DATA", "noaa-t3")
    monkeypatch.setattr(starter_data, "start_background", lambda: pytest.fail("bootstrap must not start it"))
    monkeypatch.setenv("OCEANFRAME_BACKGROUND_WORKERS", "on")
    importer.on_bootstrap()


def test_the_lifespan_starts_and_stops_it_on_the_coordinator_only(library, monkeypatch):
    from fastapi.testclient import TestClient

    import main
    from curation import starter_data

    for flag, expected in (("off", []), ("on", ["start", "stop"])):
        calls = []
        monkeypatch.setattr(starter_data, "start_background", lambda calls=calls: calls.append("start"))
        monkeypatch.setattr(starter_data, "stop_background", lambda calls=calls: calls.append("stop"))
        for name in ("curation_import_pool", "evaluation_pool", "registry_worker"):
            pool = getattr(main, name)
            monkeypatch.setattr(pool, "start", lambda: None)
            monkeypatch.setattr(pool, "stop", lambda *args: None)
        monkeypatch.setenv("OCEANFRAME_BACKGROUND_WORKERS", flag)
        with TestClient(main.app):
            pass
        assert calls == expected


def test_a_user_made_project_linked_to_the_dataset_is_adopted_and_stays_deleted(starter):
    from appdb import session_scope
    from curation import registry
    from curation.models import CurationHubEntry

    _run()
    _delete(HUB_NAME)  # the starter's own hub project is gone ...
    with session_scope() as db:
        entry = db.scalar(select(CurationHubEntry).where(CurationHubEntry.kind == "dataset"))
        registry.create_project_from_dataset(db, _admin_user(), entry, name="Mine from HF", description=None,
                                             dataset_mode="T3", splits=[])
    assert "starter" not in _project("Mine from HF")[1]
    assert _run()["parts"]["dataset"] == "ok"
    assert _project("Mine from HF")[1]["starter"] == "noaa-t3-hub"
    assert _project(HUB_NAME) is None
    _delete("Mine from HF")
    assert _run()["parts"]["dataset"] == "removed"
    assert _project(HUB_NAME) is None and _project("Mine from HF") is None


def test_the_hub_project_is_marked_in_the_same_commit_it_is_created(starter, monkeypatch):
    from curation import starter_data

    def boom(*args, **kwargs):
        raise AssertionError("_mark must not be needed for a fresh project")

    monkeypatch.setattr(starter_data, "_mark", boom)
    assert _run()["parts"]["dataset"] == "created"
    assert _project(HUB_NAME)[1]["starter"] == "noaa-t3-hub"


def test_a_refusal_is_reported_once_and_not_retried_but_a_5xx_is(starter, monkeypatch, caplog):
    from appdb import session_scope
    from curation import huggingface, starter_data

    calls = []

    def refuse(ref):
        calls.append(ref.repo_id)
        raise HTTPException(422, "Not an allowed owner")

    monkeypatch.setattr(huggingface, "repo_info", refuse)
    gave_up = set()
    with caplog.at_level("WARNING"):
        with session_scope() as db:
            first = starter_data.ensure_once(db, gave_up)
        with session_scope() as db:
            second = starter_data.ensure_once(db, gave_up)
    assert first["parts"]["dataset"] == first["parts"]["model"] == "attention"
    assert first["pending"] is False and second["pending"] is False
    assert len(calls) == 2  # one try each for the dataset and the model, none on the second pass
    assert gave_up == {"dataset", "model"}
    assert sum("needs attention" in record.getMessage() for record in caplog.records) == 2

    def busy(ref):
        raise HTTPException(429, "slow down")

    monkeypatch.setattr(huggingface, "repo_info", busy)
    with session_scope() as db:
        assert starter_data.ensure_once(db)["pending"] is True


# ── The baseline evaluation ───────────────────────────────────────────────────

@pytest.fixture
def baseline(starter, monkeypatch):
    """The starter after its imports finished and its model downloaded, with a live worker.
    Publishing and starting the run are recorded instead of done (their own suites cover them)."""
    from appdb import session_scope
    from curation import evaluation_runner, model_registry, versions
    from curation.models import (CurationClassifier, CurationDatasetVersion, CurationEvaluationRun, CurationHubEntry,
                                 CurationProject)

    _run()
    hub_id = _project(HUB_NAME)[0]
    _set_operations(hub_id, "completed")
    _set_operations(_project(REGIONAL_NAME)[0], "completed")
    with session_scope() as db:
        db.get(CurationProject, hub_id).item_count = 10
        model = CurationClassifier(name="t3", sha256="0" * 64, size_bytes=1, status="staged", class_names=["A"])
        db.add(model)
        db.flush()
        db.scalar(select(CurationHubEntry).where(CurationHubEntry.kind == "model")).classifier_id = model.id
        model_id = model.id
    calls = {"publish": 0, "validate": 0, "runs": []}

    def publish(db, project, user, notes):
        calls["publish"] += 1
        version = CurationDatasetVersion(project_id=project.id, version=1, status="published")
        db.add(version)
        db.commit()
        return version

    def validate(db, model):
        calls["validate"] += 1
        model.status = "ready"
        db.commit()

    def start_run(db, project_id, *, version_number, classifier_id, splits, image_size, batch_size, device, user):
        version = db.scalar(select(CurationDatasetVersion).where(CurationDatasetVersion.project_id == project_id))
        run = CurationEvaluationRun(project_id=project_id, version_id=version.id, classifier_id=classifier_id)
        db.add(run)
        db.commit()
        calls["runs"].append({"splits": splits, "device": device, "version": version_number})
        return run

    monkeypatch.setattr(versions, "start_publish", publish)
    monkeypatch.setattr(model_registry, "submit_validation", validate)
    monkeypatch.setattr(evaluation_runner, "start_run", start_run)
    monkeypatch.setattr(model_registry, "worker_available", lambda: True)
    monkeypatch.setattr(model_registry, "worker_gpus", lambda: 1)
    return {"calls": calls, "hub_id": hub_id, "model_id": model_id}


def test_baseline_validates_publishes_then_starts_one_run_on_test_and_val(baseline):
    calls = baseline["calls"]
    assert _run()["parts"]["baseline"] == "waiting" and calls["validate"] == 1
    assert _run()["parts"]["baseline"] == "waiting" and calls["publish"] == 1
    result = _run()
    assert result["parts"]["baseline"] == "started"
    assert calls["runs"] == [{"splits": ["test", "val"], "device": "0", "version": 1}]
    assert _project(HUB_NAME)[1]["baseline_run"]
    again = _run()
    assert again["parts"]["baseline"] == "ok" and not again["pending"]
    assert (calls["publish"], len(calls["runs"])) == (1, 1)


def test_baseline_waits_for_the_import_and_the_worker(baseline, monkeypatch):
    from curation import model_registry

    _set_operations(baseline["hub_id"], "running")
    assert _run()["parts"]["baseline"] == "waiting" and baseline["calls"]["validate"] == 0
    _set_operations(baseline["hub_id"], "completed")
    monkeypatch.setattr(model_registry, "worker_available", lambda: False)
    assert _run()["parts"]["baseline"] == "waiting" and baseline["calls"]["validate"] == 0


def test_baseline_uses_an_existing_run_rather_than_starting_another(baseline):
    from appdb import session_scope
    from curation.models import CurationDatasetVersion, CurationEvaluationRun

    with session_scope() as db:
        version = CurationDatasetVersion(project_id=baseline["hub_id"], version=1, status="published")
        db.add(version)
        db.flush()
        db.add(CurationEvaluationRun(project_id=baseline["hub_id"], version_id=version.id,
                                     classifier_id=baseline["model_id"]))
    _run()  # validates
    assert _run()["parts"]["baseline"] == "started"
    assert baseline["calls"]["runs"] == [] and baseline["calls"]["publish"] == 0


def test_a_baseline_publish_that_failed_is_not_retried_by_itself(baseline):
    from appdb import session_scope
    from curation.models import CurationDatasetVersion

    _run()
    _run()  # publishes v1
    with session_scope() as db:
        db.scalar(select(CurationDatasetVersion)).status = "failed"
    result = _run()
    assert result["parts"]["baseline"] == "attention" and baseline["calls"]["publish"] == 1


def test_a_starter_import_cut_off_by_a_restart_resumes_even_when_partly_loaded(starter):
    from appdb import session_scope
    from curation.models import CurationProject

    _run()
    regional_id = _project(REGIONAL_NAME)[0]
    with session_scope() as db:
        db.get(CurationProject, regional_id).item_count = 136000
    _set_operations(regional_id, "canceled")  # a curator's cancel of a partial import is respected
    assert _run()["parts"]["regional"] == "ok"
    _set_operations(regional_id, "interrupted")
    assert _run()["parts"]["regional"] == "requeued"


def test_starter_accounts_become_members_of_the_starter_projects(starter, monkeypatch):
    from appdb import session_scope
    from auth.service import ensure_starter_users

    monkeypatch.setenv("OCEANFRAME_STARTER_USERS", "test:lead:lead-pass-1,user1:curator:curator-pass-1,"
                                                   "watcher:viewer:viewer-pass-1")
    with session_scope() as db:
        ensure_starter_users(db)
    _run()
    for name in (REGIONAL_NAME, HUB_NAME):
        listed = {row["username"]: row["role"] for row in _members(_project(name)[0])}
        assert listed.get("test") == "lead" and listed.get("user1") == "curator" and "watcher" not in listed


def _members(project_id):
    from appdb import session_scope
    from curation import members

    with session_scope() as db:
        return members.list_members(db, project_id)


def _queue_check(project_id, status="queued"):
    from appdb import session_scope
    from curation import availability
    from curation.models import CurationImportOperation

    with session_scope() as db:
        db.add(CurationImportOperation(project_id=project_id, status=status, sources=[dict(availability.SOURCE)]))


def test_the_availability_check_queued_after_an_import_does_not_hold_up_the_baseline(baseline):
    _queue_check(baseline["hub_id"])
    _run()  # validates
    assert _run()["parts"]["baseline"] == "waiting" and baseline["calls"]["publish"] == 1


def test_an_interrupted_availability_check_is_not_an_interrupted_import(baseline):
    _queue_check(baseline["hub_id"], status="interrupted")
    assert _run()["parts"]["dataset"] == "ok"


def test_a_baseline_run_cut_off_by_a_restart_is_started_again_but_a_failed_one_is_not(baseline):
    from appdb import session_scope
    from curation.models import CurationEvaluationRun

    def set_status(status):
        with session_scope() as db:
            for run in db.scalars(select(CurationEvaluationRun)):
                run.status = status

    _run(), _run()
    assert _run()["parts"]["baseline"] == "started"
    set_status("interrupted")
    assert _run()["parts"]["baseline"] == "started" and len(baseline["calls"]["runs"]) == 2
    set_status("failed")
    assert _run()["parts"]["baseline"] == "ok" and len(baseline["calls"]["runs"]) == 2


# ── Source context for the Hugging Face patches ───────────────────────────────

def _item(project_id, file_name, label, **fields):
    from curation.models import CurationItem

    uri = fields.pop("source_image_uri", f"gs://nmfs/hf/test/{label}/{file_name}")
    return CurationItem(project_id=project_id, source_key=f"{project_id}:{file_name}", source_image_uri=uri,
                        crop_uri=fields.pop("crop_uri", uri), file_name=file_name, source_label=label,
                        current_label=label, split=fields.pop("split", "test"), **fields)


@pytest.fixture
def two_imports(starter):
    """Both starter projects imported: a regional point and its Hugging Face patch, plus a patch with no match."""
    from appdb import session_scope

    _run()
    hub_id, regional_id = _project(HUB_NAME)[0], _project(REGIONAL_NAME)[0]
    with session_scope() as db:
        db.add(_item(regional_id, "ACAS_AGU-641_2025_A_09_9.png", "ACAS", split="train", row_px=120, column_px=340,
                     source_image_uri="gs://nmfs/r/images/AGU-641_2025_A_09.JPG", crop_uri="gs://nmfs/r/crops/x.png",
                     region="MARIAN", island="Agrihan", site="AGU-641", survey_year=2025, annotator="ann"))
        db.add(_item(hub_id, "ACAS_AGU-641_2025_A_09_9.png", "ACAS", metadata_json={"patch_only": True, "hub": {"row": 4}}))
        db.add(_item(hub_id, "ACAS_NOPE_1.png", "ACAS", metadata_json={"patch_only": True}))
    for project_id in (hub_id, regional_id):
        _set_operations(project_id, "completed")
    return {"hub_id": hub_id, "regional_id": regional_id}


def _hub_items(hub_id):
    from appdb import session_scope
    from curation.models import CurationItem

    with session_scope() as db:
        return {item.file_name: {column: getattr(item, column) for column in (
            "source_image_uri", "crop_uri", "row_px", "column_px", "region", "island", "site", "survey_year",
            "annotator", "split", "current_label", "revision", "metadata_json")}
            for item in db.scalars(select(CurationItem).where(CurationItem.project_id == hub_id))}


def test_hub_patches_take_their_source_image_and_point_from_the_regional_project_once(two_imports):
    from appdb import session_scope
    from curation import availability
    from curation.models import CurationImportOperation

    hub_id = two_imports["hub_id"]
    assert _run()["parts"]["context"] == "linked"
    items = _hub_items(hub_id)
    linked, alone = items["ACAS_AGU-641_2025_A_09_9.png"], items["ACAS_NOPE_1.png"]
    assert (linked["source_image_uri"], linked["row_px"], linked["column_px"]) == (
        "gs://nmfs/r/images/AGU-641_2025_A_09.JPG", 120, 340)
    assert (linked["region"], linked["island"], linked["site"], linked["survey_year"], linked["annotator"]) == (
        "MARIAN", "Agrihan", "AGU-641", 2025, "ann")
    # What is scored, its label, split and revision stay as the dataset has them.
    assert linked["crop_uri"] == "gs://nmfs/hf/test/ACAS/ACAS_AGU-641_2025_A_09_9.png"
    assert (linked["split"], linked["current_label"], linked["revision"]) == ("test", "ACAS", 1)
    assert linked["metadata_json"] == {"patch_only": False, "hub": {"row": 4}, "context_from": two_imports["regional_id"]}
    assert alone["row_px"] is None and alone["metadata_json"] == {"patch_only": True}
    assert _project(HUB_NAME)[1]["context_from"] == two_imports["regional_id"]
    with session_scope() as db:
        checks = [op for op in db.scalars(select(CurationImportOperation).where(CurationImportOperation.project_id == hub_id))
                  if availability.is_check(op)]
    assert len(checks) == 1 and checks[0].status == "queued"
    assert _run()["parts"]["context"] == "ok"


def test_hub_patches_wait_for_both_imports(two_imports):
    _set_operations(two_imports["regional_id"], "running")
    assert _run()["parts"]["context"] == "waiting"
    assert _hub_items(two_imports["hub_id"])["ACAS_AGU-641_2025_A_09_9.png"]["row_px"] is None


def test_linking_again_changes_nothing_and_pages_through_every_batch(two_imports, monkeypatch):
    from appdb import session_scope
    from curation import source_context

    monkeypatch.setattr(source_context, "BATCH", 1)
    with session_scope() as db:
        assert source_context.link(db, two_imports["hub_id"], two_imports["regional_id"]) == {"linked": 1, "unmatched": 1}
        assert source_context.link(db, two_imports["hub_id"], two_imports["regional_id"]) == {"linked": 0, "unmatched": 1}


def test_a_removed_regional_project_leaves_the_patches_as_they_are(two_imports):
    _delete(REGIONAL_NAME)
    assert _run()["parts"]["context"] == "removed"
    assert _hub_items(two_imports["hub_id"])["ACAS_AGU-641_2025_A_09_9.png"]["row_px"] is None
