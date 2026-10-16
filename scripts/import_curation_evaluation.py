from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from curation import evaluation, settings


def install(source: Path, destination: Path, split: str, *, require_benchmark_shape: bool = True) -> Path:
    provenance = evaluation.validate_artifacts(source, split, require_benchmark_shape=require_benchmark_shape)
    root = destination / "v0"
    target = root if split == "test" else root / split
    if split != "test":
        baseline = evaluation.validate_artifacts(root, "test", require_benchmark_shape=require_benchmark_shape)
        if provenance["class_names"] != baseline["class_names"] or provenance["model_sha256"] != baseline["model_sha256"]:
            raise ValueError("Supplementary split must use the exact baseline model and class ordering")
    if any((target / name).exists() for name in evaluation.REQUIRED):
        for name in evaluation.REQUIRED:
            if not (target / name).is_file() or (target / name).read_bytes() != (source / name).read_bytes():
                raise ValueError("Installed benchmark evidence is immutable; the supplied bundle differs")
        return target
    destination.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".evaluation-", dir=destination))
    try:
        for name in evaluation.REQUIRED:
            shutil.copyfile(source / name, staging / name)
        evaluation.validate_artifacts(staging, split, require_benchmark_shape=require_benchmark_shape)
        if target.exists():
            raise ValueError("Destination already exists without a complete evaluation bundle")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and install immutable OASIS T3 benchmark results")
    parser.add_argument("source", type=Path)
    parser.add_argument("--destination", type=Path, default=settings.eval_dir())
    parser.add_argument("--split", choices=("test", "train", "val"), default="test")
    parser.add_argument("--model", type=Path, help="Also verify the evaluated model's bytes against provenance")
    args = parser.parse_args()
    try:
        provenance = evaluation.validate_artifacts(args.source, args.split)
        if args.model:
            with args.model.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != provenance["model_sha256"]:
                    raise ValueError("Supplied model does not match evaluation provenance")
        target = install(args.source, args.destination, args.split)
        print(f"Installed verified {args.split} evaluation: {target}")
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"Evaluation not installed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())