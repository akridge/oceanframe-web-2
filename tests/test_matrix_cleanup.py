"""Clean-up from the confusion matrix: preview a cell, apply one action to it, tally what is cleaned."""
from __future__ import annotations

from test_curation import P, _test_buckets, client  # noqa: F401
from test_evaluations import _completed_run


def _cell(client, pid, run_id, **params):
    response = client.get(f"{P}/projects/{pid}/evaluations/{run_id}/cleanup",
                          params={"actual": "ACBR", "predicted": "POCS", **params})
    assert response.status_code == 200, response.text
    return response.json()


def _item(client, pid, item_id):
    found = client.get(f"{P}/projects/{pid}/items", params={"item_ids": str(item_id), "removed": "any"}).json()["items"]
    assert len(found) == 1
    return found[0]


def _apply(client, pid, run_id, **body):
    return client.post(f"{P}/projects/{pid}/evaluations/{run_id}/cleanup",
                       json={"actual": "ACBR", "predicted": "POCS", **body})


def test_a_cell_is_relabelled_once_and_tallied(client, monkeypatch):
    pid, run_id, items = _completed_run(client, monkeypatch)  # train ACBR, val POCS, test ACBR; all called POCS
    preview = _cell(client, pid, run_id)
    assert preview["splits"] == ["train", "val"]  # test is opt-in
    assert preview["by_split"] == {"train": {"patches": 1, "cleaned": 0}} and preview["patches"] == 1
    applied = _apply(client, pid, run_id, action="relabel", watermark=preview["watermark"]).json()
    assert (applied["applied"], applied["already"], applied["conflict_count"]) == (1, 0, 0)
    train = _item(client, pid, items[0]['id'])
    assert train["current_label"] == "POCS" and train["review_state"] == "reviewed"
    assert _item(client, pid, items[2]['id'])["current_label"] == "ACBR"  # test untouched
    again = _apply(client, pid, run_id, action="relabel", watermark=_cell(client, pid, run_id)["watermark"]).json()
    assert (again["applied"], again["already"]) == (0, 1)
    tally = client.get(f"{P}/projects/{pid}/evaluations/{run_id}/cleanup/tally").json()
    assert tally["cells"] == [{"actual": "ACBR", "predicted": "POCS", "patches": 1, "cleaned": 1}]
    assert tally["classes"] == [{"actual": "ACBR", "patches": 1, "cleaned": 1}]
    events = client.get(f"{P}/projects/{pid}/events/export").text
    assert "Matrix clean-up: relabelled ACBR" in events


def test_test_patches_need_confirmation_and_are_marked_test_informed(client, monkeypatch):
    pid, run_id, items = _completed_run(client, monkeypatch)
    preview = _cell(client, pid, run_id, splits="test")
    assert preview["needs_confirm_test"] and preview["patches"] == 1
    refused = _apply(client, pid, run_id, action="exclude", splits=["test"], watermark=preview["watermark"])
    assert refused.status_code == 409
    done = _apply(client, pid, run_id, action="exclude", splits=["test"], watermark=preview["watermark"], confirm_test=True)
    assert done.json()["applied"] == 1
    test_item = _item(client, pid, items[2]['id'])
    assert test_item["included"] is False and test_item["metadata"]["test_informed"] is True


def test_a_patch_changed_after_the_preview_is_left_alone(client, monkeypatch):
    pid, run_id, items = _completed_run(client, monkeypatch)
    preview = _cell(client, pid, run_id)
    train = _item(client, pid, items[0]['id'])
    assert client.patch(f"{P}/projects/{pid}/items/{train['id']}",
                        json={"expected_revision": train["revision"], "review_state": "reviewed"}).status_code == 200
    applied = _apply(client, pid, run_id, action="flag", watermark=preview["watermark"]).json()
    assert (applied["applied"], applied["conflict_count"]) == (0, 1)
    assert _item(client, pid, train['id'])["review_state"] == "reviewed"
    fresh = _apply(client, pid, run_id, action="flag", watermark=_cell(client, pid, run_id)["watermark"]).json()
    assert fresh["applied"] == 1
    flagged = _item(client, pid, train['id'])
    assert flagged["review_state"] == "flagged" and flagged["metadata"]["resplit"] is True


def test_a_curators_earlier_decision_stands(client, monkeypatch):
    """A patch already relabelled to a third class (before the preview) is counted as cleaned and kept."""
    pid, run_id, items = _completed_run(client, monkeypatch)
    train = _item(client, pid, items[0]["id"])
    assert client.patch(f"{P}/projects/{pid}/items/{train['id']}",
                        json={"expected_revision": train["revision"], "current_label": "CCAH"}).status_code == 200
    preview = _cell(client, pid, run_id)
    assert (preview["patches"], preview["cleaned"]) == (1, 1)
    for action in ("relabel", "exclude", "flag"):
        result = _apply(client, pid, run_id, action=action, watermark=preview["watermark"]).json()
        assert (result["applied"], result["already"]) == (0, 1), action
    kept = _item(client, pid, train["id"])
    assert (kept["current_label"], kept["included"], kept["review_state"]) == ("CCAH", True, "unreviewed")


def test_flagging_test_patches_marks_them_test_informed(client, monkeypatch):
    pid, run_id, items = _completed_run(client, monkeypatch)
    preview = _cell(client, pid, run_id, splits="test")
    assert _apply(client, pid, run_id, action="flag", splits=["test"], watermark=preview["watermark"],
                  confirm_test=True).json()["applied"] == 1
    assert _item(client, pid, items[2]["id"])["metadata"]["test_informed"] is True


def test_the_diagonal_and_unknown_actions_are_refused(client, monkeypatch):
    pid, run_id, items = _completed_run(client, monkeypatch)
    same = client.get(f"{P}/projects/{pid}/evaluations/{run_id}/cleanup", params={"actual": "POCS", "predicted": "POCS"})
    assert same.status_code == 422
    assert _apply(client, pid, run_id, action="delete", watermark=0).status_code == 422
