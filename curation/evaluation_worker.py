from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import threading
import time
from pathlib import Path

SPLITS = ("train", "val", "test")
EVALUATOR_VERSION = "oceanframe-classification-v1"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def metrics(actual: list[str], predicted: list[str], top5: list[bool], labels: list[str]) -> dict:
    from sklearn.metrics import accuracy_score, precision_recall_fscore_support

    precision, recall, f1, support = precision_recall_fscore_support(
        actual, predicted, labels=labels, zero_division=0)
    total = len(actual)
    supported = [index for index, count in enumerate(support) if count]
    result = {"images": total, "top1_accuracy": float(accuracy_score(actual, predicted)),
              "top5_accuracy": sum(top5) / total,
              "balanced_accuracy": float(sum(recall[index] for index in supported) / len(supported)) if supported else 0.0,
              "per_class": [{"label": label, "precision": float(precision[index]), "recall": float(recall[index]),
                             "f1": float(f1[index]), "support": int(support[index])} for index, label in enumerate(labels)]}
    for prefix, average in (("macro", "macro"), ("weighted", "weighted")):
        average_precision, average_recall, average_f1, _ = precision_recall_fscore_support(
            actual, predicted, labels=labels, average=average, zero_division=0)
        result.update({f"{prefix}_precision": float(average_precision), f"{prefix}_recall": float(average_recall),
                       f"{prefix}_f1": float(average_f1)})
    return result


def evaluate_task(folder: Path, output: Path, task: dict) -> None:
    if file_hash(folder / "model.pt") != task["model_sha256"]:
        raise ValueError("Model content hash does not match the submitted checkpoint")
    if os.getenv("CURATION_TRUSTED_CHECKPOINTS", "").lower() not in {"1", "true", "on", "yes"}:
        raise ValueError("Trusted-checkpoint execution has not been enabled by the operator")
    import torch
    import ultralytics
    from ultralytics import YOLO

    model = YOLO(str(folder / "model.pt"))
    if model.task != "classify":
        raise ValueError("Selected checkpoint is not an image classifier")
    names = model.names
    labels = [str(names[index]) for index in range(len(names))] if isinstance(names, dict) else list(names)
    if not labels or len(set(labels)) != len(labels) or any(not label or len(label) > 100 for label in labels):
        raise ValueError("Classifier labels must be unique, nonempty and at most 100 characters")
    runtime = {"evaluator_version": EVALUATOR_VERSION, "torch": torch.__version__,
               "ultralytics": ultralytics.__version__, "python": platform.python_version(),
               "cuda": torch.version.cuda}
    if task["action"] == "validate":
        write_json(output / "result.json", {"status": "completed", "class_names": labels,
                                             "model_sha256": task["model_sha256"], "runtime": runtime})
        return
    if labels != task["class_names"]:
        raise ValueError("Classifier labels changed since validation")
    manifest_path = folder / "manifest.json"
    if file_hash(manifest_path) != task["manifest_sha256"]:
        raise ValueError("Evaluation input manifest failed its integrity check")
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual_by_split = {split: [] for split in SPLITS}
    predicted_by_split = {split: [] for split in SPLITS}
    top5_by_split = {split: [] for split in SPLITS}
    temporary = output / "predictions.partial"
    with temporary.open("w", encoding="utf-8") as stream:
        batch_size = task["batch_size"]
        for start in range(0, len(entries), batch_size):
            if (folder / "cancel").exists():
                write_json(output / "result.json", {"status": "cancelled"})
                return
            batch = entries[start:start + batch_size]
            paths = []
            for entry in batch:
                path = folder / "images" / f"{entry['item_id']}.png"
                if file_hash(path) != entry["image_sha256"]:
                    raise ValueError("Evaluation image failed its integrity check")
                paths.append(str(path))
            results = model.predict(source=paths, imgsz=task["image_size"], device=task["device"],
                                    batch=batch_size, verbose=False)
            if len(results) != len(batch):
                raise ValueError("Classifier did not return one result per input image")
            for entry, prediction in zip(batch, results):
                probabilities = prediction.probs
                ranked = [{"label": labels[int(index)], "confidence": float(confidence), "rank": rank}
                          for rank, (index, confidence) in enumerate(
                              zip(probabilities.top5, probabilities.top5conf.tolist()), start=1)]
                record = {"item_id": entry["item_id"], "top_predictions": ranked}
                stream.write(json.dumps(record) + "\n")
                split = entry["split"]
                actual_by_split[split].append(entry["label"])
                predicted_by_split[split].append(ranked[0]["label"])
                top5_by_split[split].append(entry["label"] in {value["label"] for value in ranked})
            write_json(output / "progress.json", {"scored_count": min(start + batch_size, len(entries))})
    temporary.replace(output / "predictions.jsonl")
    summaries = {split: metrics(actual_by_split[split], predicted_by_split[split], top5_by_split[split], labels)
                 for split in SPLITS if actual_by_split[split]}
    summaries["all"] = metrics([label for split in SPLITS for label in actual_by_split[split]],
                               [label for split in SPLITS for label in predicted_by_split[split]],
                               [correct for split in SPLITS for correct in top5_by_split[split]], labels)
    write_json(output / "result.json", {"status": "completed", "results": summaries, "runtime": runtime,
                                         "manifest_sha256": task["manifest_sha256"],
                                         "model_sha256": task["model_sha256"],
                                         "predictions_sha256": file_hash(output / "predictions.jsonl")})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", type=Path, default=Path("/inputs"))
    parser.add_argument("--outputs", type=Path, default=Path("/outputs"))
    args = parser.parse_args()
    args.outputs.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    try:  # how many GPUs this worker can use; the app picks the device for runs it starts itself
        import torch

        gpus = torch.cuda.device_count()
    except Exception:  # noqa: BLE001 — a CPU-only or broken torch means no GPU
        gpus = 0

    def heartbeat():
        while not stop.is_set():
            write_json(args.outputs / "heartbeat.json",
                       {"evaluator_version": EVALUATOR_VERSION, "at": time.time(), "gpus": gpus})
            stop.wait(3)

    threading.Thread(target=heartbeat, daemon=True).start()
    for output in args.outputs.iterdir():
        if output.is_dir() and (output / "started.json").exists() and not (output / "result.json").exists():
            write_json(output / "result.json", {"status": "interrupted", "error": "Worker restarted during inference; create a new run"})
    try:
        while True:
            for task_path in sorted(args.inputs.glob("*/task.json"), key=lambda path: path.stat().st_mtime_ns):
                output = args.outputs / task_path.parent.name
                if (output / "result.json").exists():
                    continue
                try:
                    task = json.loads(task_path.read_text(encoding="utf-8"))
                    output.mkdir(parents=True, exist_ok=True)
                    write_json(output / "started.json", {"at": time.time()})
                    if (task_path.parent / "cancel").exists():
                        write_json(output / "result.json", {"status": "cancelled"})
                    else:
                        evaluate_task(task_path.parent, output, task)
                except Exception as exc:
                    write_json(output / "result.json", {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:2000]})
            time.sleep(1)
    finally:
        stop.set()


if __name__ == "__main__":
    main()