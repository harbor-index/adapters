"""Generate Harbor tasks from the public CMPhysBench release.

Source data: ``CMPhysBench.json`` on Hugging Face (520 problems). Only the 100
problems whose ``final_answer`` is non-empty are public with a reference answer;
the other 420 cannot be graded. Two of the 100 (ids 55, 74) have reference
answers containing ``\\int`` / ``\\sum``, for which upstream SEED always returns
0, so even the reference answer cannot score. Two more (ids 56, 89) state the
reference answer verbatim in the question. These four are excluded (96 tasks).

Scoring code: the upstream SEED package (``SEED/SEED.py``, ``SEED/extended_zss.py``,
``SEED/latex_pre_process.py``, ``SEED/__init__.py``) from CMPhysBench/CMPhysBench,
downloaded at a pinned commit and copied verbatim into each task's ``tests/SEED/``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "task-template"

HF_DATASET = "weidawang/CMPhysBench"
HF_REVISION = "43d185851f731e23aa5737c3667b3a9e87bf8cd1"
HF_DATA_FILE = "CMPhysBench.json"
DATA_URL = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/{HF_REVISION}/{HF_DATA_FILE}"

SEED_REPO = "CMPhysBench/CMPhysBench"
SEED_COMMIT = "b2cd8571279450f0861759f47d98e9fc577aa993"
SEED_FILES = ("SEED/__init__.py", "SEED/SEED.py", "SEED/extended_zss.py", "SEED/latex_pre_process.py", "LICENSE")
SEED_URL = f"https://raw.githubusercontent.com/{SEED_REPO}/{SEED_COMMIT}/{{path}}"

TASK_PREFIX = "cmphysbench"
HEREDOC_DELIMITER = "__CMPHYSBENCH_REFERENCE_ANSWER__"
UNSCORABLE_MARKERS = ("\\int", "\\sum")  # upstream SEED returns 0 for these
# Problems whose statement contains the reference answer verbatim (the question
# defines the expression it then asks for), so the task would be trivial.
ANSWER_IN_QUESTION_IDS = {"56", "89"}


@dataclass(frozen=True)
class CMPhysBenchProblem:
    source_id: str
    topic: str
    answer_type: str
    question: str
    symbol: str
    answer: str

    @property
    def task_id(self) -> str:
        return make_task_id(self.source_id)

    @property
    def prompt(self) -> str:
        """User message exactly as built by upstream evaluation.py."""
        return self.question + "\n" + "Here are the relevant symbols:\n" + self.symbol


def make_task_id(source_id: str) -> str:
    """Registry-safe, stable task id, e.g. '7' -> 'cmphysbench-7'."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(source_id).strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"Cannot derive a task id from {source_id!r}")
    return f"{TASK_PREFIX}-{slug}"


def _download(url: str, dest: Path) -> None:
    logger.info("Downloading %s", url)
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            dest.write_bytes(response.read())
    except OSError as exc:
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc


class CMPhysBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        data_file: Path | None = None,
        seed_repo_dir: Path | None = None,
    ):
        """
        Args:
            output_dir: Directory to write generated tasks to.
            limit: Generate only the first N selected tasks (sorted by source id).
            overwrite: Replace existing task directories.
            task_ids: Only generate these tasks ('7' or 'cmphysbench-7').
            data_file: Local copy of CMPhysBench.json (skips download).
            seed_repo_dir: Local checkout of CMPhysBench/CMPhysBench at the pinned
                commit (skips download of the SEED scorer).
        """
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.data_file = Path(data_file) if data_file else None
        self.seed_repo_dir = Path(seed_repo_dir) if seed_repo_dir else None
        self.missing_task_ids: list[str] = []
        self.failed_task_ids: list[str] = []

    # ------------------------------------------------------------------ inputs
    def _load_problems(self, data_path: Path) -> list[CMPhysBenchProblem]:
        records = json.loads(data_path.read_text(encoding="utf-8"))
        problems: list[CMPhysBenchProblem] = []
        no_answer = unscorable = 0
        for record in records:
            if "id" not in record:
                raise ValueError("Source record without an id")
            answers = record.get("final_answer") or []
            answer = str(answers[0]) if answers else ""
            if not answer.strip():
                no_answer += 1
                continue
            if any(m in answer for m in UNSCORABLE_MARKERS):
                unscorable += 1
                logger.info("Excluding id %s: reference contains \\int or \\sum (upstream SEED scores 0)", record["id"])
                continue
            if str(record["id"]) in ANSWER_IN_QUESTION_IDS:
                unscorable += 1
                logger.info("Excluding id %s: the question states the reference answer verbatim", record["id"])
                continue
            if HEREDOC_DELIMITER in answer:
                raise ValueError(f"Answer of record {record['id']} contains the heredoc delimiter")
            problems.append(
                CMPhysBenchProblem(
                    source_id=str(record["id"]),
                    topic=str(record.get("topic") or "Unknown"),
                    answer_type=str(record.get("answer_type") or "Expression"),
                    question=str(record.get("question") or ""),
                    symbol=str(record.get("symbol") or ""),
                    answer=answer,
                )
            )
        logger.info(
            "CMPhysBench: %d records, %d without public answer, %d excluded, %d usable",
            len(records), no_answer, unscorable, len(problems),
        )
        ids = [p.task_id for p in problems]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate task ids derived from the source data")
        return sorted(problems, key=lambda p: (len(p.source_id), p.source_id))

    def _select(self, problems: list[CMPhysBenchProblem]) -> list[CMPhysBenchProblem]:
        selected = problems
        if self.task_ids:
            wanted = {make_task_id(t.lower().removeprefix(f"{TASK_PREFIX}-")) for t in self.task_ids}
            selected = [p for p in problems if p.task_id in wanted]
            found = {p.task_id for p in selected}
            self.missing_task_ids = sorted(wanted - found)
            for missing in self.missing_task_ids:
                logger.error("Unknown or excluded task id: %s", missing)
        if self.limit is not None:
            selected = selected[: max(0, self.limit)]
        return selected

    def _fetch_inputs(self, work_dir: Path) -> tuple[Path, Path]:
        data_path = self.data_file
        if data_path is None:
            data_path = work_dir / HF_DATA_FILE
            _download(DATA_URL, data_path)
        seed_dir = work_dir / "SEED"
        seed_dir.mkdir(parents=True, exist_ok=True)
        for rel in SEED_FILES:
            dest = seed_dir / Path(rel).name
            if self.seed_repo_dir is not None:
                shutil.copy2(self.seed_repo_dir / rel, dest)
            else:
                _download(SEED_URL.format(path=rel), dest)
        return data_path, seed_dir

    # ------------------------------------------------------------------ output
    @staticmethod
    def _render(template_rel: str, replacements: dict[str, str]) -> str:
        text = (TEMPLATE_DIR / template_rel).read_text(encoding="utf-8")
        for key, value in replacements.items():
            text = text.replace("{" + key + "}", value)
        return text

    def _write_task(self, problem: CMPhysBenchProblem, seed_dir: Path) -> None:
        task_dir = self.output_dir / problem.task_id
        tmp_dir = self.output_dir / f".{problem.task_id}.tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        (tmp_dir / "environment").mkdir(parents=True)
        (tmp_dir / "solution").mkdir()
        (tmp_dir / "tests" / "SEED").mkdir(parents=True)

        (tmp_dir / "instruction.md").write_text(
            self._render("instruction.md", {"problem": problem.prompt}), encoding="utf-8"
        )
        (tmp_dir / "task.toml").write_text(
            self._render(
                "task.toml",
                {
                    "task_id": problem.task_id,
                    "topic": problem.topic,
                    "topic_slug": re.sub(r"[^a-z0-9]+", "-", problem.topic.lower()).strip("-"),
                    "answer_type": problem.answer_type,
                },
            ),
            encoding="utf-8",
        )
        shutil.copy2(TEMPLATE_DIR / "environment" / "Dockerfile", tmp_dir / "environment" / "Dockerfile")

        solve = tmp_dir / "solution" / "solve.sh"
        solve.write_text(self._render("solution/solve.sh", {"answer": problem.answer}), encoding="utf-8")
        solve.chmod(0o755)

        tests = tmp_dir / "tests"
        for name in ("test.sh", "score.py", "cmphysbench_answer.py"):
            shutil.copy2(TEMPLATE_DIR / "tests" / name, tests / name)
        (tests / "test.sh").chmod(0o755)
        (tests / "ground_truth.json").write_text(
            json.dumps({"answer": problem.answer, "answer_type": problem.answer_type}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for src in seed_dir.iterdir():
            shutil.copy2(src, tests / "SEED" / src.name)

        if task_dir.exists():
            shutil.rmtree(task_dir)
        tmp_dir.rename(task_dir)

    def run(self) -> None:
        """Generate one Harbor task per selected CMPhysBench problem under self.output_dir."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cmphysbench-adapter-") as tmp:
            data_path, seed_dir = self._fetch_inputs(Path(tmp))
            problems = self._load_problems(data_path)
            selected = self._select(problems)
            generated = skipped = 0
            for problem in selected:
                if (self.output_dir / problem.task_id).exists() and not self.overwrite:
                    skipped += 1
                    continue
                try:
                    self._write_task(problem, seed_dir)
                    generated += 1
                except (OSError, ValueError) as exc:
                    logger.error("Failed to generate %s: %s", problem.task_id, exc)
                    shutil.rmtree(self.output_dir / f".{problem.task_id}.tmp", ignore_errors=True)
                    self.failed_task_ids.append(problem.task_id)
        logger.info(
            "CMPhysBench: %d usable problems, %d selected, %d generated, %d skipped (exists), %d failed -> %s",
            len(problems), len(selected), generated, skipped, len(self.failed_task_ids), self.output_dir,
        )
