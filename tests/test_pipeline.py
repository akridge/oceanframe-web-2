"""Indexing, search filters, tagging, and dataset splitting end to end."""
from __future__ import annotations


def _index(root, **kwargs):
    from library import indexer, jobs

    job = jobs.start_job("index", {}, lambda j: indexer.index_source(j, str(root), **kwargs))
    for _ in jobs.stream(job, heartbeat=30):
        pass
    assert job.status == "done", job.message
    return job.result


def test_index_populates_catalog(library, tree):
    from library import search

    result = _index(tree)
    assert result["indexed"] == 5
    assert result["failed"] == 0

    stats = search.stats()
    assert stats["ok"] == 5
    assert stats["embedded"] == 5
    assert stats["folders"] == 3


def test_reindex_skips_unchanged(library, tree):
    _index(tree)
    again = _index(tree)
    assert again["indexed"] == 0
    assert again["skipped"] == 5


def test_changed_file_is_reindexed(library, tree):
    from conftest import make_image

    _index(tree)
    make_image(blobs=2, blur=4.0).save(tree / "2024/kaneohe/sharp_b.jpg", quality=90)
    result = _index(tree)
    assert result["indexed"] == 1
    assert result["skipped"] == 4


def test_deleted_file_is_marked_missing(library, tree):
    from library import search

    _index(tree)
    (tree / "2023/kaneohe/dark.jpg").unlink()
    result = _index(tree)
    assert result["missing"] == 1
    assert search.stats()["missing"] == 1
    # The row survives so history and dataset membership are not silently lost.
    assert search.run({"status": "any"})["total"] == 5


def test_path_tags_are_derived(library, tree):
    from library import db, tags

    _index(tree)
    names = {t["name"] for t in tags.list_tags(db.connect())}
    assert {"year:2024", "year:2023", "site:kaneohe", "site:hanauma"} <= names


def test_folder_scope_filters(library, tree):
    from library import search

    _index(tree)
    assert search.run({"folder": "2024"})["total"] == 4
    assert search.run({"folder": "2024/hanauma"})["total"] == 1
    assert search.run({"folder": "2024/kaneohe", "folder_exact": True})["total"] == 3


def test_folder_children_counts(library, tree):
    from library import search

    _index(tree)
    top = search.folder_children({}, "")
    assert {c["name"]: c["count"] for c in top["children"]} == {"2023": 1, "2024": 4}
    inner = search.folder_children({}, "2024")
    assert {c["name"]: c["count"] for c in inner["children"]} == {"hanauma": 1, "kaneohe": 3}


def test_folder_children_respects_the_query(library, tree):
    from library import search

    _index(tree)
    scoped = search.folder_children({"tags": ["site:kaneohe"]}, "")
    assert {c["name"]: c["count"] for c in scoped["children"]} == {"2023": 1, "2024": 3}


def test_quality_filter(library, tree):
    from library import search

    _index(tree)
    everything = search.run({"page_size": 50})["items"]
    threshold = sorted(i["quality"] for i in everything)[2]
    filtered = search.run({"quality_min": threshold, "page_size": 50})
    assert filtered["total"] == sum(1 for i in everything if i["quality"] >= threshold)


def test_similarity_finds_the_near_duplicate(library, tree):
    from library import search

    _index(tree)
    items = {i["name"]: i["id"] for i in search.run({"page_size": 50})["items"]}
    ranked = search.run({"similar_to": items["sharp_a.jpg"], "page_size": 5})["items"]
    assert ranked[0]["name"] == "sharp_a_dup.jpg"
    assert ranked[0]["score"] > 0.9


def test_dedupe_collapses_the_pair(library, tree):
    from library import search

    _index(tree)
    plain = search.run({"page_size": 50, "sort": "quality"})
    deduped = search.run({"page_size": 50, "sort": "quality", "dedupe": True})
    assert deduped["matched"] < plain["total"]

    groups = search.duplicate_groups({})
    names = {g["keep"]["name"] for g in groups} | {
        d["name"] for g in groups for d in g["duplicates"]
    }
    assert {"sharp_a.jpg", "sharp_a_dup.jpg"} <= names


def test_keyword_search_covers_names_and_tags(library, tree):
    from library import db, search, tags

    _index(tree)
    # sharp_a, sharp_b and sharp_a_dup all carry the prefix.
    assert search.run({"text": "sharp", "mode": "keyword"})["total"] == 3

    ids = search.candidate_ids({"folder": "2024/hanauma"})
    with db.write() as conn:
        tags.add_tags(conn, ids, ["review:recheck"])
    assert search.run({"keywords": "recheck"})["total"] == 1


def test_text_search_without_clip_falls_back_and_says_so(library, tree):
    from library import search

    _index(tree)
    result = search.run({"text": "a school of fish"})
    assert "text search" in result["note"].lower()
    assert result["vector"] is True


def test_tag_filters(library, tree):
    from library import search

    _index(tree)
    assert search.run({"tags": ["site:kaneohe"]})["total"] == 4
    assert search.run({"tags": ["site:kaneohe", "year:2024"], "tags_any": False})["total"] == 3
    assert search.run({"tags": ["site:kaneohe", "site:hanauma"], "tags_any": True})["total"] == 5
    assert search.run({"exclude_tags": ["site:kaneohe"]})["total"] == 1


def test_tags_survive_removal(library, tree):
    from library import db, search, tags

    _index(tree)
    ids = search.candidate_ids({})
    with db.write() as conn:
        tags.add_tags(conn, ids, ["class:usable"])
    assert search.run({"tags": ["class:usable"]})["total"] == 5

    with db.write() as conn:
        tags.remove_tags(conn, ids[:2], ["class:usable"])
    assert search.run({"tags": ["class:usable"]})["total"] == 3


def test_fts_query_is_injection_safe(library, tree):
    """A colon or quote in the box must be data, not FTS5 syntax."""
    from library import search

    _index(tree)
    for needle in ['site:kaneohe', 'sharp" OR "', 'NEAR(a b)', '*']:
        search.run({"text": needle, "mode": "keyword"})   # must not raise


def test_glob_metacharacters_in_folder_are_literal(library, tmp_path):
    from library import search

    root = tmp_path / "odd"
    for folder in ("a[1]", "ab"):
        (root / folder).mkdir(parents=True)
        from conftest import make_image

        make_image(blobs=2).save(root / folder / "x.jpg", quality=90)
    _index(root)
    assert search.run({"folder": "a[1]"})["total"] == 1
