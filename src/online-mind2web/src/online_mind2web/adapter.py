"""Generate Harbor tasks from Online-Mind2Web (https://github.com/OSU-NLP-Group/Online-Mind2Web).

Each upstream task (task_id, confirmed_task, website, reference_length, level) becomes
one Harbor task:

* instruction.md      the task text plus the required starting website
* environment/        Debian + headless Chromium; /app/task.json holds the task metadata
* tests/              WebJudge (upstream code, Online-Mind2Web mode) scoring the
                      agent's v2 trajectory at /app/output/<task_id>/
* solution/solve.sh   placeholder: the benchmark has no oracle solutions
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
from collections import defaultdict
from pathlib import Path

from .run_browser_use import build_task_prompt

HF_REPO_ID = "osunlp/Online-Mind2Web"
HF_FILENAME = "Online_Mind2Web.json"
TEMPLATE_DIR = Path(__file__).parent / "task-template"
PARITY_SEED = 42


def slugify(task_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a task name from {task_id!r}")
    return slug


def load_tasks(dataset_path: Path | None) -> list[dict]:
    """Load the upstream task list from a local JSON file or the (gated) HF dataset."""
    if dataset_path is None:
        from huggingface_hub import hf_hub_download

        dataset_path = Path(
            hf_hub_download(
                repo_id=HF_REPO_ID,
                filename=HF_FILENAME,
                repo_type="dataset",
                token=os.environ.get("HF_TOKEN"),
            )
        )
    tasks = json.loads(Path(dataset_path).read_text())
    required = {"task_id", "confirmed_task", "website", "reference_length", "level"}
    for t in tasks:
        missing = required - t.keys()
        if missing:
            raise ValueError(f"task {t.get('task_id')} is missing fields {sorted(missing)}")
    ids = [t["task_id"] for t in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate task_id in upstream data")
    slugs = [slugify(i) for i in ids]
    if len(set(slugs)) != len(slugs):
        raise ValueError("task_id slugs collide")
    return sorted(tasks, key=lambda t: t["task_id"])


def parity_subset(tasks: list[dict], size: int) -> list[dict]:
    """Deterministic sample stratified by difficulty level (proportional allocation)."""
    if size >= len(tasks):
        return list(tasks)
    by_level: dict[str, list[dict]] = defaultdict(list)
    for t in tasks:  # tasks are already sorted by task_id
        by_level[t["level"]].append(t)
    rng = random.Random(PARITY_SEED)
    levels = sorted(by_level)
    quotas = {lvl: round(size * len(by_level[lvl]) / len(tasks)) for lvl in levels}
    # fix rounding so quotas sum to `size`
    diff = size - sum(quotas.values())
    for lvl in sorted(levels, key=lambda lv: -len(by_level[lv]))[: abs(diff)]:
        quotas[lvl] += 1 if diff > 0 else -1
    picked = []
    for lvl in levels:
        picked.extend(rng.sample(by_level[lvl], quotas[lvl]))
    return sorted(picked, key=lambda t: t["task_id"])


def _render(text: str, values: dict[str, str]) -> str:
    for key, val in values.items():
        text = text.replace("{" + key + "}", val)
    return text


def _toml_str(s: str) -> str:
    return json.dumps(s)[1:-1]  # JSON string escaping is valid TOML basic-string escaping


class OnlineMind2WebAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        dataset_path: Path | None = None,
        split: str = "full",
        parity_size: int = 100,
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.dataset_path = dataset_path
        self.split = split
        self.parity_size = parity_size

    def select_tasks(self) -> list[dict]:
        tasks = load_tasks(self.dataset_path)
        if self.split == "parity":
            tasks = parity_subset(tasks, self.parity_size)
        elif self.split != "full":
            raise ValueError(f"unknown split {self.split!r} (expected 'full' or 'parity')")
        if self.task_ids:
            wanted = set(self.task_ids)
            unknown = wanted - {t["task_id"] for t in tasks}
            if unknown:
                raise ValueError(f"unknown task ids for split {self.split!r}: {sorted(unknown)}")
            tasks = [t for t in tasks if t["task_id"] in wanted]
        if self.limit is not None:
            tasks = tasks[: self.limit]
        return tasks

    def generate_task(self, task: dict) -> Path:
        slug = slugify(task["task_id"])
        task_dir = self.output_dir / slug
        if task_dir.exists():
            if not self.overwrite:
                return task_dir
            shutil.rmtree(task_dir)
        shutil.copytree(TEMPLATE_DIR, task_dir)

        task_meta = {
            "task_id": task["task_id"],
            "confirmed_task": task["confirmed_task"],
            "website": task["website"],
            "reference_length": int(task["reference_length"]),
            "level": task["level"],
        }
        meta_json = json.dumps(task_meta, indent=2, ensure_ascii=False) + "\n"
        (task_dir / "environment" / "task.json").write_text(meta_json)
        (task_dir / "tests" / "task.json").write_text(meta_json)

        (task_dir / "instruction.md").write_text(
            build_task_prompt(task["confirmed_task"], task["website"])
        )

        toml_path = task_dir / "task.toml"
        toml_path.write_text(
            _render(
                toml_path.read_text(),
                {
                    "task_slug": slug,
                    "task_id": _toml_str(task["task_id"]),
                    "website": _toml_str(task["website"]),
                    "difficulty": task["level"],
                    "reference_length": str(int(task["reference_length"])),
                },
            )
        )
        return task_dir

    def run(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        tasks = self.select_tasks()
        for task in tasks:
            self.generate_task(task)
        print(f"Generated {len(tasks)} Online-Mind2Web tasks ({self.split}) in {self.output_dir}")
