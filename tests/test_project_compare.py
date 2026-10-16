"""Comparing two projects, live or published, matched by crop file name and label."""
from __future__ import annotations

from test_curation import P, _add, _item_payload, _project, _test_buckets, client  # noqa: F401
from test_versions import _publish


def _crop(key, label, split, file_name=None):
    payload = {**_item_payload(key, label), "split": split}
    if file_name:
        payload["file_name"] = file_name
    return payload


def test_compare_two_live_projects(client):
    a = _project(client, "Side A")["id"]
    b = _project(client, "Side B")["id"]
    _add(client, a, _crop("a1", "ACBR", "train", "x1.png"))
    _add(client, a, _crop("a2", "ACBR", "train", "x2.png"))
    _add(client, a, _crop("a3", "POCS", "val", "x3.png"))
    _add(client, b, _crop("b1", "ACBR", "test", "x1.png"))     # same crop, other split
    a2_in_b = _add(client, b, _crop("b2", "ACBR", "train", "x2.png"))
    _add(client, b, _crop("b4", "SAND", "train", "x4.png"))    # only in B
    # relabel B's copy of x2 → label disagreement (source_label still ACBR, so it still matches)
    client.patch(f"{P}/projects/{b}/items/{a2_in_b['id']}", json={"expected_revision": a2_in_b["revision"], "current_label": "POCS"})
    body = client.get(f"{P}/compare-projects?a={a}&a_version=live&b={b}&b_version=live").json()
    assert (body["a"]["count"], body["b"]["count"], body["matched"], body["only_a"], body["only_b"]) == (3, 3, 2, 1, 1)
    assert body["label_disagreements"]["count"] == 1
    assert body["label_disagreements"]["pairs"] == [{"a_label": "ACBR", "b_label": "POCS", "count": 1}]
    assert body["split_disagreements"]["pairs"] == [{"a_split": "train", "b_split": "test", "count": 1}]
    classes = {row["label"]: row for row in body["classes"]}
    assert classes["SAND"] == {"label": "SAND", "a": 0, "b": 1, "diff": 1}


def test_repeats_inside_one_side_do_not_inflate_counts(client):
    a = _project(client, "Repeat A")["id"]
    b = _project(client, "Repeat B")["id"]
    _add(client, a, _crop("r1", "ACBR", "train", "same.png"))
    _add(client, b, _crop("r2", "ACBR", "train", "same.png"))
    _add(client, b, _crop("r3", "ACBR", "train", "same.png"))  # B holds the crop twice
    body = client.get(f"{P}/compare-projects?a={a}&a_version=live&b={b}&b_version=live").json()
    assert (body["matched"], body["only_a"], body["only_b"]) == (1, 0, 0)


def test_compare_a_published_version_and_bad_pairs(client):
    a = _project(client, "Versioned A")["id"]
    b = _project(client, "T1 B", mode="T1")["id"]
    _add(client, a, _crop("v1", "ACBR", "train", "v1.png"))
    _publish(client, a)
    ok = client.get(f"{P}/compare-projects?a={a}&a_version=1&b={a}&b_version=live")
    assert ok.status_code == 200 and ok.json()["matched"] == 1
    assert client.get(f"{P}/compare-projects?a={a}&a_version=live&b={a}&b_version=live").status_code == 422
    assert client.get(f"{P}/compare-projects?a={a}&a_version=live&b={b}&b_version=live").status_code == 422  # modes differ
    assert client.get(f"{P}/compare-projects?a={a}&a_version=7&b={a}&b_version=live").status_code == 404


def test_a_non_ascii_digit_version_is_a_422_not_a_500(client):
    a = _project(client, "Superscript")["id"]
    for ref in ("%C2%B2", "%D9%A3"):   # "²" and Arabic-Indic "٣": str.isdigit() says yes, int() disagrees
        response = client.get(f"{P}/compare-projects?a={a}&a_version={ref}&b={a}&b_version=live")
        assert response.status_code == 422, response.text


def test_a_version_without_frozen_inputs_is_refused(client):
    from appdb import session_scope
    from curation.models import CurationVersionInput

    a = _project(client, "Legacy compare")["id"]
    _add(client, a, _crop("l1", "ACBR", "train", "l1.png"))
    _publish(client, a)
    with session_scope() as db:
        db.query(CurationVersionInput).delete()
    response = client.get(f"{P}/compare-projects?a={a}&a_version=1&b={a}&b_version=live")
    assert response.status_code == 422, response.text
    assert "publish a new version first" in response.json()["detail"]


def test_an_empty_published_version_is_not_treated_as_legacy(client):
    from appdb import session_scope
    from curation.models import CurationDatasetVersion

    a = _project(client, "Empty compare")["id"]
    with session_scope() as db:
        db.add(CurationDatasetVersion(project_id=a, version=1, status="published", item_count=0))
    response = client.get(f"{P}/compare-projects?a={a}&a_version=1&b={a}&b_version=live")
    assert response.status_code == 200, response.text
