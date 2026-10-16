"""Change sets: edits pile up as uncommitted; Commit groups them under a message."""
from __future__ import annotations

from test_curation import P, _add, _item_payload, _join, _test_buckets, team  # noqa: F401

CS = "/change-sets"


def _setup(team):
    pid = team["admin"].post(f"{P}/projects", json={"name": "Commits"}).json()["id"]
    _join(team, pid, "ana")
    _join(team, pid, "ben")
    items = [_add(team["admin"], pid, _item_payload(f"cs-{n}", "ACBR")) for n in range(3)]
    return pid, items


def _relabel(client, pid, item, label="POCS"):
    current = next(i for i in client.get(f"{P}/projects/{pid}/items", params={"limit": 100}).json()["items"] if i["id"] == item["id"])
    response = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                            json={"expected_revision": current["revision"], "current_label": label})
    assert response.status_code == 200, response.text


def test_edits_are_uncommitted_until_committed(team):
    pid, items = _setup(team)
    _relabel(team["ana"], pid, items[0])
    _relabel(team["ana"], pid, items[1])
    _relabel(team["ben"], pid, items[2])
    mine = team["ana"].get(f"{P}/projects/{pid}{CS}/pending?user=me").json()
    assert mine["count"] == 2 and mine["summary"]["kinds"] == {"relabel": 2}
    assert mine["summary"]["classes"] == [{"label": "POCS", "items": 2}] and len(mine["recent"]) == 2

    committed = team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "  Relabelled POCS on Hawaii  "})
    assert committed.status_code == 201, committed.text
    body = committed.json()
    assert (body["message"], body["change_count"], body["author_name"]) == ("Relabelled POCS on Hawaii", 2, "Ana")
    assert team["ana"].get(f"{P}/projects/{pid}{CS}/pending?user=me").json()["count"] == 0
    assert team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "again"}).status_code == 409
    # ben's edit is untouched and shows as in progress; admin's item creations are admin's own
    ben_id = team["ben"].get("/api/auth/me").json()["user"]["id"]
    open_work = {row["user_id"]: row for row in team["admin"].get(f"{P}/projects/{pid}{CS}/uncommitted").json()}
    assert open_work[ben_id] == {"user_id": ben_id, "name": "Ben", "count": 1}


def test_imports_and_admin_item_creation_are_not_ana_s(team):
    pid, _ = _setup(team)  # admin created the items: those are admin's uncommitted changes, not ana's
    assert team["ana"].get(f"{P}/projects/{pid}{CS}/pending?user=me").json()["count"] == 0
    assert team["admin"].get(f"{P}/projects/{pid}{CS}/pending?user=me").json()["summary"]["kinds"] == {"added": 3}


def test_history_detail_and_show_these_items(team):
    pid, items = _setup(team)
    _relabel(team["ana"], pid, items[0])
    first = team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "one"}).json()
    _relabel(team["ben"], pid, items[1])
    team["ben"].post(f"{P}/projects/{pid}{CS}", json={"message": "two"})
    history = team["vic"].get(f"{P}/projects/{pid}{CS}").json()
    assert [cs["message"] for cs in history] == ["two", "one"]
    only_ana = team["vic"].get(f"{P}/projects/{pid}{CS}?user_id={first['author_user_id']}").json()
    assert [cs["message"] for cs in only_ana] == ["one"]
    detail = team["vic"].get(f"{P}/projects/{pid}{CS}/{first['id']}").json()
    assert detail["item_ids"] == [items[0]["id"]]
    assert detail["events"][0]["previous_value"]["current_label"] == "ACBR"
    assert detail["events"][0]["new_value"]["current_label"] == "POCS"


def test_message_rules_and_membership(team):
    pid, items = _setup(team)
    _relabel(team["ana"], pid, items[0])
    assert team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "   "}).status_code == 422
    assert team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "x" * 501}).status_code == 422
    assert team["vic"].post(f"{P}/projects/{pid}{CS}", json={"message": "not mine"}).status_code == 403


def test_a_lead_commits_for_a_member_who_left_and_curators_cannot(team):
    pid, items = _setup(team)
    _relabel(team["ana"], pid, items[0])
    ana_id = team["ana"].get("/api/auth/me").json()["user"]["id"]
    assert team["ben"].post(f"{P}/projects/{pid}{CS}", json={"message": "for ana", "user_id": ana_id}).status_code == 403
    assert team["admin"].delete(f"{P}/projects/{pid}/members/{ana_id}").status_code == 200  # ana leaves
    assert {r["user_id"]: r["name"] for r in team["admin"].get(f"{P}/projects/{pid}{CS}/uncommitted").json()}[ana_id] == "Ana"
    done = team["admin"].post(f"{P}/projects/{pid}{CS}", json={"message": "Ana's last edits", "user_id": ana_id})
    assert done.status_code == 201
    assert (done.json()["author_name"], done.json()["committed_by_name"]) == ("Ana", "lead-admin")


def test_an_edit_landing_during_a_commit_stays_uncommitted(team, monkeypatch):
    from curation import change_sets

    pid, items = _setup(team)
    _relabel(team["ana"], pid, items[0])
    real = change_sets.summarize
    fired = []

    def summarize_then_edit(db, where):
        result = real(db, where)  # the summary has been read: now another edit lands
        if fired:
            return result
        fired.append(1)
        _relabel(team["ana"], pid, items[1], "SAND")
        return result

    monkeypatch.setattr(change_sets, "summarize", summarize_then_edit)
    body = team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "first"}).json()
    assert body["change_count"] == 1
    monkeypatch.setattr(change_sets, "summarize", real)
    assert team["ana"].get(f"{P}/projects/{pid}{CS}/pending?user=me").json()["count"] == 1


def test_only_committable_events_count(team):
    from appdb import session_scope
    from curation.models import CurationEvent

    pid, items = _setup(team)
    ana_id = team["ana"].get("/api/auth/me").json()["user"]["id"]
    _relabel(team["ana"], pid, items[0])
    with session_scope() as db:
        for kind, item_id in (("source_import_completed", None), ("hub_link", items[1]["id"]), ("split_adopted", items[2]["id"])):
            db.add(CurationEvent(project_id=pid, item_id=item_id, event_type=kind, user_id=ana_id,
                                 curator_name="Ana", new_value={}))
    assert team["ana"].get(f"{P}/projects/{pid}{CS}/pending?user=me").json()["count"] == 1
    open_work = {r["user_id"]: r for r in team["admin"].get(f"{P}/projects/{pid}{CS}/uncommitted").json()}
    assert open_work[ana_id]["count"] == 1
    body = team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "only the edit"}).json()
    assert body["change_count"] == 1 and body["summary"]["kinds"] == {"relabel": 1}
    with session_scope() as db:
        left = [e.change_set_id for e in db.query(CurationEvent).filter(
            CurationEvent.project_id == pid, CurationEvent.event_type.in_(("source_import_completed", "hub_link", "split_adopted")))]
    assert left == [None, None, None]


def test_versions_list_their_change_sets_and_uncommitted_work(team):
    import time

    pid, items = _setup(team)
    team["admin"].post(f"{P}/projects/{pid}{CS}", json={"message": "Imported the first three"})
    _relabel(team["ana"], pid, items[0])
    team["ana"].post(f"{P}/projects/{pid}{CS}", json={"message": "Ana fixed a POCS"})
    _relabel(team["ben"], pid, items[1])  # left uncommitted

    preview = team["admin"].get(f"{P}/projects/{pid}/versions/preview").json()
    assert [cs["message"] for cs in preview["change_sets"]] == ["Imported the first three", "Ana fixed a POCS"]
    assert preview["uncommitted"] == [{"name": "Ben", "count": 1}]

    assert team["admin"].post(f"{P}/projects/{pid}/versions", json={"notes": "v1"}).status_code == 202
    deadline = time.time() + 30
    while team["admin"].get(f"{P}/projects/{pid}/versions/1").json()["status"] == "building" and time.time() < deadline:
        time.sleep(0.1)
    version = team["admin"].get(f"{P}/projects/{pid}/versions/1").json()
    assert [cs["message"] for cs in version["changes"]["change_sets"]] == ["Imported the first three", "Ana fixed a POCS"]
    changelog = team["admin"].get(f"{P}/projects/{pid}/versions/1/files/CHANGELOG.md").text
    assert "## Change sets" in changelog and "Ana fixed a POCS" in changelog and "Uncommitted: Ben 1" in changelog
    manifest = team["admin"].get(f"{P}/projects/{pid}/versions/1/files/manifest.json").json()
    assert len(manifest["change_sets"]) == 2 and manifest["uncommitted"] == [{"name": "Ben", "count": 1}]
