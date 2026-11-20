"""Generate Harbor tasks from Workspace-Bench (388 workspace tasks, en and cn).

Each task runs inside one of five persona workspaces (up to 19 GB). The
workspace is fetched from Hugging Face when the image is built, so the
generated task directories stay small. Grading uses upstream's own
agent-as-a-judge (a Claude Code session over the task inputs, the agent's
outputs and its trace), cloned at a pinned commit inside the image.
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import snapshot_download

from .prompt import wrap_prompt

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "task-template"

TASKS_REPO = "Workspace-Bench/Workspace-Bench"
TASKS_REVISION = "3491f9eb611eaf3bd6753048d94e0e049c07ad30"
TASK_DIRS = {"en": "task_clean_en", "cn": "task_clean_cn"}

# Persona (metadata "persona") -> folder inside filesys_<lang>.zip.
PERSONA_DIRS = {
    "Product Manager": "ProductManager_Workdir",
    "Operations Manager": "OperationsManager_Workdir",
    "Logistics Manager": "LogisticsManager_Workdir",
    "Backend Developer": "BackendDeveloper_Workdir",
    "Researcher": "Research_Workdir",
}

# Upstream's downloader (evaluation/scripts/download_hf_assets.py @ 3fbd0f1,
# _normalize_task_meta) adds these fields to every task's metadata before its
# runner and judge read it. On the pinned task data it adds exactly id, job,
# file_system and user_profit (checked on all 776 tasks); language is unchanged.
PERSONA_TO_FILE_SYSTEM = {
    "Product Manager": "产品人员",
    "Backend Developer": "开发人员",
    "Researcher": "研究人员",
    "Operations Manager": "运营人员",
    "Logistics Manager": "行政/后勤人员",
}


def normalize_meta(meta: dict, task_id: str) -> dict:
    """Same additions as upstream's _normalize_task_meta (see PERSONA_TO_FILE_SYSTEM)."""
    meta.setdefault("id", task_id)
    persona = str(meta.get("persona") or meta.get("job") or "").strip()
    file_system = str(meta.get("file_system") or "").strip()
    fs = PERSONA_TO_FILE_SYSTEM.get(persona) or PERSONA_TO_FILE_SYSTEM.get(file_system) or file_system
    if fs:
        meta["file_system"] = fs
        meta.setdefault("user_profit", fs)
    if persona:
        meta.setdefault("job", persona)
    return meta


# Disk each persona workspace needs inside the sandbox (unzipped size plus room
# for outputs and the office tooling).
PERSONA_STORAGE_MB = {
    "Research_Workdir": 40960,
    "BackendDeveloper_Workdir": 16384,
    "LogisticsManager_Workdir": 12288,
    "OperationsManager_Workdir": 12288,
    "ProductManager_Workdir": 12288,
}

# Upstream's Workspace-Bench-Lite: a 100-task subset chosen by the benchmark
# authors (same ids in en and cn). The task metadata is identical to the full
# set apart from data file names. The parity subset is drawn from it (see PARITY_IDS).
LITE_IDS = frozenset(
    int(x)
    for x in """
    3 7 13 15 23 33 35 37 38 44 45 47 53 54 55 72 75 78 79 83 85 87 91 92 94
    95 100 102 107 108 115 116 120 124 127 128 129 131 137 139 143 146 152 154
    158 159 160 161 171 175 178 191 192 207 224 226 227 232 242 244 251 255 258
    266 267 269 274 276 277 278 281 284 286 287 288 289 291 300 306 311 314 328
    329 334 337 340 346 354 357 358 359 360 363 372 373 374 380 381 386 388
    """.split()
)

# Tasks left out of the generated dataset, with the reason: no output built only
# from the task's inputs can pass every rubric point (a rubric contradicts the
# inputs or another rubric), so the oracle cannot reach 100%. Each has a written
# proof; see the README. Source: excluded_tasks.json next to this file.
EXCLUDED_TASKS: dict[str, str] = {
    name: f"{e['reason']} (best honest answer: {e['honest_score']})"
    for name, e in json.loads((Path(__file__).parent / "excluded_tasks.json").read_text(encoding="utf-8")).items()
}

# Parity pool: the 100 Workspace-Bench-Lite tasks in English, with the 10 Lite tasks in
# EXCLUDED_TASKS replaced (same persona and difficulty, an English task outside Lite whose
# oracle was audited as using no rubric-only values and whose rubrics name no file by a
# placeholder the instruction never gives, lowest task number first).
PARITY_REPLACEMENTS = {
    7: 206, 131: 177, 137: 155, 152: 25, 158: 28, 207: 58, 226: 4, 277: 279, 291: 186, 358: 111,
}
PARITY_POOL = frozenset((LITE_IDS - PARITY_REPLACEMENTS.keys()) | set(PARITY_REPLACEMENTS.values()))
assert len(PARITY_POOL) == 100
assert not {f"en-{i:03d}" for i in PARITY_POOL} & EXCLUDED_TASKS.keys()
assert {f"en-{i:03d}" for i in PARITY_REPLACEMENTS} <= EXCLUDED_TASKS.keys()

# Parity split (what the parity experiment ran on): 50 tasks of the pool, after setting aside
# the 13 whose oracle needed values the inputs lack (oracle/disclosure) or whose rubric names
# a file the instruction never gives (en-178). Stratified by persona like the pool
# (Operations 16, Logistics 15, Researcher 9, Backend 5, Product 5), random.Random(20261120),
# chosen before any parity run.
PARITY_IDS = frozenset({3, 13, 25, 37, 44, 47, 54, 55, 72, 75, 78, 79, 87, 91, 100, 107, 108, 111, 120, 124, 139, 143, 146, 155, 171, 175, 177, 186, 191, 192, 227, 242, 244, 255, 266, 276, 278, 279, 284, 287, 288, 311, 314, 328, 329, 334, 357, 363, 373, 386})
assert len(PARITY_IDS) == 50 and PARITY_IDS <= PARITY_POOL


WORK_DIR = "/app"
OUTPUT_DIR = "model_output"


@dataclass
class WorkspaceTask:
    task_id: int
    language: str
    meta: dict
    source_dir: Path

    @property
    def name(self) -> str:
        return f"{self.language}-{self.task_id:03d}"

    @property
    def persona_dir(self) -> str:
        return PERSONA_DIRS[self.meta["persona"]]


class WorkspaceBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        languages: list[str] | None = None,
        split: str = "full",
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        oracle_dir: Path | None = None,
        tasks_root: Path | None = None,
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.languages = languages or ["en", "cn"]
        self.split = split
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = set(task_ids) if task_ids else None
        self.oracle_dir = Path(oracle_dir) if oracle_dir else None
        self.tasks_root = Path(tasks_root) if tasks_root else None

    def _tasks_dir(self, language: str) -> Path:
        """Local folder holding <id>/metadata.json and <id>/data/ for one language."""
        if self.tasks_root is not None:
            return self.tasks_root / TASK_DIRS[language]
        root = snapshot_download(
            repo_id=TASKS_REPO,
            repo_type="dataset",
            revision=TASKS_REVISION,
            allow_patterns=[f"{TASK_DIRS[language]}/**"],
        )
        return Path(root) / TASK_DIRS[language]

    def load_tasks(self) -> list[WorkspaceTask]:
        tasks = []
        for language in self.languages:
            tasks_dir = self._tasks_dir(language)
            found = sorted(
                (p for p in tasks_dir.iterdir() if p.is_dir() and p.name.isdigit()),
                key=lambda p: int(p.name),
            )
            if len(found) != 388:
                raise RuntimeError(f"Expected 388 {language} tasks in {tasks_dir}, found {len(found)}")
            for d in found:
                meta = normalize_meta(json.loads((d / "metadata.json").read_text()), d.name)
                tasks.append(WorkspaceTask(int(d.name), language, meta, d))
        return tasks

    def _selected(self, tasks: list[WorkspaceTask]) -> list[WorkspaceTask]:
        out = []
        for t in tasks:
            if t.name in EXCLUDED_TASKS:
                continue
            if self.split == "parity" and t.task_id not in PARITY_IDS:
                continue
            if self.task_ids is not None and t.name not in self.task_ids:
                continue
            out.append(t)
        if self.split == "parity":
            # Parity runs on English Lite only.
            out = [t for t in out if t.language == "en"]
        if self.limit is not None:
            out = out[: self.limit]
        return out

    @staticmethod
    def _render(path: Path, **values: str) -> None:
        text = path.read_text()
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        path.write_text(text)

    @staticmethod
    def _collect_meta_b64(task: WorkspaceTask) -> str:
        """The metadata fields upstream's collector reads to find expected outputs (no rubrics)."""
        fields = {k: task.meta[k] for k in ("output_files", "output_file", "output_manifests") if k in task.meta}
        return base64.b64encode(json.dumps(fields, ensure_ascii=False).encode("utf-8")).decode("ascii")

    def _oracle_files(self, task: WorkspaceTask) -> Path | None:
        """`<oracle_dir>/<name>/` holds the reference output files for a task."""
        if self.oracle_dir is None:
            return None
        d = self.oracle_dir / task.name
        return d if d.is_dir() and any(d.iterdir()) else None

    def generate_task(self, task: WorkspaceTask) -> None:
        task_dir = self.output_dir / task.name
        if task_dir.exists():
            if not self.overwrite:
                logger.info("Skipping %s (exists; use --overwrite)", task.name)
                return
            shutil.rmtree(task_dir)
        shutil.copytree(TEMPLATE_DIR, task_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        prompt = wrap_prompt(
            prompt=str(task.meta["task"]),
            work_dir=WORK_DIR,
            task_target_output_dir=OUTPUT_DIR,
            language=task.language,
        )
        (task_dir / "instruction.md").write_text(prompt)
        # The verifier puts the prompt at the start of the trace, as upstream does.
        (task_dir / "tests" / "instruction.md").write_text(prompt)

        self._render(
            task_dir / "environment" / "Dockerfile",
            language=task.language,
            persona_dir=task.persona_dir,
        )
        self._render(
            task_dir / "task.toml",
            task_name=task.name,
            language=task.language,
            persona=task.persona_dir.removesuffix("_Workdir").lower(),
            difficulty=str(task.meta.get("task_diff") or "medium"),
            storage_mb=str(PERSONA_STORAGE_MB[task.persona_dir]),
            collect_meta_b64=self._collect_meta_b64(task),
        )

        # Upstream's judge reads <metadata dir>/data as the task inputs.
        judge_task = task_dir / "tests" / "task"
        judge_task.mkdir(parents=True)
        (judge_task / "metadata.json").write_text(
            json.dumps(task.meta, ensure_ascii=False, indent=2) + "\n"
        )
        shutil.copytree(task.source_dir / "data", judge_task / "data")

        oracle = self._oracle_files(task)
        out = task_dir / "solution" / "output"
        if oracle is None:
            logger.warning("No oracle output for %s; solve.sh will fail", task.name)
            out.mkdir(parents=True)
        else:
            shutil.copytree(oracle, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        for script in ("solution/solve.sh", "tests/test.sh"):
            (task_dir / script).chmod(0o755)

    def run(self) -> None:
        selected = self._selected(self.load_tasks())
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for task in selected:
            self.generate_task(task)
        logger.info("Wrote %d tasks to %s", len(selected), self.output_dir)
