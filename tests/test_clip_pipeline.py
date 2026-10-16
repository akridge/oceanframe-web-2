"""
Exercise the CLIP code path without downloading weights.

Semantic search is the library's headline feature and the one part that a
hermetic suite cannot fully prove: whether "a school of fish over sand" finds
the right photo is a property of the *model*, and checking it means fetching
600 MB from a model host.

What is testable offline is everything this project actually wrote around the
model — preprocessing, tokenising, batching, L2 normalisation, dimension
propagation into the vector store, and the planner's text branch. open_clip can
build the architecture with random weights and no network, so these tests run
the real classes end to end and would catch a shape, device, dtype or
dimension-plumbing bug just as well as real weights would.

``test_semantic_ranking_is_meaningful`` is the other half: it is skipped unless
real weights can be loaded, and asserts the thing only real weights can prove.
Run it with::

    OCEANFRAME_CLIP_WEIGHTS=1 python -m pytest tests/test_clip_pipeline.py -v
"""
from __future__ import annotations

import os

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("torch", reason="the CLIP path needs torch")
pytest.importorskip("open_clip", reason="the CLIP path needs open_clip_torch")

from conftest import make_image  # noqa: E402


@pytest.fixture(scope="module")
def clip():
    """
    One randomly-initialised CLIP for the whole module.

    Building the architecture takes ~25 s even without weights, so it is shared;
    none of these tests mutate the model.  The env changes are undone on the way
    out — leaking LIB_EMBED_BACKEND=clip would make every later module try to
    build CLIP.
    """
    import importlib

    patch = pytest.MonkeyPatch()
    patch.setenv("LIB_CLIP_PRETRAINED", "random")
    patch.setenv("LIB_EMBED_BACKEND", "clip")

    from library import settings

    importlib.reload(settings)
    from library.embed.clip import ClipEmbedder

    yield ClipEmbedder()

    patch.undo()
    importlib.reload(settings)
    import library.embed as embed_module

    embed_module.reset_embedder()


# ── The embedder ──────────────────────────────────────────────────────────────

def test_images_and_text_land_in_one_unit_normalised_space(clip):
    images = clip.embed_images([make_image(blobs=3), make_image(blobs=7), make_image(blur=4.0)])
    text = clip.embed_text(["a school of fish", "bleached coral"])

    assert images.shape == (3, clip.dim)
    assert text.shape == (2, clip.dim)
    assert images.dtype == np.float32 and text.dtype == np.float32
    # Unit norm is what lets the vector store treat a dot product as cosine.
    assert np.allclose(np.linalg.norm(images, axis=1), 1.0, atol=1e-5)
    assert np.allclose(np.linalg.norm(text, axis=1), 1.0, atol=1e-5)
    # Shared space: a dot product between the two must be a valid cosine.
    scores = images @ text.T
    assert scores.shape == (3, 2)
    assert np.all(scores >= -1.001) and np.all(scores <= 1.001)


def test_empty_batches_keep_their_shape(clip):
    assert clip.embed_images([]).shape == (0, clip.dim)
    assert clip.embed_text([]).shape == (0, clip.dim)


def test_grayscale_and_rgba_inputs_are_accepted(clip):
    """Real libraries hold palette PNGs and CMYK scans, not just clean RGB."""
    odd = [
        make_image(blobs=2).convert("L"),
        make_image(blobs=2).convert("RGBA"),
        make_image(blobs=2).convert("P"),
        Image.new("RGB", (7, 3), (10, 20, 30)),      # absurd aspect ratio
    ]
    vectors = clip.embed_images(odd)
    assert vectors.shape == (len(odd), clip.dim)
    assert np.isfinite(vectors).all()


def test_untrained_model_is_self_identifying(clip):
    """
    A catalog embedded with random weights must never be mistaken for a real one.

    The tag is part of the embedder name, and the name is the vector store's
    identity, so swapping in real weights invalidates the vectors instead of
    silently mixing two spaces.
    """
    assert clip.untrained is True
    assert clip.name.endswith("/random")
    assert clip.supports_text is True


def test_empty_pretrained_tag_is_rejected():
    """open_clip only logs a warning; silently meaningless embeddings are worse."""
    from library.embed.clip import _resolve_pretrained

    with pytest.raises(ValueError, match="LIB_CLIP_PRETRAINED is empty"):
        _resolve_pretrained("")
    assert _resolve_pretrained("random") is None
    assert _resolve_pretrained("laion2b_s34b_b79k") == "laion2b_s34b_b79k"


# ── The vector store at CLIP's dimensionality ─────────────────────────────────

def test_vector_store_round_trips_clip_dimensions(library, clip):
    from library.vectorstore import VectorStore

    store = VectorStore()
    store.ensure_model(clip.dim, clip.name)

    vectors = clip.embed_images([make_image(blobs=i) for i in range(6)])
    rows = store.add(vectors)
    assert rows == list(range(6))
    assert store.dim == clip.dim

    query = clip.embed_text(["coral"])[0]
    ranked = store.search(query, top_k=6)
    assert len(ranked) == 6
    scores = [s for _, s in ranked]
    assert scores == sorted(scores, reverse=True)
    # Exact against brute force, at 512 dimensions rather than the hash 256.
    expected = list(np.argsort(-(vectors @ query))[:6])
    assert [r for r, _ in ranked] == expected


def test_switching_embedding_model_invalidates_the_catalog(library, clip):
    """
    The hash descriptor is 256-d and CLIP is 512-d.  Upgrading must wipe the
    vectors rather than leave two incompatible spaces in one file.
    """
    from library.vectorstore import VectorStore

    store = VectorStore()
    store.ensure_model(256, "hash")
    store.add(np.random.RandomState(0).rand(10, 256).astype(np.float32))
    assert store.rows == 10

    wiped = store.ensure_model(clip.dim, clip.name)
    assert wiped is True
    assert store.rows == 0
    assert store.dim == clip.dim


# ── The planner's text branch ─────────────────────────────────────────────────

def _use_clip(clip):
    """Point the embedder singleton at the shared model, bypassing the loader."""
    import library.embed as embed_module

    embed_module._instance = clip
    embed_module._status.update(
        backend=clip.name, dim=clip.dim, detail="test", supports_text=True
    )


def test_text_query_reaches_the_vector_planner(library, tree, clip):
    """
    With a text-capable embedder the planner must take the vector branch — no
    keyword fallback, no advisory note.
    """
    from library import indexer, jobs, search

    _use_clip(clip)
    job = jobs.start_job("index", {}, lambda j: indexer.index_source(j, str(tree)))
    for _ in jobs.stream(job, heartbeat=120):
        pass
    assert job.status == "done", job.message
    assert job.result["text_search"] is True

    result = search.run({"text": "a photograph of coral", "page_size": 5})
    assert result["vector"] is True
    assert result["note"] == "", "a CLIP-backed query must not fall back to keywords"
    assert result["items"], "expected ranked results"
    for item in result["items"]:
        assert -1.001 <= item["score"] <= 1.001
    scores = [i["score"] for i in result["items"]]
    assert scores == sorted(scores, reverse=True)


def test_text_query_composes_with_structured_filters(library, tree, clip):
    """The filter-then-rank order has to hold at CLIP's dimensionality too."""
    from library import indexer, jobs, search

    _use_clip(clip)
    job = jobs.start_job("index", {}, lambda j: indexer.index_source(j, str(tree)))
    for _ in jobs.stream(job, heartbeat=120):
        pass

    scoped = search.run({"text": "coral", "folder": "2024/kaneohe", "page_size": 10})
    assert scoped["items"]
    assert all(i["folder"].startswith("2024/kaneohe") for i in scoped["items"])
    assert len(scoped["items"]) < search.run({"text": "coral", "page_size": 10})["total"]


# ── The half that needs real weights ──────────────────────────────────────────

@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("OCEANFRAME_CLIP_WEIGHTS", "") not in {"1", "true", "yes"},
    reason="set OCEANFRAME_CLIP_WEIGHTS=1 to download real CLIP weights",
)
def test_semantic_ranking_is_meaningful():
    """
    The one claim random weights cannot support: that the ranking means
    something.  Two visually unmistakable images, two matching phrases.
    """
    import importlib

    from library import settings

    os.environ["LIB_CLIP_PRETRAINED"] = os.getenv("CLIP_TAG", "laion2b_s34b_b79k")
    os.environ["LIB_EMBED_BACKEND"] = "clip"
    importlib.reload(settings)
    from library.embed.clip import ClipEmbedder

    embedder = ClipEmbedder()
    assert not embedder.untrained, "this test is pointless without real weights"

    red_square = Image.new("RGB", (224, 224), (200, 20, 20))
    blue_ocean = Image.new("RGB", (224, 224), (10, 90, 160))
    images = embedder.embed_images([red_square, blue_ocean])
    text = embedder.embed_text(["a solid red square", "deep blue ocean water"])

    scores = images @ text.T
    assert scores[0, 0] > scores[0, 1], "red image should match the red phrase"
    assert scores[1, 1] > scores[1, 0], "blue image should match the ocean phrase"
