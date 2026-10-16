"""A stand-in for curation.huggingface: recorded repo metadata, local files, no network."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hf"


def _load(name: str):
    from curation.huggingface import RepoInfo

    return RepoInfo(**json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def dataset_info():
    return _load("dataset_info.json")


def model_info():
    return _load("model_info.json")


def install_fake_hub(monkeypatch, infos: dict, files: dict[str, Path]) -> None:
    """infos: repo_id -> RepoInfo; files: 'repo_id/path' -> local file."""
    from curation import huggingface

    def repo_info(ref):
        info = infos.get(ref.repo_id)
        if info is None or info.kind != ref.kind:
            raise HTTPException(404, f"{ref.repo_id} was not found on Hugging Face, or it is private")
        if info.gated or info.private:
            raise HTTPException(422, huggingface.PUBLIC_ONLY)
        return info if ref.ref in {"main", info.sha} else replace(info, sha=ref.ref.ljust(40, "0")[:40])

    def download(kind, repo_id, sha, path):
        local = files.get(f"{repo_id}/{path}")
        if local is None:
            raise HTTPException(404, f"{path} is not in {repo_id}@{sha[:8]}")
        return local

    monkeypatch.setattr(huggingface, "repo_info", repo_info)
    monkeypatch.setattr(huggingface, "download", download)
