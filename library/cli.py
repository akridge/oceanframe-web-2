#!/usr/bin/env python3
"""
Batch entry point for the image library.

The web UI drives the same functions, but a long first crawl of a large bucket
belongs in a terminal (or a Cloud Run job / cron), not in a browser tab:

    python -m library.cli index gs://my-survey-bucket/2024
    python -m library.cli index /data/surveys --force
    python -m library.cli index "gs://nmfs_odp_pifsc/PIFSC/ESD/ARP/..." \
        --tag-pattern '(?P<split>train|val|test)/(?P<class>[A-Z_]+)' 
    python -m library.cli embed --rebuild
    python -m library.cli annotate --annotator yolo --query '{"quality_min": 50}'
    python -m library.cli annotate --annotator sam3 --prompts "fish,bleached coral"
    python -m library.cli search "school of fish over sand" --limit 20
    python -m library.cli dataset create reef-v1 --query '{"tags":["site:kaneohe"]}'
    python -m library.cli dataset export reef-v1 --kind yolo-detect
    python -m library.cli stats
"""
from __future__ import annotations

import argparse
import json
import sys

from library import datasets as datasets_mod
from library import db, indexer, jobs, search, settings, tags as tags_mod


def _run(kind: str, params: dict, target) -> dict:
    """Run a job synchronously and report progress on stderr."""
    job = jobs.start_job(kind, params, target)
    # Redraw in place on a terminal; emit one line per 10% when the output is a
    # log file or a pipe, so CI logs stay readable.
    interactive = sys.stderr.isatty()
    step = 1 if interactive else 10
    last = -step

    for line in jobs.stream(job, heartbeat=5):
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        if event.get("type") == "progress":
            percent = int((event.get("frac") or 0) * 100)
            if percent - last >= step or percent == 100:
                last = percent
                message = event.get("message", "")[:70]
                end = "" if interactive else "\n"
                print(f"\r  {percent:3d}%  {message:<70}", end=end, file=sys.stderr, flush=True)
        elif event.get("type") == "log":
            print(f"\r  {event['message'][:76]:<76}", file=sys.stderr, flush=True)
    if interactive:
        print(file=sys.stderr)

    if job.status == "error":
        print(f"Failed: {job.message}", file=sys.stderr)
        if job.result.get("traceback"):
            print(job.result["traceback"], file=sys.stderr)
        raise SystemExit(1)
    return job.result


def _parse_query(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SystemExit(f"--query must be JSON: {exc}") from exc


def _csv(raw: str | None) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_index(args) -> None:
    root = args.root or settings.DEFAULT_SOURCE
    if not root:
        raise SystemExit("Give a source root, or set LIB_SOURCE.")
    result = _run("index", {"root": root},
                  lambda job: indexer.index_source(job, root, force=args.force, limit=args.limit,
                                                   label=args.label, prune=not args.no_prune,
                                                   tag_pattern=args.tag_pattern))
    print(json.dumps(result, indent=2))


def cmd_embed(args) -> None:
    print(json.dumps(_run("embed", {}, lambda job: indexer.embed_missing(job, rebuild=args.rebuild)), indent=2))


def cmd_annotate(args) -> None:
    ids = search.candidate_ids(_parse_query(args.query))
    if args.limit:
        ids = ids[: args.limit]
    if not ids:
        raise SystemExit("That query matched nothing.")
    print(f"Annotating {len(ids):,} assets with {args.annotator}", file=sys.stderr)
    result = _run("annotate", {"annotator": args.annotator},
                  lambda job: indexer.annotate_assets(
                      job, ids, annotator=args.annotator, prompts=_csv(args.prompts) or None,
                      model_ref=args.model or None, replace=not args.keep))
    print(json.dumps(result, indent=2))


def cmd_search(args) -> None:
    query = _parse_query(args.query)
    if args.text:
        query["text"] = args.text
    query.setdefault("page_size", args.limit)
    result = search.run(query)
    if result.get("note"):
        print(f"# {result['note']}", file=sys.stderr)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"{result['matched']:,} of {result['total']:,} matched")
    for item in result["items"]:
        score = f"{item['score']:.3f}  " if "score" in item else ""
        print(f"  {score}q={item['quality']:5.1f}  {item['folder']}/{item['name']}")


def cmd_tag(args) -> None:
    ids = search.candidate_ids(_parse_query(args.query))
    if not ids:
        raise SystemExit("That query matched nothing.")
    with db.write() as conn:
        if args.remove:
            n = tags_mod.remove_tags(conn, ids, _csv(args.names))
            print(f"Removed {n} tag assignments from {len(ids):,} assets")
        else:
            n = tags_mod.add_tags(conn, ids, _csv(args.names))
            print(f"Added {n} tag assignments across {len(ids):,} assets")


def cmd_dataset(args) -> None:
    if args.action == "list":
        for dataset in datasets_mod.list_datasets():
            splits = " ".join(f"{k}={v}" for k, v in sorted(dataset["splits"].items()))
            print(f"  {dataset['id']:>3}  {dataset['name']:<28} {dataset['size']:>7,}  {splits}")
        return

    if args.action == "create":
        dataset = datasets_mod.create(
            args.name, query=_parse_query(args.query), notes=args.notes,
            split_mode=args.split_mode, ratios=None)
        print(json.dumps(dataset, indent=2))
        return

    dataset = _find_dataset(args.name)
    if args.action == "delete":
        datasets_mod.delete(dataset["id"])
        print(f"Deleted {dataset['name']}")
        return

    result = _run("export", {"dataset": dataset["name"]},
                  lambda job: datasets_mod.export(
                      job, dataset["id"], kind=args.kind, include_images=not args.no_images,
                      conf=args.conf, labels=_csv(args.labels) or None, tag_prefix=args.tag_prefix))
    print(json.dumps(result, indent=2))


def _find_dataset(name: str) -> dict:
    for dataset in datasets_mod.list_datasets():
        if dataset["name"] == name or str(dataset["id"]) == name:
            return dataset
    raise SystemExit(f"No dataset named '{name}'")


def cmd_stats(_args) -> None:
    print(json.dumps(search.stats(), indent=2))
    print(json.dumps({"sources": indexer.list_sources()}, indent=2))


def cmd_doctor(_args) -> None:
    """Report what is installed and what each capability needs."""
    from library.annotate import annotator_statuses  # noqa: PLC0415
    from library.embed import get_embedder  # noqa: PLC0415

    embedder = get_embedder()
    print(f"data dir       : {settings.DATA_DIR}")
    print(f"default source : {settings.DEFAULT_SOURCE or '(unset — set LIB_SOURCE)'}")
    print(f"embedder       : {embedder.describe()}")
    print(f"  text search  : {'yes' if embedder.supports_text else 'no'}")
    for state in annotator_statuses():
        print(f"annotator {state['name']:<6}: {'ready' if state['available'] else 'unavailable'} — {state['detail']}")
    import importlib.util  # noqa: PLC0415

    has_gcs = importlib.util.find_spec("google.cloud.storage") is not None
    print(f"gcs client     : {'installed' if has_gcs else 'missing — pip install google-cloud-storage'}")


# ── Parser ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m library.cli", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="Crawl a source into the catalog")
    p.add_argument("root", nargs="?", default="", help="gs://bucket/prefix or a local directory")
    p.add_argument("--force", action="store_true", help="Re-read objects even if unchanged")
    p.add_argument("--limit", type=int, default=0,
                   help="Stop after N *new* objects. Resumable: run it again to take the next N, "
                        "which is how you walk a bucket too large for one sitting.")
    p.add_argument("--label", default="", help="Friendly name for the source")
    p.add_argument("--no-prune", action="store_true", help="Do not flag vanished objects as missing")
    p.add_argument("--tag-pattern", default=None,
                   help="Regex whose named groups become key:value tags, remembered for this source. "
                        r"e.g. '(?P<split>train|val|test)/(?P<class>[A-Z_]+)'")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("embed", help="Compute missing vectors (or rebuild them all)")
    p.add_argument("--rebuild", action="store_true")
    p.set_defaults(func=cmd_embed)

    p = sub.add_parser("annotate", help="Run YOLO or SAM 3 over matching assets")
    p.add_argument("--annotator", default="yolo", choices=["yolo", "sam3"])
    p.add_argument("--query", default="", help="JSON query selecting the assets")
    p.add_argument("--prompts", default="", help="Comma-separated concepts (SAM 3) or class filter (YOLO)")
    p.add_argument("--model", default="", help="Override the checkpoint path")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--keep", action="store_true", help="Keep previous detections from this model")
    p.set_defaults(func=cmd_annotate)

    p = sub.add_parser("search", help="Query the catalog")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--query", default="", help="JSON filters merged with the text")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("tag", help="Add or remove tags on everything a query matches")
    p.add_argument("names", help="Comma-separated tag names")
    p.add_argument("--query", default="", help="JSON query selecting the assets")
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_tag)

    p = sub.add_parser("dataset", help="Create, list, export or delete datasets")
    p.add_argument("action", choices=["create", "list", "export", "delete"])
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--query", default="")
    p.add_argument("--notes", default="")
    p.add_argument("--split-mode", default="by_folder", choices=list(datasets_mod.SPLIT_MODES))
    p.add_argument("--kind", default="yolo-detect", choices=list(datasets_mod.EXPORT_KINDS))
    p.add_argument("--no-images", action="store_true", help="Labels and manifest only")
    p.add_argument("--conf", type=float, default=0.25)
    p.add_argument("--labels", default="", help="Restrict to these classes")
    p.add_argument("--tag-prefix", default="", help="Tag namespace for yolo-classify (default class:)")
    p.set_defaults(func=cmd_dataset)

    sub.add_parser("stats", help="Catalog summary").set_defaults(func=cmd_stats)
    sub.add_parser("doctor", help="Report installed backends and what is missing").set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    db.init_db()
    try:
        args.func(args)
    except BrokenPipeError:
        # Downstream `head`/`less` went away; exit quietly like any other tool.
        try:
            sys.stdout.close()
        finally:
            raise SystemExit(0) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
