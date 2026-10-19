"""The library and curation point at each other: a library image that is a curation patch, and back."""
from __future__ import annotations

import time

from test_curation import P, _add, _item_payload, _project, _test_buckets, client  # noqa: F401


def _library_asset(uri: str) -> int:
    """An indexed library image at ``uri`` (in a collection rooted above its folder), without reading storage."""
    from library import db

    rest, name = uri.rsplit("/", 1)
    root, folder = rest.rsplit("/", 1)
    with db.write() as conn:
        source = conn.execute("INSERT INTO sources(kind, root, label, tag_pattern, model_ref, added_at) "
                              "VALUES ('gcs', ?, 'T3 all', '', '', ?)", (root + "/", time.time())).lastrowid
        return conn.execute("INSERT INTO assets(source_id, uri, folder, name, status) VALUES (?, ?, ?, ?, 'ok')",
                            (source, uri, folder, name)).lastrowid


def test_a_library_image_links_to_its_curation_patch_and_back(client):
    from curation import settings

    project = _project(client, "Pacific T3")
    item = _add(client, project["id"], _item_payload("ACBR_0001", "ACBR"))
    other = _add(client, project["id"], _item_payload("ACBR_0002", "ACBR"))
    uri = f"{settings.t3_all_root()}ACBR/ACBR_0001.jpg"
    asset_id = _library_asset(uri)

    links = client.get(f"{P}/library-links", params={"uri": uri}).json()["links"]
    assert [(link["project_id"], link["item_id"]) for link in links] == [(project["id"], item["id"])]
    assert links[0]["project_name"] == "Pacific T3"

    # Same file name under another label folder is a different patch.
    assert client.get(f"{P}/library-links", params={"uri": f"{settings.t3_all_root()}POCS/ACBR_0001.jpg"}).json() == {"links": []}

    assets = client.get(f"{P}/projects/{project['id']}/items/{item['id']}/library").json()["assets"]
    assert [(a["asset_id"], a["uri"], a["source"]) for a in assets] == [(asset_id, uri, "T3 all")]
    assert client.get(f"{P}/projects/{project['id']}/items/{other['id']}/library").json() == {"assets": []}


def test_a_crop_uri_matches_outside_t3_too(client):
    project = _project(client, "Regional", mode="T1")
    item = _add(client, project["id"], _item_payload("POCS_0009", "POCS"))
    uri = "gs://nmfs/crops/POCS_0009.jpg"
    # The crop folder is not the label, so only the crop URI itself can match.
    assert client.get(f"{P}/library-links", params={"uri": uri}).json() == {"links": []}
    asset_id = _library_asset(uri)
    assets = client.get(f"{P}/projects/{project['id']}/items/{item['id']}/library").json()["assets"]
    assert [a["asset_id"] for a in assets] == [asset_id]
