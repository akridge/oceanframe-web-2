"""Comparing any two published versions, or a version with the live draft."""
from __future__ import annotations

from test_curation import P, _add, _project, _test_buckets, client, db_session, team  # noqa: F401
from test_versions import _on_image, _publish


def _two_versions(client, db_session):
    from curation.models import CurationItem

    pid = _project(client, "Compare")["id"]
    a = _add(client, pid, {**_on_image("c1", "ACBR", "img-1"), "split": "train"})
    b = _add(client, pid, {**_on_image("c2", "ACBR", "img-2"), "split": "train"})
    s = _add(client, pid, {**_on_image("c3", "SAND", "img-3"), "split": "val"})
    _publish(client, pid, "first")
    client.patch(f"{P}/projects/{pid}/items/{a['id']}", json={"expected_revision": a["revision"], "current_label": "POCS"})
    client.patch(f"{P}/projects/{pid}/items/{s['id']}", json={"expected_revision": s["revision"], "included": False})
    moved = db_session.get(CurationItem, b["id"])
    moved.split = "test"
    db_session.commit()
    _publish(client, pid, "second")
    return pid


def test_compare_two_versions_reports_relabels_exclusions_and_split_moves(client, db_session):
    pid = _two_versions(client, db_session)
    body = client.get(f"{P}/projects/{pid}/versions/compare?base=1&target=2").json()
    changes = body["changes"]
    assert (body["base"], body["target"]) == ("v1", "v2")
    assert (changes["before"], changes["after"], changes["in"], changes["out"],
            changes["relabelled"], changes["moved"]) == (3, 2, 0, 1, 1, 1)
    assert changes["split_moves"] == [{"from": "train", "to": "test", "count": 1}]
    per_class = {row["label"]: row for row in changes["per_class"]}
    assert per_class["ACBR"]["moved"] == 1 and per_class["ACBR"]["moved_to"] == {"test": 1}
    assert per_class["SAND"]["out"] == 1


def test_compare_with_live(client, db_session):
    pid = _two_versions(client, db_session)
    reverse = client.get(f"{P}/projects/{pid}/versions/compare?base=live&target=1").json()
    assert reverse["base"] == "live" and reverse["changes"]["in"] == 1  # SAND is excluded live, present in v1


def test_compare_refuses_bad_pairs(client, db_session):
    from curation.models import CurationDatasetVersion

    pid = _two_versions(client, db_session)
    url = f"{P}/projects/{pid}/versions/compare"
    assert client.get(f"{url}?base=1&target=1").status_code == 422
    assert client.get(f"{url}?base=7&target=1").status_code == 404
    assert client.get(f"{url}?base=abc&target=1").status_code == 422
    db_session.add(CurationDatasetVersion(project_id=pid, version=3, status="failed", item_count=0))
    db_session.commit()
    response = client.get(f"{url}?base=2&target=3")
    assert response.status_code == 422 and "not published" in response.json()["detail"]


def test_viewers_can_compare(team):
    pid = _project(team["admin"], "Viewer compare")["id"]
    assert team["vic"].get(f"{P}/projects/{pid}/versions/compare?base=live&target=1").status_code == 404
