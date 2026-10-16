"""Comparing two people's work: what each did, where they overlapped, where they disagree."""
from __future__ import annotations

from test_change_sets import CS, _relabel, _setup
from test_curation import P, _join, _test_buckets, team  # noqa: F401


def _ids(team):
    return {n: team[n].get("/api/auth/me").json()["user"]["id"] for n in ("ana", "ben")}


def test_compare_two_people(team):
    pid, items = _setup(team)
    ids = _ids(team)
    _relabel(team["ana"], pid, items[0], "POCS")
    _relabel(team["ben"], pid, items[0], "SAND")      # disagreement on items[0]
    _relabel(team["ben"], pid, items[1], "POCS")      # overlap, agreement (ben's edit is the real change;
    _relabel(team["ana"], pid, items[1], "POCS")      # ana's is a no-op, which kinds_of calls "other")
    _relabel(team["ben"], pid, items[2], "TURFH")     # ben only
    team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "Ana's pass"})
    body = team["vic"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b={ids['ben']}").json()
    ana, ben = body["people"]
    assert (ana["name"], ana["changes"], [c["message"] for c in ana["change_sets"]]) == ("Ana", 2, ["Ana's pass"])
    assert (ben["changes"], ben["change_sets"], ben["kinds"]) == (3, [], {"relabel": 3})
    assert body["overlap"] == 2
    assert body["disagreements"]["count"] == 1
    row = body["disagreements"]["items"][0]
    assert (row["item_id"], row["field"], row["a_value"], row["b_value"], row["current"]) == (
        items[0]["id"], "current_label", "POCS", "SAND", "SAND")


def test_someone_with_no_changes_gives_an_empty_side(team):
    pid, items = _setup(team)
    ids = _ids(team)
    _relabel(team["ana"], pid, items[0])
    body = team["ana"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b={ids['ben']}").json()
    assert body["people"][1]["changes"] == 0 and body["overlap"] == 0 and body["disagreements"]["count"] == 0


def test_since_a_version_and_bad_requests(team):
    import time

    pid, items = _setup(team)
    ids = _ids(team)
    _relabel(team["ana"], pid, items[0])
    team["admin"].post(f"{P}/projects/{pid}/versions", json={})
    deadline = time.time() + 30
    while team["admin"].get(f"{P}/projects/{pid}/versions/1").json()["status"] == "building" and time.time() < deadline:
        time.sleep(0.1)
    _relabel(team["ana"], pid, items[1])
    since = team["ana"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b={ids['ben']}&since=1").json()
    assert since["people"][0]["changes"] == 1
    assert team["ana"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b={ids['ana']}").status_code == 422
    assert team["ana"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b=999999").status_code == 404
    assert team["ana"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b={ids['ben']}&since=9").status_code == 404


def test_more_than_a_thousand_change_sets_stay_under_oracle_s_in_limit(team):
    from sqlalchemy import insert

    from appdb import session_scope
    from curation import change_sets
    from curation.models import CurationChangeSet, CurationEvent

    pid, _ = _setup(team)
    ids = _ids(team)
    with session_scope() as db:
        sets = [CurationChangeSet(project_id=pid, author_user_id=ids["ana"], author_name="Ana",
                                  message=f"pass {n}", change_count=1) for n in range(1100)]
        db.add_all(sets)
        db.flush()
        db.execute(insert(CurationEvent), [{
            "project_id": pid, "event_type": "item_updated", "user_id": ids["ana"], "curator_name": "Ana",
            "change_set_id": change_set.id, "previous_value": {"current_label": "ACBR"},
            "new_value": {"current_label": "POCS"}} for change_set in sets])
        assert len(change_sets.in_window(db, pid, 0, None)["change_sets"]) == 1100
    body = team["vic"].get(f"{P}/projects/{pid}/people/compare?a={ids['ana']}&b={ids['ben']}")
    assert body.status_code == 200, body.text
    assert len(body.json()["people"][0]["change_sets"]) == 1100
