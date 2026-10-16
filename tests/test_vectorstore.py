"""Vector store: exactness, persistence, and subset ranking."""
from __future__ import annotations

import numpy as np


def _unit(rows, dim, seed=0):
    from library.embed.base import l2_normalise

    return l2_normalise(np.random.RandomState(seed).randn(rows, dim))


def test_search_matches_brute_force(library):
    from library.vectorstore import VectorStore

    store = VectorStore()
    store.ensure_model(16, "test")
    vectors = _unit(400, 16)
    store.add(vectors[:250])
    store.add(vectors[250:])

    query = vectors[7]
    got = [row for row, _ in store.search(query, top_k=10)]
    expected = list(np.argsort(-(vectors @ query))[:10])
    assert got == expected


def test_subset_search_only_returns_candidates(library):
    from library.vectorstore import VectorStore

    store = VectorStore()
    store.ensure_model(8, "test")
    store.add(_unit(100, 8))

    candidates = [3, 17, 42, 88]
    results = store.search(_unit(1, 8, seed=5)[0], top_k=10, candidate_rows=candidates)
    assert {row for row, _ in results} <= set(candidates)
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)


def test_reload_recovers_state(library):
    from library.vectorstore import VectorStore

    store = VectorStore()
    store.ensure_model(4, "test")
    store.add(_unit(20, 4))

    reopened = VectorStore()
    assert reopened.rows == 20
    assert reopened.dim == 4
    assert reopened.model == "test"


def test_put_overwrites_in_place(library):
    from library.embed.base import l2_normalise
    from library.vectorstore import VectorStore

    store = VectorStore()
    store.ensure_model(4, "test")
    store.add(_unit(10, 4))

    replacement = l2_normalise(np.array([1.0, 0.0, 0.0, 0.0]))[0]
    store.put(5, replacement)
    assert np.allclose(store.get([5])[0], replacement)
    assert store.rows == 10


def test_model_change_wipes_vectors(library):
    from library.vectorstore import VectorStore

    store = VectorStore()
    store.ensure_model(8, "hash")
    store.add(_unit(30, 8))

    assert store.ensure_model(512, "clip") is True
    assert store.rows == 0
    assert store.dim == 512
