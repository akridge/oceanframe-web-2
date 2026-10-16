"""Split breakdowns: included items per group and split, live and for a published version."""
from __future__ import annotations

from test_curation import P, _add, _item_payload, _project, _test_buckets, client, db_session, team  # noqa: F401
from test_versions import _publish

ROWS = [("b1", "ACBR", "train", "Hawaii"), ("b2", "ACBR", "train", "Hawaii"), ("b3", "ACBR", "val", "Mariana"),
        ("b4", "ACBR", "test", "Hawaii"), ("b5", "POCS", "train", "Mariana"), ("b6", "POCS", None, "Hawaii")]


def _seed(client, db_session):
    pid = _project(client, "Splits")["id"]
    for key, label, split, region in ROWS:
        payload = _item_payload(key, label, region=region)
        if split:
            payload["split"] = split
        _add(client, pid, payload)
    excluded = _add(client, pid, {**_item_payload("b7", "ACBR"), "split": "train"})
    client.patch(f"{P}/projects/{pid}/items/{excluded['id']}",
                 json={"expected_revision": excluded["revision"], "included": False})
    # The API always gives a split at creation; clear b6's so it is unassigned, as an import can leave it.
    from curation.models import CurationItem  # noqa: PLC0415

    db_session.query(CurationItem).filter_by(project_id=pid, source_key="b6").update({"split": None})
    db_session.commit()
    return pid


def test_live_breakdown_by_label_counts_included_items_per_split(client, db_session, monkeypatch):
    monkeypatch.setenv("CURATION_RARE_MIN", "1")
    pid = _seed(client, db_session)
    body = client.get(f"{P}/projects/{pid}/breakdown?by=label").json()
    rows = {row["key"]: row for row in body["rows"]}
    assert rows["ACBR"]["counts"] == {"train": 2, "val": 1, "test": 1, "unassigned": 0}
    assert rows["POCS"]["counts"] == {"train": 1, "val": 0, "test": 0, "unassigned": 1}
    assert (rows["ACBR"]["rare"], rows["POCS"]["rare"]) == (False, True)
    assert body["totals"] == {"train": 3, "val": 1, "test": 1, "unassigned": 1} and body["total"] == 6
    assert (body["source"], body["rare_min"], body["by"]) == ("live", 1, "label")
    assert [row["key"] for row in body["rows"]] == ["ACBR", "POCS"]  # largest first


def test_live_breakdown_by_region(client, db_session):
    pid = _seed(client, db_session)
    rows = {row["key"]: row["counts"] for row in client.get(f"{P}/projects/{pid}/breakdown?by=region").json()["rows"]}
    assert rows == {"Hawaii": {"train": 2, "val": 0, "test": 1, "unassigned": 1},
                    "Mariana": {"train": 1, "val": 1, "test": 0, "unassigned": 0}}


def test_missing_group_values_become_a_none_row(client):
    pid = _project(client, "No annotator")["id"]
    _add(client, pid, {**_item_payload("n1", "ACBR"), "split": "val"})
    rows = client.get(f"{P}/projects/{pid}/breakdown?by=annotator").json()["rows"]
    assert rows == [{"key": "(none)", "counts": {"train": 0, "val": 1, "test": 0, "unassigned": 0},
                     "total": 1, "rare": True}]


def test_version_breakdown_reads_the_frozen_rows(client, db_session):
    pid = _seed(client, db_session)
    _publish(client, pid)
    body = client.get(f"{P}/projects/{pid}/versions/1/breakdown?by=label").json()
    rows = {row["key"]: row for row in body["rows"]}
    assert body["source"] == 1 and body["total"] == 6 and body["totals"]["unassigned"] == 0
    assert rows["ACBR"]["counts"] == {"train": 2, "val": 1, "test": 1, "unassigned": 0}
    assert rows["POCS"]["total"] == 2  # b6 got a split when v1 was published
    by_region = client.get(f"{P}/projects/{pid}/versions/1/breakdown?by=region").json()
    assert {row["key"] for row in by_region["rows"]} == {"Hawaii", "Mariana"}


def test_breakdown_rejects_unknown_groups_and_versions(client, db_session):
    pid = _seed(client, db_session)
    assert client.get(f"{P}/projects/{pid}/breakdown?by=colour").status_code == 422
    assert client.get(f"{P}/projects/{pid}/versions/9/breakdown").status_code == 404


def test_breakdown_is_readable_by_viewers(team):
    pid = _project(team["admin"], "Viewer splits")["id"]
    assert team["vic"].get(f"{P}/projects/{pid}/breakdown").status_code == 200
