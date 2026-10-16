"""Per-project roles: members curate, leads publish and manage members; admins do anything."""
from __future__ import annotations

from test_curation import P, _add, _item_payload, _test_buckets, team  # noqa: F401


def _project_as(client, name):
    response = client.post(f"{P}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _user_id(team, name):
    me = team[name].get("/api/auth/me").json()["user"]
    return me["id"]


def test_creator_leads_and_non_members_are_read_only(team):
    project = _project_as(team["ana"], "Ana's reef")  # ana is an app-wide curator: may create
    assert project["my_role"] == "lead"
    pid = project["id"]
    item = _add(team["ana"], pid, _item_payload("m-1", "ACBR"))
    # Ben (curator app-wide) is not a member: can read, cannot edit
    assert team["ben"].get(f"{P}/projects/{pid}/items").status_code == 200
    edit = team["ben"].patch(f"{P}/projects/{pid}/items/{item['id']}", json={"expected_revision": 1, "included": False})
    assert edit.status_code == 403
    assert "Ana" in edit.json()["detail"]  # names a lead to ask
    listed = {p["id"]: p["my_role"] for p in team["ben"].get(f"{P}/projects").json()}
    assert listed[pid] is None


def test_lead_adds_members_and_project_roles_override_app_roles(team):
    pid = _project_as(team["ana"], "Shared")["id"]
    item = _add(team["ana"], pid, _item_payload("m-2", "ACBR"))
    # vic is an app-wide viewer; as a project curator she may curate this project
    assert team["ana"].post(f"{P}/projects/{pid}/members", json={"username": "vic", "role": "curator"}).status_code == 201
    assert team["vic"].patch(f"{P}/projects/{pid}/items/{item['id']}",
                             json={"expected_revision": 1, "included": False}).status_code == 200
    # but not publish: that needs the project lead role
    assert team["vic"].post(f"{P}/projects/{pid}/versions", json={}).status_code == 403
    assert team["ana"].post(f"{P}/projects/{pid}/members", json={"username": "vic", "role": "curator"}).status_code == 409
    assert team["ana"].post(f"{P}/projects/{pid}/members", json={"username": "nobody", "role": "curator"}).status_code == 404
    members = {m["username"]: m["role"] for m in team["ben"].get(f"{P}/projects/{pid}/members").json()}
    assert members == {"ana": "lead", "vic": "curator"}


def test_only_leads_manage_members_and_the_last_lead_stays(team):
    pid = _project_as(team["ana"], "Guarded")["id"]
    team["ana"].post(f"{P}/projects/{pid}/members", json={"username": "ben", "role": "curator"})
    ana, ben = _user_id(team, "ana"), _user_id(team, "ben")
    assert team["ben"].post(f"{P}/projects/{pid}/members", json={"username": "vic", "role": "curator"}).status_code == 403
    demote = team["ana"].patch(f"{P}/projects/{pid}/members/{ana}", json={"role": "curator"})
    assert demote.status_code == 409 and "lead" in demote.json()["detail"]
    assert team["ana"].delete(f"{P}/projects/{pid}/members/{ana}").status_code == 409
    assert team["ana"].patch(f"{P}/projects/{pid}/members/{ben}", json={"role": "lead"}).status_code == 200
    assert team["ana"].patch(f"{P}/projects/{pid}/members/{ana}", json={"role": "curator"}).status_code == 200
    assert team["ben"].delete(f"{P}/projects/{pid}/members/{ana}").status_code == 200


def test_admins_act_as_lead_everywhere_and_app_leads_do_not(team):
    pid = _project_as(team["ana"], "Admin reach")["id"]
    assert {p["id"]: p["my_role"] for p in team["admin"].get(f"{P}/projects").json()}[pid] == "lead"
    assert team["admin"].post(f"{P}/projects/{pid}/members", json={"username": "ben", "role": "curator"}).status_code == 201
    item = _add(team["ana"], pid, _item_payload("lead-1", "ACBR"))
    # an app-wide lead who is not a member: no publishing, no editing
    created = team["admin"].post("/api/auth/admin/users", json={
        "username": "lou", "password": "pass-word-1", "role": "lead", "display_name": "Lou"})
    assert created.status_code == 200, created.text
    from fastapi.testclient import TestClient
    import main
    lou = TestClient(main.app)
    assert lou.post("/api/auth/login", json={"username": "lou", "password": "pass-word-1"}).status_code == 200
    assert lou.get("/api/auth/me").json()["user"]["role"] == "lead"
    assert lou.post(f"{P}/projects/{pid}/versions", json={}).status_code == 403
    edit = lou.patch(f"{P}/projects/{pid}/items/{item['id']}", json={"expected_revision": item["revision"], "included": False})
    assert edit.status_code == 403, edit.text
    assert team["ana"].patch(f"{P}/projects/{pid}/items/{item['id']}",   # the same revision still applies for a member
                             json={"expected_revision": item["revision"], "included": False}).status_code == 200


def test_user_search_for_the_add_member_form(team):
    found = team["ana"].get(f"{P}/users?query=be").json()
    assert {u["username"] for u in found} == {"ben"}
    assert team["vic"].get(f"{P}/users?query=").status_code == 200


def test_a_failure_adding_the_lead_leaves_no_project_behind(team, monkeypatch):
    from curation import members

    def boom(*args, **kwargs):
        raise RuntimeError("member insert failed")

    monkeypatch.setattr(members, "ensure_member", boom)
    quiet = type(team["ana"])(team["ana"].app, raise_server_exceptions=False)
    quiet.cookies.update(team["ana"].cookies)
    assert quiet.post(f"{P}/projects", json={"name": "Orphan"}).status_code >= 500
    assert "Orphan" not in {p["name"] for p in team["admin"].get(f"{P}/projects").json()}


def test_the_first_start_seed_makes_its_owner_the_project_lead(team):
    from appdb import session_scope
    from curation import importer, members

    with session_scope() as db:
        pid = importer.seed_noaa_pacific(db)
    assert pid is not None
    with session_scope() as db:
        listed = members.list_members(db, pid)
    assert [(m["username"], m["role"], m["added_by_name"]) for m in listed] == [("lead-admin", "lead", "first start")]
