"""Generate Harbor tasks from the DataSpace public-reference release."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

HF_REPO_ID = "HKUSTDial/DataSpace"
HF_REVISION = "96be19257d7aa251dbce4771d46a26fc36f6dc52"
ARCHIVE_PATH = "release/DataSpace-Benchmark.zip"
ARCHIVE_SHA256 = "68f221fafcf0b50396eca55f8298af1ad73d586a6cac6995916afca535a560af"
ARCHIVE_ROOT = "DataSpace-Benchmark"
UPSTREAM_CODE_REVISION = "6491caa4c70cc06cacb6103ba73cefb00746abfe"
EXPECTED_PUBLIC_TASKS = 60

DEFAULT_CACHE_DIR = Path(
    os.environ.get("DATASPACE_CACHE_DIR", Path.home() / ".cache" / "harbor-dataspace")
)
TEMPLATE_ROOT = Path(__file__).with_name("task-template")
TASK_ID_PATTERN = re.compile(r"^task_[1-9][0-9]*$")
MODALITY_COLUMNS = (
    ("csv", "has_csv"),
    ("json", "has_json"),
    ("sqlite", "has_sqlite"),
    ("markdown", "has_markdown"),
    ("pdf", "has_pdf"),
    ("video", "has_video"),
)


@dataclass(frozen=True)
class DataSpaceTask:
    task_id: str
    question: str
    context_dir: Path
    gold_path: Path
    config_path: Path
    order_sensitive: bool
    modalities: tuple[str, ...]
    question_language: str


class DataSpaceAdapter:
    """Convert DataSpace tasks with public gold answers into Harbor tasks."""

    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: Iterable[str] | None = None,
        benchmark_root: Path | None = None,
        cache_dir: Path = DEFAULT_CACHE_DIR,
    ):
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = [_normalize_task_id(t) for t in task_ids or []]
        self.benchmark_root = Path(benchmark_root) if benchmark_root else None
        self.cache_dir = Path(cache_dir)

    def run(self) -> list[Path]:
        root = self._resolve_benchmark_root()
        tasks = self._load_tasks(root)
        if self.task_ids:
            unknown = sorted(set(self.task_ids) - {t.task_id for t in tasks})
            if unknown:
                raise ValueError(
                    "Unknown or non-public DataSpace task id(s): " + ", ".join(unknown)
                )
            tasks = [t for t in tasks if t.task_id in set(self.task_ids)]
        if self.limit is not None:
            tasks = tasks[: self.limit]

        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated: list[Path] = []
        for task in tasks:
            task_dir = self.output_dir / task.task_id
            if task_dir.exists():
                if not self.overwrite:
                    continue
                shutil.rmtree(task_dir)
            self._write_task(task_dir, task)
            generated.append(task_dir)
        return generated

    def _resolve_benchmark_root(self) -> Path:
        if self.benchmark_root is not None:
            return _validate_benchmark_root(self.benchmark_root)
        extracted = self.cache_dir / ARCHIVE_ROOT
        if extracted.is_dir():
            return _validate_benchmark_root(extracted)
        archive = _download_archive(self.cache_dir)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(self.cache_dir)
        return _validate_benchmark_root(extracted)

    def _load_tasks(self, root: Path) -> list[DataSpaceTask]:
        public = json.loads(
            (root / "metadata" / "public_reference_tasks.json").read_text(
                encoding="utf-8"
            )
        )["task_ids"]
        config_ids = sorted(
            p.stem for p in (root / "evaluation" / "configs").glob("task_*.json")
        )
        if sorted(public) != config_ids or len(public) != EXPECTED_PUBLIC_TASKS:
            raise RuntimeError(
                f"Expected {EXPECTED_PUBLIC_TASKS} public-reference tasks with "
                f"matching configs; found {len(public)} listed and "
                f"{len(config_ids)} configs in {root}"
            )
        metadata = _load_task_metadata(root / "metadata" / "task_metadata.csv")

        tasks = []
        for task_id in sorted(public, key=_task_sort_key):
            task_dir = root / "input" / task_id
            payload = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
            config_path = root / "evaluation" / "configs" / f"{task_id}.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            question = str(payload["question"]).strip()
            tasks.append(
                DataSpaceTask(
                    task_id=task_id,
                    question=question,
                    context_dir=task_dir / "context",
                    gold_path=root / "output" / task_id / "gold.csv",
                    config_path=config_path,
                    order_sensitive=bool(config["order_sensitive"]),
                    modalities=tuple(
                        name
                        for name, column in MODALITY_COLUMNS
                        if metadata[task_id][column] == "True"
                    ),
                    question_language=(
                        "zh" if re.search(r"[\u4e00-\u9fff]", question) else "en"
                    ),
                )
            )
        return tasks

    def _write_task(self, task_dir: Path, task: DataSpaceTask) -> None:
        shutil.copytree(TEMPLATE_ROOT, task_dir)

        instruction = (TEMPLATE_ROOT / "instruction.md").read_text(encoding="utf-8")
        instruction = instruction.replace("{task_id}", task.task_id).replace(
            "{question}", task.question
        )
        (task_dir / "instruction.md").write_text(
            instruction.rstrip("\n"), encoding="utf-8"
        )

        keywords = ["data-analysis", "table-qa", task.question_language]
        keywords.extend(task.modalities)
        task_toml = (TEMPLATE_ROOT / "task.toml").read_text(encoding="utf-8")
        task_toml = (
            task_toml.replace("{task_id}", task.task_id)
            .replace("{upstream_code_revision}", UPSTREAM_CODE_REVISION)
            .replace("{upstream_data_revision}", HF_REVISION)
            .replace("{keywords}", json.dumps(keywords))
            .replace("{modalities}", json.dumps(list(task.modalities)))
            .replace("{question_language}", task.question_language)
            .replace("{order_sensitive}", "true" if task.order_sensitive else "false")
        )
        (task_dir / "task.toml").write_text(task_toml, encoding="utf-8")

        shutil.copytree(task.context_dir, task_dir / "environment" / "workspace")

        gold_dir = task_dir / "tests" / "gold" / task.task_id
        gold_dir.mkdir(parents=True)
        shutil.copyfile(task.gold_path, gold_dir / "gold.csv")
        configs_dir = task_dir / "tests" / "configs"
        configs_dir.mkdir()
        shutil.copyfile(task.config_path, configs_dir / f"{task.task_id}.json")
        shutil.copyfile(task.gold_path, task_dir / "solution" / "gold.csv")

        for script in (task_dir / "tests" / "test.sh", task_dir / "solution" / "solve.sh"):
            script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _normalize_task_id(value: str) -> str:
    value = str(value).strip()
    if value.isdigit():
        value = f"task_{value}"
    if not TASK_ID_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid DataSpace task id: {value!r} (expected task_N)")
    return value


def _task_sort_key(task_id: str) -> int:
    return int(task_id.removeprefix("task_"))


def _validate_benchmark_root(root: Path) -> Path:
    root = root.resolve()
    for relative in (
        "input",
        "output",
        "evaluation/configs",
        "metadata/public_reference_tasks.json",
        "metadata/task_metadata.csv",
    ):
        if not (root / relative).exists():
            raise FileNotFoundError(
                f"{root} is not an extracted DataSpace release: missing {relative}"
            )
    return root


def _load_task_metadata(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["task_id"]: row for row in csv.DictReader(handle)}


def _download_archive(cache_dir: Path) -> Path:
    from huggingface_hub import hf_hub_download

    archive = Path(
        hf_hub_download(
            repo_id=HF_REPO_ID,
            filename=ARCHIVE_PATH,
            repo_type="dataset",
            revision=HF_REVISION,
            local_dir=cache_dir,
        )
    )
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError(
            f"SHA-256 mismatch for {archive}: expected {ARCHIVE_SHA256}, "
            f"got {digest.hexdigest()}"
        )
    return archive
