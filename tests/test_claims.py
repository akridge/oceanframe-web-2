"""Claims and the change feed: two curators on one project do not review the same items."""
from __future__ import annotations

from test_curation import P, _add, _item_payload, _join, _project, _test_buckets, team  # noqa: F401


def _shared(team, name="Claims"):
    pid = _project(team["admin"], name)["id"]
    for person in ("ana", "ben"):
        _join(team, pid, person)
    image_a = [_add(team["admin"], pid, {**_item_payload(f"a{n}", "ACBR"), "source_image_uri": "gs://nmfs/images/a.jpg"})
               for n in range(2)]
    image_b = [_add(team["admin"], pid, {**_item_payload(f"b{n}", "POCS"), "source_image_uri": "gs://nmfs/images/b.jpg"})
               for n in range(2)]
    return pid, image_a, image_b


def _claim(client, pid, holder, ids):
    response = client.post(f"{P}/projects/{pid}/claims", json={"holder": holder, "item_ids": [i["id"] for i in ids]})
    assert response.status_code == 200, response.text
    return response.json()


def _queue(client, pid, **params):
    response = client.get(f"{P}/projects/{pid}/source-images", params=params)
    assert response.status_code == 200, response.text
    return [image["source_image_uri"].rsplit("/", 1)[1] for image in response.json()["images"]]


def test_an_open_image_drops_out_of_the_other_curators_queue(team):
    pid, image_a, _ = _shared(team)
    assert _claim(team["ana"], pid, "ana-tab-1", image_a) == {"held": [], "claimed": 2}
    assert _queue(team["ben"], pid, hide_claimed="true") == ["b.jpg"]
    assert _queue(team["ana"], pid, hide_claimed="true") == ["a.jpg", "b.jpg"]  # her own claims never hide
    assert _queue(team["ben"], pid) == ["a.jpg", "b.jpg"]  # only when asked
    items = team["ben"].get(f"{P}/projects/{pid}/items", params={"hide_claimed": "true"}).json()["items"]
    assert {item["source_image_uri"] for item in items} == {"gs://nmfs/images/b.jpg"}

    # Ben opening the same image is told who has it, and takes nothing from her.
    held = _claim(team["ben"], pid, "ben-tab-1", image_a)
    assert held["claimed"] == 0 and {h["user_name"] for h in held["held"]} == {"Ana"}
    # A second tab of hers does not report her own claims as held.
    assert _claim(team["ana"], pid, "ana-tab-2", image_a)["held"] == []

    # Moving on lets go: the tab's claims are exactly what it names.
    _claim(team["ana"], pid, "ana-tab-1", [])
    _claim(team["ana"], pid, "ana-tab-2", [])
    assert _queue(team["ben"], pid, hide_claimed="true") == ["a.jpg", "b.jpg"]


def test_a_claim_runs_out(team, monkeypatch):
    from datetime import timedelta

    from curation import claims

    pid, image_a, _ = _shared(team, "Expiry")
    _claim(team["ana"], pid, "ana-tab-1", image_a)
    monkeypatch.setattr(claims, "TTL", timedelta(seconds=-1))  # renewals now land already expired
    _claim(team["ana"], pid, "ana-tab-1", image_a)
    assert _queue(team["ben"], pid, hide_claimed="true") == ["a.jpg", "b.jpg"]
    assert _claim(team["ben"], pid, "ben-tab-1", image_a)["claimed"] == 2


def test_viewers_cannot_claim(team):
    pid, image_a, _ = _shared(team, "Viewer claims")
    response = team["vic"].post(f"{P}/projects/{pid}/claims", json={"holder": "vic-tab-1", "item_ids": [image_a[0]["id"]]})
    assert response.status_code == 403


def test_finished_images_leave_the_queue_with_their_counts_intact(team):
    pid, image_a, _ = _shared(team, "Handled")
    for item in image_a[:1]:
        team["ana"].patch(f"{P}/projects/{pid}/items/{item['id']}",
                          json={"expected_revision": item["revision"], "review_state": "reviewed"})
    page = team["ben"].get(f"{P}/projects/{pid}/source-images", params={"handled": "hide"}).json()["images"]
    first = next(image for image in page if image["source_image_uri"].endswith("a.jpg"))
    assert (first["annotation_count"], first["reviewed_count"]) == (2, 1)  # still to do: one point
    team["ana"].patch(f"{P}/projects/{pid}/items/{image_a[1]['id']}",
                      json={"expected_revision": image_a[1]["revision"], "review_state": "flagged"})
    assert _queue(team["ben"], pid, handled="hide") == ["b.jpg"]


def test_the_change_feed_reports_other_peoples_work_since_a_watermark(team):
    pid, image_a, image_b = _shared(team, "Feed")
    start = team["ben"].get(f"{P}/projects/{pid}/changes").json()
    assert start["items"] == [] and start["last_id"] > 0
    team["ana"].patch(f"{P}/projects/{pid}/items/{image_a[0]['id']}",
                      json={"expected_revision": image_a[0]["revision"], "review_state": "reviewed"})
    team["ben"].patch(f"{P}/projects/{pid}/items/{image_b[0]['id']}",
                      json={"expected_revision": image_b[0]["revision"], "review_state": "reviewed"})
    _claim(team["ana"], pid, "ana-tab-1", image_b)
    feed = team["ben"].get(f"{P}/projects/{pid}/changes", params={"after_id": start["last_id"]}).json()
    assert [(item["id"], item["review_state"], item["updated_by_name"]) for item in feed["items"]] == [
        (image_a[0]["id"], "reviewed", "Ana")]  # his own save is not news to him
    assert feed["images"] == [{"source_image_uri": "gs://nmfs/images/a.jpg", "pending": 1}]
    assert {c["item_id"] for c in feed["claimed"]} == {i["id"] for i in image_b}
    assert feed["last_id"] > start["last_id"] and not feed["reload"]
    again = team["ben"].get(f"{P}/projects/{pid}/changes", params={"after_id": feed["last_id"]}).json()
    assert again["items"] == []


def test_presence_shows_who_is_online_and_what_they_have_open(team, monkeypatch):
    from datetime import timedelta

    from curation import presence

    pid, image_a, _ = _shared(team, "Presence")
    url = f"{P}/projects/{pid}/presence"
    _claim(team["ana"], pid, "ana-tab-1", image_a)
    team["ana"].patch(f"{P}/projects/{pid}/items/{image_a[0]['id']}",
                      json={"expected_revision": image_a[0]["revision"], "review_state": "reviewed"})
    team["ana"].post(url, json={"holder": "ana-tab-1", "view": "source", "detail": "a.jpg"})
    online = team["vic"].post(url, json={"holder": "vic-tab-1", "view": "activity"}).json()["online"]
    by_name = {person["name"]: person for person in online}
    assert set(by_name) == {"Ana", "Vic"}  # viewers are online too
    ana = by_name["Ana"]
    assert (ana["open_items"], ana["recent_changes"]) == (2, 1)
    assert ana["tabs"] == [{**ana["tabs"][0], "project": "Presence", "view": "source", "detail": "a.jpg"}]
    # Another person cannot take over a tab's token.
    team["ben"].post(url, json={"holder": "ana-tab-1", "view": "matrix"})
    assert {p["name"] for p in team["vic"].post(url, json={"holder": "vic-tab-1", "view": "activity"}).json()["online"]} == {"Ana", "Vic"}
    team["ana"].post(url, json={"holder": "ana-tab-1", "leaving": True})
    monkeypatch.setattr(presence, "ONLINE", timedelta(seconds=-1))  # and a tab not heard from is gone
    assert team["vic"].post(url, json={"holder": "vic-tab-1", "view": "activity"}).json()["online"] == []
