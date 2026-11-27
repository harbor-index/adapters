"""Import Tencent's published GPT-5 responses as replay solutions.

Tencent released one GPT-5 response per question, together with the score its
Gemini 2.5 Pro judge gave. A response is imported only when its question occurs
exactly once in both files and its complete checklist equals the pinned record.
Published scores go to the manifest for comparison and never become a reward.
"""

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile

from .adapter import ArtifactsBenchAdapter

REVISION = "88c968b87e150e63de7660937e6dcfb8e7d643cf"
RELEASE_PATH = "dataset/release_data_20250725/gemini_2.5_pro_judge_gpt-5.jsonl"
RELEASE_SHA256 = "eab68e0cf640dcd9b33802acbc16a8463ea798b1cad18b116c0421a54173577a"
# Reviewed by hand: task 73 is a static SVG whose published response scored 99.
REVIEWED = {73: "ce5eeb7b5fd0c8851b4d91382e4ec5ca1ccb17ae06f005c7358739abd22bd9f4"}
MANIFEST = "manifest.json"
NATIVE_INPUT = "replay-native-input.jsonl"


def _read_release(path):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != RELEASE_SHA256:
        raise ValueError(f"{path}: SHA-256 mismatch for upstream revision {REVISION}")
    by_question = {}
    for line in data.splitlines():
        if line.strip():
            row = json.loads(line)
            by_question.setdefault(row["question"], []).append(row)
    return by_question


def _match(task, published, question_counts):
    if question_counts[task["question"]] != 1:
        return None, "question repeats in the pinned dataset"
    candidates = published.get(task["question"], [])
    if len(candidates) != 1:
        return None, f"{len(candidates)} published responses share this question"
    result = candidates[0]
    # The release uses a different class encoding. Join on the original prompt
    # and verify the entire rubric, including ordering and point totals.
    if result["checklist"] != task["checklist"]:
        return None, "published checklist differs from the pinned dataset"
    answer = result.get("model_infer_result")
    if not isinstance(answer, str) or not answer.strip():
        return None, "published response is empty"
    return result, None


def _write_atomic(path, data):
    with NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
        try:
            temp.write(data)
            temp.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def import_references(dataset, release, output_dir, split="full", task_ids=None, overwrite=False):
    """Write NNNN.md answers, a manifest and native-side input rows. Return the manifest."""
    every_task = ArtifactsBenchAdapter(output_dir, dataset=dataset)._records()
    selected = ArtifactsBenchAdapter(output_dir, dataset=dataset, split=split, task_ids=task_ids)._records()
    published = _read_release(release)
    question_counts = {}
    for task in every_task:
        question_counts[task["question"]] = question_counts.get(task["question"], 0) + 1

    if output_dir.exists():
        if not overwrite:
            raise ValueError(f"{output_dir} exists. Use --overwrite to replace it")
        if not (output_dir / MANIFEST).is_file():
            raise ValueError(f"Refusing to replace {output_dir}: no {MANIFEST} from a previous import")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    imported, skipped, native_rows = [], [], []
    for task in selected:
        result, reason = _match(task, published, question_counts)
        if reason:
            skipped.append({"index": task["index"], "reason": reason})
            continue
        answer = result["model_infer_result"]
        digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        reviewed = REVIEWED.get(task["index"])
        if reviewed and digest != reviewed:
            raise ValueError(f"Task {task['index']}: published response differs from the reviewed copy")
        _write_atomic(output_dir / f"{task['index']:04d}.md", answer.encode("utf-8"))
        imported.append({
            "index": task["index"],
            "difficulty": task["difficulty"],
            "answer_sha256": digest,
            "published_score": float(result["gemini_mllm_score"]),
        })
        native_rows.append(dict(task, answer=answer))

    manifest = {
        "source": f"https://github.com/Tencent-Hunyuan/ArtifactsBenchmark/blob/{REVISION}/{RELEASE_PATH}",
        "release_sha256": RELEASE_SHA256,
        "published_judge": "gemini-2.5-pro (Tencent, 2025)",
        "split": split,
        "selected": len(selected),
        "imported": imported,
        "skipped": skipped,
    }
    native = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in native_rows)
    _write_atomic(output_dir / NATIVE_INPUT, native.encode("utf-8"))
    _write_atomic(output_dir / MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, help="Pinned artifacts_bench.json, otherwise downloaded")
    parser.add_argument("--release", type=Path, required=True, help=f"Pinned {RELEASE_PATH}")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--split", choices=("full", "parity"), default="full")
    parser.add_argument("--task-ids", type=int, nargs="+")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        manifest = import_references(args.dataset, args.release, args.output_dir,
                                     args.split, args.task_ids, args.overwrite)
    except (ValueError, OSError) as error:
        parser.exit(1, f"artifactsbench.reference: {error}\n")
    print(f"Imported {len(manifest['imported'])} of {manifest['selected']} selected tasks into {args.output_dir}")
    for entry in manifest["skipped"]:
        print(f"  skipped {entry['index']}: {entry['reason']}")


if __name__ == "__main__":
    main()
