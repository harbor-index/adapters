"""MBABench 2.0 adapter: converts the 101 public MBABench tasks into Harbor tasks.

Source:  https://github.com/namkoong-lab/MBABench-2.0
Dataset: https://huggingface.co/datasets/namkoong-lab/MBABench-2.0
Paper:   https://arxiv.org/abs/2610.31711

Each generated task reproduces one attempt of the upstream *coding-agent*
pipeline (``coding-agents/``, prompt template v13, prompt_version 113):

* The agent works in ``/workspace``, seeded exactly as upstream's
  ``workspace.create_attempt`` + ``prompt_builder.build_prompt`` seed it:
  ``PROMPT.md``, ``HOUSE_STANDARDS.md`` and ``starting_files/<workbook>.xlsx``.
* ``instruction.md`` is byte-identical to upstream's ``PROMPT.md``.
* The verifier runs in a separate container with the official judge (cloned at
  ``UPSTREAM_COMMIT``) and grades ``/workspace/solution.xlsx`` through upstream's
  own ``judge/main_scripts/grade.py``; the reward is the judge's total / 100.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ADAPTER_NAME = "mbabench-2-0"
TASK_ORG = "namkoong-lab"

HF_REPO_ID = "namkoong-lab/MBABench-2.0"
# Dataset v1.1 (2026-10-26): workbooks of tasks 8, 13, 18, 21, 23, 43, 47, 67,
# 78 and 88 updated to remove case ambiguity.
HF_REVISION = "85fa36690ad45f54ecc12f330009b44ca1e31b71"

# Upstream code the vendored prompts come from and the verifier image clones.
UPSTREAM_REPO = "https://github.com/namkoong-lab/MBABench-2.0"
UPSTREAM_COMMIT = "d3fc95f7964260759632d2a1a9d729092f26c9bb"

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "task-template"
PROMPTS_DIR = PACKAGE_DIR / "prompts"

# Byte-exact copies of coding-agents/coding_agent/prompts/* at UPSTREAM_COMMIT.
# The v13 pin is the same md5 upstream's prompt_builder.SCRUBBED_MD5 enforces.
SYSTEM_PROMPT_FILE = "system_prompt_coding_v1.txt"
TASK_TEMPLATE_FILE = "task_template_shared_v13.txt"
HOUSE_STANDARDS_FILE = "house_standards_v1.md"
PROMPT_MD5 = {
    SYSTEM_PROMPT_FILE: "ff01b0c7e3534d9fd8f14c4f65d35ef0",
    TASK_TEMPLATE_FILE: "008713b35953c2d1356dd81e7f51d4e3",
    HOUSE_STANDARDS_FILE: "29653a2b2059be3a5fa79884f49c6dc3",
}
# Upstream's attempt-row prompt_version: system v1 * 100 + template v13.
PROMPT_VERSION = 113
# Name the house standards are staged under in the workspace root (v13).
HOUSE_STANDARDS_WORKSPACE_NAME = "HOUSE_STANDARDS.md"

# Parity subset (--split parity): 30 tasks stratified by upstream difficulty in
# proportion to the full set (Easy 3, Medium 12, Medium-Hard 9, Hard 6), drawn
# with random.Random(2026).sample over each stratum's sorted task ids.
# fmt: off
PARITY_TASK_IDS: tuple[int, ...] = (
    1, 3, 6, 10, 11, 18, 28, 30, 31, 33, 38, 39, 41, 46, 50,
    52, 58, 59, 62, 69, 72, 73, 75, 79, 82, 84, 87, 88, 94, 98,
)
# fmt: on
SPLITS = ("full", "parity")

# Harbor's metadata.difficulty vocabulary; the upstream rating is kept verbatim
# in metadata.upstream_difficulty.
DIFFICULTY_MAP = {
    "Easy": "easy",
    "Medium": "medium",
    "Medium-Hard": "hard",
    "Hard": "hard",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _toml_string(value: str) -> str:
    """A TOML basic string (JSON string escapes are valid TOML escapes)."""
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class MBABenchTask:
    """One upstream task: its ``task.json`` row plus resolved file paths."""

    row: dict
    task_dir: Path
    starting_file: Path
    solution_file: Path

    @property
    def task_id(self) -> int:
        return int(self.row["task_id"])

    @property
    def local_task_id(self) -> str:
        return f"{ADAPTER_NAME}-task-{self.task_id}"

    @classmethod
    def from_task_dir(cls, dataset_dir: Path, task_dir: Path) -> "MBABenchTask":
        row = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        starting, solution = row["starting_files"], row["solution_files"]
        if len(starting) != 1 or len(solution) != 1:
            # Every released task has exactly one of each; the judge and the
            # oracle below rely on it, so refuse anything else loudly.
            raise ValueError(
                f"task {row['task_id']}: expected one starting and one solution "
                f"workbook, got {starting} / {solution}"
            )
        return cls(
            row=row,
            task_dir=task_dir,
            starting_file=dataset_dir / starting[0],
            solution_file=dataset_dir / solution[0],
        )


@dataclass(frozen=True)
class PromptMaterial:
    system_prompt: str
    task_template: str
    house_standards: Path

    @classmethod
    def load(cls) -> "PromptMaterial":
        for name, expected in PROMPT_MD5.items():
            actual = hashlib.md5((PROMPTS_DIR / name).read_bytes()).hexdigest()
            if actual != expected:
                raise RuntimeError(
                    f"prompts/{name} no longer matches upstream (md5 {actual} != "
                    f"{expected}); it must stay a byte-exact copy"
                )
        return cls(
            system_prompt=(PROMPTS_DIR / SYSTEM_PROMPT_FILE).read_text(
                encoding="utf-8"
            ),
            task_template=(PROMPTS_DIR / TASK_TEMPLATE_FILE).read_text(
                encoding="utf-8"
            ),
            house_standards=PROMPTS_DIR / HOUSE_STANDARDS_FILE,
        )


class MBABench20Adapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "full",
        dataset_dir: Path | None = None,
        **kwargs,
    ):
        if split not in SPLITS:
            raise ValueError(f"unknown split {split!r}; choose from {SPLITS}")
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = [int(t) for t in task_ids] if task_ids else None
        self.split = split
        self.dataset_dir = Path(dataset_dir) if dataset_dir else None

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        dataset_dir = self._resolve_dataset_dir()
        tasks = self._select_tasks(self._load_tasks(dataset_dir))
        self._verify_manifest(dataset_dir, tasks)
        prompts = PromptMaterial.load()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated = 0
        for task in tasks:
            if self._generate_task(task, prompts):
                generated += 1
        logger.info(
            "Generated %d of %d selected tasks into %s",
            generated,
            len(tasks),
            self.output_dir,
        )

    # -------------------------------------------------------------- dataset

    def _resolve_dataset_dir(self) -> Path:
        if self.dataset_dir is not None:
            if not (self.dataset_dir / "tasks").is_dir():
                raise FileNotFoundError(
                    f"{self.dataset_dir} is not an MBABench dataset (no tasks/ directory)"
                )
            return self.dataset_dir

        from huggingface_hub import snapshot_download

        logger.info("Downloading %s@%s from Hugging Face", HF_REPO_ID, HF_REVISION[:12])
        return Path(
            snapshot_download(
                repo_id=HF_REPO_ID, repo_type="dataset", revision=HF_REVISION
            )
        )

    @staticmethod
    def _load_tasks(dataset_dir: Path) -> list[MBABenchTask]:
        tasks = [
            MBABenchTask.from_task_dir(dataset_dir, task_json.parent)
            for task_json in (dataset_dir / "tasks").glob("task_id=*/task.json")
        ]
        return sorted(tasks, key=lambda t: t.task_id)

    def _select_tasks(self, tasks: list[MBABenchTask]) -> list[MBABenchTask]:
        if self.split == "parity":
            tasks = [t for t in tasks if t.task_id in PARITY_TASK_IDS]
        if self.task_ids is not None:
            known = {t.task_id for t in tasks}
            unknown = sorted(set(self.task_ids) - known)
            if unknown:
                raise ValueError(
                    f"unknown task id(s) for split {self.split!r}: {unknown}"
                )
            wanted = set(self.task_ids)
            tasks = [t for t in tasks if t.task_id in wanted]
        if self.limit is not None:
            tasks = tasks[: self.limit]
        return tasks

    @staticmethod
    def _verify_manifest(dataset_dir: Path, tasks: list[MBABenchTask]) -> None:
        """Check the selected tasks' files against the dataset's MANIFEST.json
        (size + sha256), as upstream's scripts/download_dataset.py does."""
        manifest_path = dataset_dir / "MANIFEST.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"{manifest_path} not found; cannot verify the dataset"
            )
        entries = {e["path"]: e for e in json.loads(manifest_path.read_text())["files"]}
        problems = []
        for task in tasks:
            prefix = f"{task.task_dir.relative_to(dataset_dir).as_posix()}/"
            expected = {rel for rel in entries if rel.startswith(prefix)}
            on_disk = {
                p.relative_to(dataset_dir).as_posix()
                for p in task.task_dir.rglob("*")
                if p.is_file()
            }
            problems += [f"missing: {rel}" for rel in sorted(expected - on_disk)]
            problems += [
                f"not in manifest: {rel}" for rel in sorted(on_disk - expected)
            ]
            for rel in sorted(expected & on_disk):
                path, entry = dataset_dir / rel, entries[rel]
                if (
                    path.stat().st_size != entry["size"]
                    or sha256_file(path) != entry["sha256"]
                ):
                    problems.append(f"differs: {rel}")
        if problems:
            raise RuntimeError(f"dataset does not match MANIFEST.json: {problems}")

    # ------------------------------------------------------------ generation

    def _generate_task(self, task: MBABenchTask, prompts: PromptMaterial) -> bool:
        task_out = self.output_dir / task.local_task_id
        if task_out.exists():
            if not self.overwrite:
                logger.info("Skipping %s (exists; pass --overwrite)", task_out.name)
                return False
            shutil.rmtree(task_out)
        task_out.mkdir(parents=True)

        instruction = self._write_environment(task, prompts, task_out / "environment")
        (task_out / "instruction.md").write_text(instruction, encoding="utf-8")
        self._write_task_toml(task, task_out / "task.toml")
        self._write_solution(task, task_out / "solution")
        self._write_tests(task, prompts, task_out / "tests")
        return True

    def _write_environment(
        self, task: MBABenchTask, prompts: PromptMaterial, env_dir: Path
    ) -> str:
        """Seed environment/workspace/ (copied to /workspace) and return the
        prompt, which is also the task's instruction.md."""
        workspace = env_dir / "workspace"
        (workspace / "starting_files").mkdir(parents=True)
        shutil.copy2(
            TEMPLATE_DIR / "environment" / "Dockerfile", env_dir / "Dockerfile"
        )

        starting = workspace / "starting_files" / task.starting_file.name
        shutil.copy2(task.starting_file, starting)
        house_standards = workspace / HOUSE_STANDARDS_WORKSPACE_NAME
        shutil.copy2(prompts.house_standards, house_standards)

        prompt = self._render_prompt(prompts, starting.parent, house_standards)
        (workspace / "PROMPT.md").write_text(prompt, encoding="utf-8")
        return prompt

    @staticmethod
    def _render_prompt(
        prompts: PromptMaterial, starting_dir: Path, house_standards: Path
    ) -> str:
        """Mirror of upstream prompt_builder.build_prompt (template v13)."""
        listing = "\n".join(
            f"- starting_files/{p.name} ({p.stat().st_size:,} bytes)"
            for p in sorted(starting_dir.iterdir())
        )
        listing += (
            f"\n- {house_standards.name} ({house_standards.stat().st_size:,} bytes)"
        )
        template = (TEMPLATE_DIR / "instruction.md").read_text(encoding="utf-8")
        return (
            template.replace("{system_prompt}", prompts.system_prompt)
            .replace("{task_template}", prompts.task_template)
            .replace("{workspace_listing}", listing)
        )

    @staticmethod
    def _write_task_toml(task: MBABenchTask, path: Path) -> None:
        row = task.row
        replacements = {
            "{local_task_id}": task.local_task_id,
            "{task_org}": TASK_ORG,
            "{description}": _toml_string(
                f"MBABench 2.0 task {task.task_id} ({row['task_name']}): {row['description']}"
            ),
            "{difficulty}": DIFFICULTY_MAP[row["difficulty"]],
            "{upstream_difficulty}": _toml_string(row["difficulty"]),
            "{difficulty_score}": repr(float(row["difficulty_score"])),
            "{model_type}": _toml_string(row["model_type"]),
            "{task_id}": str(task.task_id),
            "{task_name}": _toml_string(row["task_name"]),
            "{estimated_solve_time_hours}": repr(
                float(row["estimated_solve_time_hours"])
            ),
        }
        text = (TEMPLATE_DIR / "task.toml").read_text(encoding="utf-8")
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def _write_solution(task: MBABenchTask, solution_dir: Path) -> None:
        solution_dir.mkdir()
        solve = solution_dir / "solve.sh"
        shutil.copy2(TEMPLATE_DIR / "solution" / "solve.sh", solve)
        solve.chmod(0o755)
        shutil.copy2(task.solution_file, solution_dir / "golden.xlsx")

    @staticmethod
    def _write_tests(
        task: MBABenchTask, prompts: PromptMaterial, tests_dir: Path
    ) -> None:
        """tests/ is the separate verifier's build context: the judge's data
        root for this one task plus the grading driver."""
        template_tests = TEMPLATE_DIR / "tests"
        shutil.copytree(template_tests, tests_dir)
        (tests_dir / "test.sh").chmod(0o755)

        dockerfile = tests_dir / "Dockerfile"
        dockerfile.write_text(
            dockerfile.read_text(encoding="utf-8")
            .replace("{upstream_repo}", UPSTREAM_REPO)
            .replace("{upstream_commit}", UPSTREAM_COMMIT),
            encoding="utf-8",
        )

        # <data_root>/tasks/task_id=<N>/ exactly as the downloaded benchmark
        # lays it out (utils/local_store.py), restricted to this task.
        data_task_dir = tests_dir / "data" / "tasks" / task.task_dir.name
        shutil.copytree(task.task_dir, data_task_dir)

        seeded = {
            f"starting_files/{task.starting_file.name}": sha256_file(
                task.starting_file
            ),
            HOUSE_STANDARDS_WORKSPACE_NAME: sha256_file(prompts.house_standards),
        }
        config = {
            "task_id": task.task_id,
            "task_name": task.row["task_name"],
            "prompt_version": PROMPT_VERSION,
            "seeded_sha256": seeded,
            "golden_sha256": sha256_file(task.solution_file),
        }
        (tests_dir / "task_config.json").write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )
