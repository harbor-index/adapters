"""Check generated task contents against the pinned JSONL using Harbor's own config parser."""

import argparse
import hashlib
import json
import os
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from harbor.models.task.task import Task
from harbor.models.task.verifier_mode import resolve_verifier_environment_definition


def check(directory, rows):
    ids = []
    difficulty = Counter()
    references = []
    suffix = '\n\nSave your complete response, including any code blocks, to `/workspace/answer.md`.\n'
    for path in sorted(directory.resolve().iterdir()):
        task = Task(path)
        config = task.config
        index = config.metadata["upstream_index"]
        row = rows[index]
        definition = resolve_verifier_environment_definition(config, task.paths, None)
        checks = {
            "id": path.name == f"artifactsbench-{index:04d}" and config.task.name == f"tencent/{path.name}",
            "instruction": (path / "instruction.md").read_text() == row["question"] + suffix,
            "rubric": json.loads((path / "tests/record.json").read_bytes()) == row,
            "verifier": definition and definition.bundled_tests and definition.directory == path / "tests",
            "test_script": os.access(path / "tests/test.sh", os.X_OK),
            "artifact": config.artifacts == ["/workspace/answer.md"],
        }
        if not all(checks.values()):
            raise ValueError(f"{path}: failed {[k for k, v in checks.items() if not v]}")
        ids.append(index)
        difficulty[row["difficulty"]] += 1
        if (path / "solution/solve.sh").is_file():
            references.append(index)
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate task IDs")
    return {"parsed_tasks": len(ids), "difficulty": dict(difficulty), "reference_replay_ids": references}, ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--full", type=Path, required=True)
    parser.add_argument("--parity", type=Path, required=True)
    args = parser.parse_args()
    data = args.dataset.read_bytes()
    rows = {r["index"]: r for r in map(json.loads, data.splitlines())}
    full, all_ids = check(args.full, rows)
    parity, subset = check(args.parity, rows)
    if set(rows) != set(all_ids) or len(subset) != 60:
        raise ValueError("Unexpected generated coverage")
    parity["ids"] = subset
    print(json.dumps({"dataset_sha256": hashlib.sha256(data).hexdigest(), "harbor": version("harbor"),
                      "full": full, "proposed_parity": parity}, indent=2))


if __name__ == "__main__":
    main()
