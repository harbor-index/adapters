"""Generate deterministic Harbor tasks for native TabArena outer splits."""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
from pathlib import Path
from string import Template

import numpy as np

from .data import AssetCache, default_cache_dir, load_manifest, load_task, task_slug

TEMPLATE_DIR = Path(__file__).parent / "task-template"


class TabArenaAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        cache_dir: Path | None = None,
        manifest_only: bool = False,
    ):
        if limit is not None and limit < 1:
            raise ValueError("--limit must be positive")
        self.output_dir = Path(output_dir)
        self.limit, self.overwrite, self.task_ids = limit, overwrite, task_ids
        self.manifest_only = manifest_only
        self.cache = AssetCache(
            cache_dir if cache_dir is not None else default_cache_dir()
        )

    def select_tasks(self) -> list[dict]:
        selected = sorted(load_manifest()["splits"], key=task_slug)
        if self.task_ids:
            requested = {name.removeprefix("tabarena/") for name in self.task_ids}
            known = {task_slug(item) for item in selected}
            unknown = requested - known
            if unknown:
                raise ValueError(f"Unknown task IDs: {', '.join(sorted(unknown))}")
            selected = [item for item in selected if task_slug(item) in requested]
        return selected[: self.limit] if self.limit is not None else selected

    @staticmethod
    def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)

    def run(self) -> None:
        selected = self.select_tasks()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "upstream_commit": load_manifest()["upstream_commit"],
            "release": "TabArena-Lite",
            "declared_release_task_count": 51,
            "selection": "lite",
            "selected_task_count": len(selected),
            "tasks": selected,
        }
        (self.output_dir / "task_manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        if self.manifest_only:
            return
        for split in selected:
            destination = self.output_dir / task_slug(split)
            if destination.exists() and not self.overwrite:
                raise FileExistsError(
                    f"Task already exists: {destination}; use --overwrite"
                )
            data = load_task(split, self.cache)
            with tempfile.TemporaryDirectory(
                dir=self.output_dir, prefix=".tabarena-build-"
            ) as temporary:
                task = Path(temporary) / "task"
                shutil.copytree(
                    TEMPLATE_DIR,
                    task,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                env_data, tests = task / "environment" / "data", task / "tests"
                env_data.mkdir(parents=True, exist_ok=True)
                tests.mkdir(parents=True, exist_ok=True)
                metadata = data.metadata
                features = [feature["name"] for feature in metadata["features"]]
                self._write_csv(
                    env_data / "train.csv",
                    features + [metadata["target"]],
                    data.train_rows,
                )
                self._write_csv(tests / "test.csv", features, data.test_rows)
                # JSON is authoritative: preserve null separately from literal NA/empty strings.
                (env_data / "train.json").write_text(
                    json.dumps(
                        {
                            "columns": features + [metadata["target"]],
                            "data": data.train_rows,
                        },
                        allow_nan=False,
                    )
                    + "\n"
                )
                (tests / "test.json").write_text(
                    json.dumps(
                        {"columns": features, "data": data.test_rows}, allow_nan=False
                    )
                    + "\n"
                )
                order = np.random.default_rng(metadata["seed"]).permutation(
                    len(data.test_labels)
                )
                classes = metadata["classes"]
                answers = [
                    [float(value == label) for label in classes]
                    if classes
                    else [float(value)]
                    for value in data.test_labels
                ]
                (task / "solution" / "answers.json").write_text(
                    json.dumps(
                        {
                            "classes": classes,
                            "predictions": [answers[int(i)] for i in order],
                        },
                        allow_nan=False,
                    )
                    + "\n"
                )
                serialized = json.dumps(metadata, indent=2) + "\n"
                (env_data / "metadata.json").write_text(serialized)
                (tests / "metadata.json").write_text(serialized)
                (tests / "test_labels.json").write_text(
                    json.dumps(data.test_labels) + "\n"
                )
                (tests / "manifest.json").write_text(
                    json.dumps(data.provenance, indent=2) + "\n"
                )
                variables = {
                    "task_name": f"tabarena/{task_slug(split)}",
                    "dataset_name": metadata["dataset_name"],
                    "task_id": str(metadata["task_id"]),
                    "problem_type": metadata["problem_type"],
                    "metric": metadata["metric"],
                    "target": metadata["target"],
                    "feature_count": str(len(features)),
                    "train_rows": str(metadata["train_rows"]),
                    "classes_json": json.dumps(metadata["classes"]),
                }
                for name in ["instruction.md", "task.toml"]:
                    path = task / name
                    path.write_text(
                        Template(path.read_text()).safe_substitute(variables)
                    )
                for script in task.rglob("*.sh"):
                    script.chmod(script.stat().st_mode | 0o111)
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.move(str(task), destination)
