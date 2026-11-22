"""TavernBench -> Harbor adapter.

TavernBench (released in https://github.com/MCG-NJU/AutoTavern) is a set of
Hearthstone Battlegrounds decision and rule-execution questions. Every item is a
self-contained prompt that asks for one JSON object, plus a label that says how
the answer is scored.

This adapter writes one Harbor task per item:

* ``instruction.md``  the benchmark's system line and prompt, unchanged, followed
  by one sentence that tells the agent to write the JSON object to
  ``/app/answer.json``;
* ``tests/``          the item's label and a verifier that applies the
  benchmark's own scoring rules;
* ``solution/``       an oracle that writes an answer built from the label.

Only the two JSONL files of the requested split are read. They are pinned by
commit and by SHA-256, so a given adapter revision always generates the same
tasks.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCE_REPO = "MCG-NJU/AutoTavern"
# Tag v1.0.0, the release the upstream README names for reproducing the paper.
SOURCE_COMMIT = "fb61201a557ddd36a411cf44aac51ce4d9b606ce"
SOURCE_DATA_DIR = "tavernbench/data"
RAW_URL = "https://raw.githubusercontent.com/{repo}/{commit}/{path}"

# SHA-256 of the pinned source files. `eval` is the fixed 368-task set every
# system answers in the paper; `full` is all 960 tasks (eval is a subset of it).
SOURCE_SHA256: dict[str, dict[str, str]] = {
    "eval": {
        "prompts.jsonl": "0c9b15d239585a9069b6d8c07bb674db0a0d3e7aa876f84e3452866ff160d590",
        "labels.jsonl": "99447d4fbafd9b3748a0460b02598a4b7244897ab4313a081bfdff0fb579f940",
    },
    "full": {
        "prompts.jsonl": "58caa46d3dd0b186f05688dae40044acd382e5db641790988c4dc4f6a4bfce33",
        "labels.jsonl": "cfb321bbf7ed0538999949abf1a7189bc9a17d3522240ecbe219dd02325464c6",
    },
}
SPLITS = tuple(SOURCE_SHA256)
DEFAULT_SPLIT = "eval"

TASK_NAME_PREFIX = "mcg-nju/tavernbench__"
TEMPLATE_DIR = Path(__file__).parent / "task-template"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "harbor-adapters" / "tavernbench"

# Scoring kinds whose label holds the value of every accepted answer.
_VALUE_MAP_KINDS = ("value_map", "value_map_str", "value_json")
_DIRECT_KINDS = ("exact", "set", "numeric_rel", "numeric_abs")


def sanitize_task_id(source_id: str) -> str:
    """Turn an upstream id such as ``R1-000`` into a registry-safe task id."""
    task_id = re.sub(r"[^a-z0-9]+", "-", source_id.lower()).strip("-")
    if not task_id:
        raise ValueError(f"Cannot derive a task id from {source_id!r}")
    return task_id


def oracle_answer(label: dict[str, Any]) -> dict[str, Any]:
    """Build an answer that the benchmark's scoring rules count as correct.

    The label is the ground truth, so the oracle answer is the label itself.
    For the ``value_*`` kinds the label stores the simulated value of every
    option; the oracle picks the best one.
    """
    kind = label["scoring"]
    if kind in _DIRECT_KINDS:
        return {"answer": label["answer"]}
    if kind == "brier":
        return {key: label["answer"][key] for key in ("win", "tie", "loss")}
    if kind in _VALUE_MAP_KINDS:
        values = label["values"]
        best_key = max(values, key=lambda key: values[key])
        if kind == "value_map_str":
            return {"answer": best_key}
        if kind == "value_json":
            return {"answer": json.loads(best_key)}
        return {
            "answer": [int(part) for part in best_key.split(",")] if best_key else []
        }
    raise ValueError(f"Unknown scoring kind {kind!r} in label {label.get('id')!r}")


@dataclass(frozen=True)
class TavernBenchTask:
    """One TavernBench item: its prompt record and its label."""

    prompt: dict[str, Any]
    label: dict[str, Any]

    @property
    def source_id(self) -> str:
        return self.prompt["id"]

    @property
    def task_id(self) -> str:
        return sanitize_task_id(self.source_id)

    @property
    def task_name(self) -> str:
        return f"{TASK_NAME_PREFIX}{self.task_id}"

    @property
    def identifiers(self) -> set[str]:
        """Every spelling accepted by ``--task-ids``."""
        return {self.source_id, self.source_id.lower(), self.task_id, self.task_name}


class TavernBenchAdapter:
    """Generate Harbor tasks from the pinned TavernBench release."""

    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = DEFAULT_SPLIT,
        source_dir: Path | None = None,
        cache_dir: Path | None = None,
        **kwargs: Any,
    ):
        """
        Args:
            output_dir: Directory the task folders are written to.
            limit: Generate only the first N selected tasks.
            overwrite: Replace task folders that already exist.
            task_ids: Only generate these tasks (upstream ids such as ``R1-000``,
                task ids such as ``r1-000``, or full task names).
            split: ``eval`` (368 tasks, the paper's evaluation set) or ``full``
                (all 960 tasks).
            source_dir: Root of a local AutoTavern checkout. When omitted, the
                pinned files are downloaded from GitHub and cached.
            cache_dir: Where downloaded source files are cached.
        """
        if split not in SOURCE_SHA256:
            raise ValueError(f"Unknown split {split!r}; choose one of {SPLITS}")
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = list(task_ids) if task_ids else None
        self.split = split
        self.source_dir = Path(source_dir) if source_dir else None
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR

    # ------------------------------------------------------------------ source

    def _source_file(self, name: str) -> Path:
        """Return a verified local copy of one pinned source file."""
        relative = f"{SOURCE_DATA_DIR}/{self.split}/{name}"
        expected = SOURCE_SHA256[self.split][name]

        if self.source_dir is not None:
            path = self.source_dir / relative
            if not path.is_file():
                raise FileNotFoundError(
                    f"{path} not found; --source-dir must be the root of an AutoTavern checkout"
                )
            self._verify(path.read_bytes(), expected, str(path))
            return path

        path = self.cache_dir / SOURCE_COMMIT / self.split / name
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
            return path

        url = RAW_URL.format(repo=SOURCE_REPO, commit=SOURCE_COMMIT, path=relative)
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                data = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(
                f"Could not download {url}: {exc}. Clone https://github.com/{SOURCE_REPO} "
                f"at {SOURCE_COMMIT} and pass its root with --source-dir instead."
            ) from exc
        self._verify(data, expected, url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    @staticmethod
    def _verify(data: bytes, expected: str, origin: str) -> None:
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise ValueError(
                f"{origin} does not match the pinned TavernBench release "
                f"({SOURCE_REPO}@{SOURCE_COMMIT}): sha256 {actual}, expected {expected}"
            )

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        with path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    # ------------------------------------------------------------------- tasks

    def load_tasks(self) -> list[TavernBenchTask]:
        """Load the split in upstream order and apply ``task_ids`` and ``limit``."""
        prompts = self._read_jsonl(self._source_file("prompts.jsonl"))
        labels = {
            label["id"]: label
            for label in self._read_jsonl(self._source_file("labels.jsonl"))
        }

        tasks: list[TavernBenchTask] = []
        seen: set[str] = set()
        for prompt in prompts:
            source_id = prompt["id"]
            if source_id not in labels:
                raise ValueError(f"No label for prompt {source_id!r}")
            task = TavernBenchTask(prompt=prompt, label=labels[source_id])
            if task.task_id in seen:
                raise ValueError(f"Duplicate task id {task.task_id!r}")
            seen.add(task.task_id)
            tasks.append(task)

        if self.task_ids is not None:
            wanted = set(self.task_ids)
            known = set().union(*(task.identifiers for task in tasks))
            unknown = sorted(wanted - known)
            if unknown:
                raise ValueError(
                    f"Unknown task ids for split {self.split!r}: {', '.join(unknown)}"
                )
            tasks = [task for task in tasks if task.identifiers & wanted]

        if self.limit is not None:
            tasks = tasks[: max(0, self.limit)]
        return tasks

    def run(self) -> list[TavernBenchTask]:
        """Write the selected tasks under ``self.output_dir`` and return them."""
        tasks = self.load_tasks()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        written = skipped = 0
        for task in tasks:
            task_dir = self.output_dir / task.task_id
            if task_dir.exists():
                if not self.overwrite:
                    skipped += 1
                    continue
                shutil.rmtree(task_dir)
            self._write_task(task, task_dir)
            written += 1

        print(
            f"TavernBench ({self.split}): wrote {written} task(s), skipped {skipped} existing, "
            f"{len(tasks)} selected -> {self.output_dir}"
        )
        return tasks

    def _write_task(self, task: TavernBenchTask, task_dir: Path) -> None:
        shutil.copytree(TEMPLATE_DIR, task_dir)

        self._render(
            task_dir / "task.toml",
            {
                "task_name": task.task_name,
                "source_id": task.source_id,
                "scenario": task.prompt["scenario"],
                "category": task.prompt["category"],
                "scoring": task.label["scoring"],
                "source_commit": SOURCE_COMMIT,
            },
        )
        self._render(
            task_dir / "instruction.md",
            {
                "system_prompt": task.prompt["system"].strip(),
                "prompt": task.prompt["prompt"].strip(),
            },
        )
        self._render(
            task_dir / "solution" / "solve.sh",
            {
                "oracle_answer": json.dumps(
                    oracle_answer(task.label), ensure_ascii=False
                )
            },
        )
        (task_dir / "tests" / "label.json").write_text(
            json.dumps(task.label, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for script in (
            task_dir / "solution" / "solve.sh",
            task_dir / "tests" / "test.sh",
        ):
            script.chmod(0o755)

    @staticmethod
    def _render(path: Path, values: dict[str, str]) -> None:
        """Fill ``{name}`` placeholders in one pass.

        Prompts contain braces (the answer format is JSON), so ``str.format``
        cannot be used, and a single pass keeps text inserted for one
        placeholder from being read as another.
        """
        template = path.read_text(encoding="utf-8")
        pattern = re.compile("|".join(re.escape("{" + name + "}") for name in values))
        path.write_text(
            pattern.sub(lambda match: values[match.group(0)[1:-1]], template),
            encoding="utf-8",
        )
