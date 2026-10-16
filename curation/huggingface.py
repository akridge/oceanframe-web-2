"""
The Hugging Face Hub, for the curation registry.

Everything here touches the network and nothing else in curation does, so
tests replace these functions (tests/hub_fakes.py). Public repositories only:
no token is ever sent, and gated or private repositories are refused rather
than half-working. Every read is pinned to the commit sha resolved at
registration, so a registry entry means the same bytes for ever.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from curation import settings

PUBLIC_ONLY = "Only public Hugging Face repositories are supported"
KINDS = ("dataset", "model")

_URL = re.compile(
    r"^https?://(?:www\.)?huggingface\.co/(?:(datasets)/)?([\w.-]+/[\w.-]+)"
    r"(?:/(?:tree|blob|resolve)/([^/?#]+)(?:/[^?#]*)?)?/?(?:[?#].*)?$"
)
_REPO_ID = re.compile(r"^[\w.-]+/[\w.-]+$")


@dataclass(frozen=True)
class HubRef:
    kind: str
    repo_id: str
    ref: str = "main"


@dataclass
class RepoInfo:
    kind: str
    repo_id: str
    sha: str
    card: dict = field(default_factory=dict)
    files: list[dict] = field(default_factory=list)
    gated: bool | str = False
    private: bool = False


def _check_owner(repo_id: str) -> None:
    allowed = settings.hf_allowed_orgs()
    if allowed and repo_id.split("/", 1)[0].lower() not in allowed:
        raise HTTPException(403, f"{repo_id.split('/', 1)[0]} is not in CURATION_HF_ALLOWED_ORGS")


def parse_ref(text: str, kind: str | None = None) -> HubRef:
    """A huggingface.co link, or ``owner/name[@ref]`` with a kind."""
    text = (text or "").strip()
    match = _URL.match(text)
    if match:
        url_kind = "dataset" if match.group(1) else "model"
        if match.group(2).split("/", 1)[0] in {"spaces", "datasets", "models"}:
            raise HTTPException(422, "Paste a link to a Hugging Face dataset or model")
        if kind and kind != url_kind:
            raise HTTPException(422, f"That link is a Hugging Face {url_kind}, not a {kind}")
        ref = HubRef(url_kind, match.group(2), match.group(3) or "main")
    else:
        repo_id, _, revision = text.partition("@")
        if kind not in KINDS or not _REPO_ID.match(repo_id):
            raise HTTPException(422, "Paste a huggingface.co link or an owner/name repository id")
        ref = HubRef(kind, repo_id, revision or "main")
    _check_owner(ref.repo_id)
    return ref


def _api_class():
    from huggingface_hub import HfApi  # noqa: PLC0415 — only needed when someone registers

    return HfApi


def repo_info(ref: HubRef) -> RepoInfo:
    from huggingface_hub.utils import (  # noqa: PLC0415
        GatedRepoError, RepositoryNotFoundError, RevisionNotFoundError,
    )

    api = _api_class()(endpoint=settings.hf_endpoint(), token=False)
    try:
        info = api.repo_info(ref.repo_id, repo_type=ref.kind, revision=ref.ref, files_metadata=True)
    except GatedRepoError as exc:
        raise HTTPException(422, PUBLIC_ONLY) from exc
    except RevisionNotFoundError as exc:
        raise HTTPException(404, f"Revision {ref.ref!r} was not found in {ref.repo_id}") from exc
    except RepositoryNotFoundError as exc:
        raise HTTPException(404, f"{ref.repo_id} was not found on Hugging Face, or it is private") from exc
    except Exception as exc:  # noqa: BLE001 — the hub's client raises several transport types
        raise HTTPException(502, f"Hugging Face did not answer: {exc}") from exc
    if getattr(info, "gated", False) or getattr(info, "private", False):
        raise HTTPException(422, PUBLIC_ONLY)
    card = info.card_data.to_dict() if getattr(info, "card_data", None) else {}
    for key in ("pipeline_tag", "library_name"):
        value = getattr(info, key, None) or card.get(key)
        if value:
            card[key] = value
    files = [{"path": sibling.rfilename, "size": getattr(sibling, "size", None)}
             for sibling in (getattr(info, "siblings", None) or [])]
    return RepoInfo(kind=ref.kind, repo_id=ref.repo_id, sha=info.sha, card=card, files=files)


def download(kind: str, repo_id: str, sha: str, path: str) -> Path:
    """One file at a pinned commit, into the hub cache; returns the local path."""
    from huggingface_hub import hf_hub_download  # noqa: PLC0415
    from huggingface_hub.utils import EntryNotFoundError  # noqa: PLC0415

    try:
        local = hf_hub_download(repo_id, path, repo_type=kind, revision=sha,
                                cache_dir=settings.hub_cache_dir(), token=False,
                                endpoint=settings.hf_endpoint())
    except EntryNotFoundError as exc:
        raise HTTPException(404, f"{path} is not in {repo_id}@{sha[:8]}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not download {path} from Hugging Face: {exc}") from exc
    return Path(local)


def browse_url(kind: str, repo_id: str, sha: str) -> str:
    prefix = "datasets/" if kind == "dataset" else ""
    return f"{settings.hf_endpoint()}/{prefix}{repo_id}/tree/{sha}"
