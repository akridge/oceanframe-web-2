"""Combined releases from published versions: duplicates kept once, conflicts dropped, leakage listed."""
from __future__ import annotations

import csv
import io
import time

from test_curation import P, _add, _item_payload, _join, _test_buckets, team  # noqa: F401

R = f"{P}/releases"


def _crop(key, label, split, file_name, image=None):
    payload = {**_item_payload(key, label), "split": split, "file_name": file_name}
    if image:
        payload["source_image_uri"] = f"gs://nmfs/images/{image}.jpg"
    return payload


def _publish(client, pid):
    assert client.post(f"{P}/projects/{pid}/versions", json={}).status_code == 202
    deadline = time.time() + 30
    while time.time() < deadline:
        versions = client.get(f"{P}/projects/{pid}/versions").json()["versions"]
        if versions and versions[0]["status"] != "building":
            assert versions[0]["status"] == "published", versions[0]
            return versions[0]["version"]
        time.sleep(0.1)
    raise AssertionError("version did not finish")


def _wait(client, release_id):
    deadline = time.time() + 30
    while time.time() < deadline:
        body = client.get(f"{R}/{release_id}").json()
        if body["status"] != "building":
            return body
        time.sleep(0.1)
    raise AssertionError("release did not finish")


def _three_projects(team):
    admin = team["admin"]
    pids = [admin.post(f"{P}/projects", json={"name": n}).json()["id"] for n in ("Hawaii", "Mariana", "Rare")]
    hawaii, mariana, rare = pids
    _add(admin, hawaii, _crop("h1", "ACBR", "train", "a.png", "img-a"))
    _add(admin, hawaii, _crop("h2", "POCS", "train", "b.png", "img-b"))
    _add(admin, hawaii, _crop("h3", "SAND", "val", "c.png", "img-c"))
    _add(admin, mariana, _crop("m1", "ACBR", "train", "a.png", "img-a"))   # same crop, same label → duplicate
    _add(admin, mariana, _crop("m2", "TURFH", "test", "d.png", "img-b"))   # img-b now in train and test → leakage
    m3 = _add(admin, mariana, _crop("m3", "POCS", "train", "b.png", "img-b"))
    _add(admin, rare, _crop("r1", "POCS", "train", "b.png", "img-b"))       # b.png: two agree…
    r2 = _add(admin, rare, _crop("r2", "SAND", "val", "c.png", "img-c"))
    admin.patch(f"{P}/projects/{rare}/items/{r2['id']}", json={"expected_revision": r2["revision"], "current_label": "CCAH"})
    admin.patch(f"{P}/projects/{mariana}/items/{m3['id']}", json={"expected_revision": m3["revision"], "current_label": "ACBR"})
    return pids, [_publish(admin, pid) for pid in pids]


def test_a_combined_release_keeps_duplicates_once_and_drops_conflicts(team):
    (hawaii, mariana, rare), versions = _three_projects(team)
    response = team["admin"].post(R, json={"name": "Pacific T3", "dataset_mode": "T3", "notes": "first", "sources": [
        {"project_id": hawaii, "version": versions[0]}, {"project_id": mariana, "version": versions[1]},
        {"project_id": rare, "version": versions[2]}]})
    assert response.status_code == 202, response.text
    body = _wait(team["admin"], response.json()["id"])
    assert body["status"] == "published", body
    stats = body["statistics"]
    # a.png: duplicate (kept once). b.png: POCS/ACBR/POCS → conflict, all three dropped.
    # c.png: SAND vs CCAH → conflict, both dropped. d.png kept.
    assert (stats["duplicates"], stats["conflicts"]) == (1, 2)
    assert body["item_count"] == 2
    manifest = list(csv.DictReader(io.StringIO(team["admin"].get(f"{R}/{body['id']}/files/manifest.csv").text)))
    assert sorted(row["file_name"] for row in manifest) == ["a.png", "d.png"]
    assert {row["project"] for row in manifest} == {"Hawaii", "Mariana"}
    conflicts = list(csv.DictReader(io.StringIO(team["admin"].get(f"{R}/{body['id']}/files/conflicts.csv").text)))
    assert sorted((row["file_name"], row["label"]) for row in conflicts) == [
        ("b.png", "ACBR"), ("b.png", "POCS"), ("b.png", "POCS"), ("c.png", "CCAH"), ("c.png", "SAND")]
    changelog = team["admin"].get(f"{R}/{body['id']}/files/CHANGELOG.md").text
    assert "Pacific T3 — r1" in changelog and "Hawaii v1" in changelog


def test_leakage_is_listed_not_fixed(team):
    admin = team["admin"]
    a = admin.post(f"{P}/projects", json={"name": "Leak A"}).json()["id"]
    b = admin.post(f"{P}/projects", json={"name": "Leak B"}).json()["id"]
    _add(admin, a, _crop("l1", "ACBR", "train", "l1.png", "shared-image"))
    _add(admin, b, _crop("l2", "POCS", "test", "l2.png", "shared-image"))
    va, vb = _publish(admin, a), _publish(admin, b)
    rid = admin.post(R, json={"name": "Leaky", "dataset_mode": "T3", "sources": [
        {"project_id": a, "version": va}, {"project_id": b, "version": vb}]}).json()["id"]
    body = _wait(admin, rid)
    assert body["statistics"]["leakage"] == 1 and body["item_count"] == 2
    leakage = admin.get(f"{R}/{rid}/files/leakage.csv").text
    assert "shared-image" in leakage


def test_numbering_permissions_and_validation(team):
    admin = team["admin"]
    pid = admin.post(f"{P}/projects", json={"name": "Solo"}).json()["id"]
    _add(admin, pid, _crop("s1", "ACBR", "train", "s1.png"))
    version = _publish(admin, pid)
    source = [{"project_id": pid, "version": version}]
    first = _wait(admin, admin.post(R, json={"name": "Solo set", "dataset_mode": "T3", "sources": source}).json()["id"])
    second = _wait(admin, admin.post(R, json={"name": "Solo set", "dataset_mode": "T3", "sources": source}).json()["id"])
    assert (first["number"], second["number"]) == (1, 2)
    assert "unchanged" in admin.get(f"{R}/{second['id']}/files/CHANGELOG.md").text.lower()
    # ana: app-wide curator → 403; a lead who isn't a project lead → 403
    assert team["ana"].post(R, json={"name": "x", "dataset_mode": "T3", "sources": source}).status_code == 403
    assert admin.post(R, json={"name": "x", "dataset_mode": "T1", "sources": source}).status_code == 422  # mode
    assert admin.post(R, json={"name": "x", "dataset_mode": "T3", "sources": []}).status_code == 422
    assert admin.post(R, json={"name": "x", "dataset_mode": "T3", "sources": source + source}).status_code == 422
    assert admin.post(R, json={"name": "x", "dataset_mode": "T3",
                               "sources": [{"project_id": pid, "version": 9}]}).status_code == 422
    assert team["vic"].get(R).status_code == 200


def test_a_build_interrupted_by_a_restart_is_marked_failed(team):
    from appdb import session_scope
    from curation import releases
    from curation.models import CurationRelease

    with session_scope() as db:
        db.add(CurationRelease(name="Stuck", number=1, dataset_mode="T3", status="building", sources=[]))
    with session_scope() as db:
        assert releases.mark_interrupted(db) == 1
    with session_scope() as db:
        stuck = db.query(CurationRelease).filter_by(name="Stuck").one()
        assert stuck.status == "failed" and "build it again" in stuck.error_message


def _solo(admin, name, key="s1"):
    pid = admin.post(f"{P}/projects", json={"name": name}).json()["id"]
    _add(admin, pid, _crop(key, "ACBR", "train", f"{key}.png"))
    return pid, _publish(admin, pid)


def test_names_that_slug_the_same_keep_their_own_files(team):
    admin = team["admin"]
    pid, version = _solo(admin, "Folder source")
    source = [{"project_id": pid, "version": version}]
    one = _wait(admin, admin.post(R, json={"name": "Pacific T3", "dataset_mode": "T3", "sources": source}).json()["id"])
    two = _wait(admin, admin.post(R, json={"name": "Pacific_T3", "dataset_mode": "T3", "sources": source}).json()["id"])
    dotted = _wait(admin, admin.post(R, json={"name": "..", "dataset_mode": "T3", "sources": source}).json()["id"])
    assert one["status"] == two["status"] == dotted["status"] == "published"
    changelogs = [admin.get(f"{R}/{b['id']}/files/CHANGELOG.md").text for b in (one, two, dotted)]
    assert "Pacific T3 — r1" in changelogs[0] and "Pacific_T3 — r1" in changelogs[1] and ".. — r1" in changelogs[2]


def test_agree_agree_disagree_is_one_conflict_and_no_duplicate(team):
    admin = team["admin"]
    pids, versions = [], []
    for index, label in enumerate(("ACBR", "ACBR", "POCS")):
        pid = admin.post(f"{P}/projects", json={"name": f"Vote {index}"}).json()["id"]
        item = _add(admin, pid, _crop(f"v{index}", "ACBR", "train", "same.png", "img-same"))
        if label != "ACBR":
            admin.patch(f"{P}/projects/{pid}/items/{item['id']}", json={"expected_revision": item["revision"], "current_label": label})
        pids.append(pid)
        versions.append(_publish(admin, pid))
    rid = admin.post(R, json={"name": "Votes", "dataset_mode": "T3", "sources": [
        {"project_id": p, "version": v} for p, v in zip(pids, versions)]}).json()["id"]
    body = _wait(admin, rid)
    assert (body["statistics"]["duplicates"], body["statistics"]["conflicts"], body["item_count"]) == (0, 1, 0)
    rows = list(csv.DictReader(io.StringIO(admin.get(f"{R}/{rid}/files/conflicts.csv").text)))
    assert sorted(r["label"] for r in rows) == ["ACBR", "ACBR", "POCS"]


def test_a_failed_build_is_recorded_and_cleans_up(team, monkeypatch):
    from curation import releases
    from curation.models import CurationRelease
    from appdb import session_scope

    admin = team["admin"]
    pid, version = _solo(admin, "Doomed source")

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(releases, "_write", boom)
    body = _wait(admin, admin.post(R, json={"name": "Doomed", "dataset_mode": "T3",
                                            "sources": [{"project_id": pid, "version": version}]}).json()["id"])
    assert body["status"] == "failed" and "disk on fire" in body["error_message"]
    with session_scope() as db:
        assert not releases.release_dir(db.get(CurationRelease, body["id"])).exists()
    assert admin.get(f"{R}/{body['id']}/files/manifest.csv").status_code == 404   # not published


def test_an_app_lead_who_is_not_a_project_lead_is_refused(team):
    admin = team["admin"]
    pid, version = _solo(admin, "Lead gate")
    response = admin.post("/api/auth/admin/users", json={
        "username": "lena", "password": "pass-word-1", "role": "lead", "display_name": "Lena"})
    assert response.status_code == 200, response.text
    from fastapi.testclient import TestClient
    import main
    lena = TestClient(main.app)
    lena.post("/api/auth/login", json={"username": "lena", "password": "pass-word-1"})
    refused = lena.post(R, json={"name": "Nope", "dataset_mode": "T3", "sources": [{"project_id": pid, "version": version}]})
    assert refused.status_code == 403, refused.text
    assert "project lead" in refused.json()["detail"]


def test_an_unpublished_version_is_refused_and_unknown_files_404(team):
    from appdb import session_scope
    from curation.models import CurationDatasetVersion

    admin = team["admin"]
    pid, version = _solo(admin, "Unpublished")
    with session_scope() as db:
        for number, status in ((90, "failed"), (91, "building")):
            db.add(CurationDatasetVersion(project_id=pid, version=number, status=status))
    for number in (90, 91):
        refused = admin.post(R, json={"name": "x", "dataset_mode": "T3", "sources": [{"project_id": pid, "version": number}]})
        assert refused.status_code == 422, refused.text
    done = _wait(admin, admin.post(R, json={"name": "Files", "dataset_mode": "T3",
                                            "sources": [{"project_id": pid, "version": version}]}).json()["id"])
    assert admin.get(f"{R}/{done['id']}/files/secrets.txt").status_code == 404
    assert admin.get(f"{R}/{done['id']}/files/leakage.csv").status_code == 200
    assert admin.get(f"{R}/999999").status_code == 404


def _reference_files(sources):
    """The pre-streaming in-memory build, kept here as the oracle for the streamed one."""
    from collections import defaultdict

    from sqlalchemy import select

    from appdb import session_scope
    from curation.models import CurationVersionInput as VI
    from curation.versions import SPLITS

    kept, groups, agreeing, conflicted, duplicates = {}, {}, defaultdict(list), set(), 0
    with session_scope() as db:
        for source in sources:
            rows = db.execute(select(
                VI.item_id, VI.label, VI.split, VI.source_label, VI.crop_uri, VI.source_image_uri, VI.row_px,
                VI.column_px, VI.crop_size, VI.region, VI.island, VI.site, VI.survey_year, VI.annotator,
                VI.source_key, VI.file_name).where(VI.version_id == source["version_id"]).order_by(VI.item_id)).all()
            for row in rows:
                record = (source["project_name"], source["version"], *row)
                key = (row.file_name, row.source_label)
                if key in conflicted:
                    groups[key].append(record)
                    continue
                if key in kept:
                    if kept[key][3] == row.label:
                        duplicates += 1
                        agreeing[key].append(record)
                    else:
                        conflicted.add(key)
                        earlier = agreeing.pop(key, [])
                        duplicates -= len(earlier)
                        groups[key] = [kept.pop(key), *earlier, record]
                    continue
                kept[key] = record
    image_splits, image_owners = defaultdict(set), defaultdict(set)
    manifest, conflicts, leakage = io.StringIO(newline=""), io.StringIO(newline=""), io.StringIO(newline="")
    writer = csv.writer(manifest)
    writer.writerow(releases_columns())
    for record in sorted(kept.values(), key=lambda r: (r[4], r[3], r[0], r[2])):
        writer.writerow(record)
        image_splits[record[7]].add(record[4])
        image_owners[record[7]].add(f"{record[0]}:{record[4]}")
    writer = csv.writer(conflicts)
    writer.writerow(["file_name", "source_label", "project", "project_version", "item_id", "label", "split"])
    for key in sorted(conflicted):
        for record in groups[key]:
            writer.writerow([key[0], key[1], record[0], record[1], record[2], record[3], record[4]])
    writer = csv.writer(leakage)
    writer.writerow(["source_image_uri", "project_splits"])
    leaks = sorted(image for image, splits in image_splits.items() if len(splits) > 1)
    for image in leaks:
        writer.writerow([image, " ".join(sorted(image_owners[image]))])
    assert set(SPLITS) >= {r[4] for r in kept.values()}
    return {"manifest.csv": manifest.getvalue(), "conflicts.csv": conflicts.getvalue(),
            "leakage.csv": leakage.getvalue()}, {"duplicates": duplicates, "conflicts": len(conflicted),
                                                 "leakage": len(leaks), "item_count": len(kept)}


def releases_columns():
    from curation.releases import COLUMNS
    return COLUMNS


def test_a_streamed_build_matches_the_in_memory_build_on_many_rows(team):
    """agree/agree/disagree, duplicates across and within sources with different splits, request order not
    name order, and more rows than one fetch batch: the files match the old in-memory build exactly."""
    from sqlalchemy import insert

    from appdb import session_scope
    from curation.models import CurationDatasetVersion, CurationVersionInput

    admin = team["admin"]
    names = ("Zulu bulk", "Alpha bulk", "Mike bulk")       # request order is not name order
    pids = [admin.post(f"{P}/projects", json={"name": n}).json()["id"] for n in names]
    labels, splits = ("ACBR", "POCS", "SAND", "TURFH"), ("train", "val", "test")

    def row(vid, item_id, file_name, label, split, image, source_label="ACBR"):
        return {"version_id": vid, "item_id": item_id, "label": label, "split": split, "source_key": f"k{vid}-{item_id}",
                "source_label": source_label, "crop_uri": f"gs://c/{vid}/{item_id}.png",
                "source_image_uri": f"gs://i/{image}.jpg", "file_name": file_name, "crop_size": 224,
                "row_px": item_id % 500, "column_px": None, "region": "Hawaii", "island": None, "site": "S1",
                "survey_year": 2024, "annotator": None, "metadata_json": {}}

    vids = []
    with session_scope() as db:
        for pid in pids:
            version = CurationDatasetVersion(project_id=pid, version=1, status="published", item_count=0)
            db.add(version)
            db.flush()
            vids.append(version.id)
    zulu, alpha, mike = vids
    rows = []
    for index, vid in enumerate(vids):
        for n in range(1, 2201):    # 6,600 filler rows in all, past the 5,000-row fetch batch
            rows.append(row(vid, n * 3 + index, f"fill-{vid}-{n}.png", labels[n % 4], splits[n % 3], f"img-{n % 700}"))
    rows += [
        row(zulu, 100001, "vote.png", "ACBR", "train", "vote"), row(alpha, 100001, "vote.png", "ACBR", "train", "vote"),
        row(mike, 100001, "vote.png", "POCS", "train", "vote"),                       # agree/agree/disagree
        row(zulu, 100002, "dup.png", "SAND", "val", "dup-z"), row(alpha, 100002, "dup.png", "SAND", "train", "dup-a"),
        row(mike, 1, "first.png", "TURFH", "train", "first-m"), row(zulu, 100003, "first.png", "TURFH", "test", "first-z"),
        row(alpha, 2, "inner.png", "POCS", "test", "inner"), row(alpha, 100004, "inner.png", "POCS", "train", "inner"),
        row(zulu, 100005, "late.png", "ACBR", "val", "late"), row(mike, 100005, "late.png", "ACBR", "val", "late"),
        row(alpha, 100006, "late.png", "SAND", "val", "late"),                          # conflict after a duplicate
    ]
    with session_scope() as db:
        db.execute(insert(CurationVersionInput), rows)
        for vid in vids:
            db.get(CurationDatasetVersion, vid).item_count = sum(1 for r in rows if r["version_id"] == vid)

    rid = admin.post(R, json={"name": "Bulk", "dataset_mode": "T3", "sources": [
        {"project_id": pid, "version": 1} for pid in pids]}).json()["id"]
    body = _wait(admin, rid)
    assert body["status"] == "published", body
    expected, stats = _reference_files(body["sources"])
    for name, text in expected.items():
        served = admin.get(f"{R}/{rid}/files/{name}")
        assert served.status_code == 200
        assert served.content == text.encode("utf-8"), name
    got = body["statistics"]
    assert (got["duplicates"], got["conflicts"], got["leakage"], body["item_count"]) == (
        stats["duplicates"], stats["conflicts"], stats["leakage"], stats["item_count"])
    assert (got["duplicates"], got["conflicts"]) == (3, 2)
    manifest = {r["file_name"]: r for r in csv.DictReader(io.StringIO(admin.get(f"{R}/{rid}/files/manifest.csv").text))}
    # the first source in request order wins, whatever its split, its item_id or its project's name
    assert (manifest["dup.png"]["project"], manifest["dup.png"]["split"]) == ("Zulu bulk", "val")
    assert (manifest["first.png"]["project"], manifest["first.png"]["split"]) == ("Zulu bulk", "test")
    assert manifest["inner.png"]["split"] == "test"
    assert "vote.png" not in manifest and "late.png" not in manifest
    assert got["per_source"] == {"Zulu bulk v1": 2202, "Alpha bulk v1": 2201, "Mike bulk v1": 2200}


def test_a_version_without_frozen_inputs_is_refused(team):
    from appdb import session_scope
    from curation.models import CurationVersionInput

    admin = team["admin"]
    pid, version = _solo(admin, "Legacy release")
    with session_scope() as db:
        db.query(CurationVersionInput).delete()
    refused = admin.post(R, json={"name": "Old", "dataset_mode": "T3", "sources": [{"project_id": pid, "version": version}]})
    assert refused.status_code == 422, refused.text
    assert "publish a new version first" in refused.json()["detail"]


def test_an_empty_published_version_is_not_treated_as_legacy(team):
    from appdb import session_scope
    from curation.models import CurationDatasetVersion

    admin = team["admin"]
    pid = admin.post(f"{P}/projects", json={"name": "Empty release"}).json()["id"]
    with session_scope() as db:
        db.add(CurationDatasetVersion(project_id=pid, version=1, status="published", item_count=0))
    response = admin.post(R, json={"name": "Empty", "dataset_mode": "T3", "sources": [{"project_id": pid, "version": 1}]})
    assert response.status_code == 202, response.text
