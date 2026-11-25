"""Generate Harbor tasks from the public PHYBench release.

Source data: the 100 fully released PHYBench problems (question, reference
solution, reference answer) in ``PHYBench-fullques_v1.json`` on Hugging Face.
The other 400 released questions have no public answers and cannot be graded.

Scoring code: the upstream EED scorer (``EED/EED.py``, ``EED/extended_zss.py``,
``EED/latex_pre_process.py``) from phybench-official/phybench, downloaded at a
pinned commit and copied verbatim into each task's ``tests/eed/``.
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

HF_DATASET = "Eureka-Lab/PHYBench"
HF_REVISION = "d6d91c787b7abb865eb2490a328bf85a9f5095f0"
HF_DATA_FILE = "PHYBench-fullques_v1.json"
DATA_URL = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/{HF_REVISION}/{HF_DATA_FILE}"

EED_REPO = "phybench-official/phybench"
EED_COMMIT = "d9db3ec7246f3678aaee65d44a32649ad93beea2"
EED_FILES = ("EED/EED.py", "EED/extended_zss.py", "EED/latex_pre_process.py", "LICENSE")
EED_URL = f"https://raw.githubusercontent.com/{EED_REPO}/{EED_COMMIT}/{{path}}"

TASK_PREFIX = "phybench"
HEREDOC_DELIMITER = "__PHYBENCH_REFERENCE_ANSWER__"


@dataclass(frozen=True)
class PHYBenchProblem:
    source_id: str
    tag: str
    content: str
    answer: str

    @property
    def task_id(self) -> str:
        return make_task_id(self.source_id)


def make_task_id(source_id: str) -> str:
    """Registry-safe, stable task id, e.g. '495' -> 'phybench-495'."""
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


class PHYBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        data_file: Path | None = None,
        eed_dir: Path | None = None,
    ):
        """
        Args:
            output_dir: Directory to write generated tasks to.
            limit: Generate only the first N selected tasks (sorted by source id).
            overwrite: Replace existing task directories.
            task_ids: Only generate these tasks ('495' or 'phybench-495').
            data_file: Local copy of PHYBench-fullques_v1.json (skips download).
            eed_dir: Local checkout of phybench-official/phybench at the pinned
                commit (skips download of the EED scorer).
        """
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.data_file = Path(data_file) if data_file else None
        self.eed_dir = Path(eed_dir) if eed_dir else None
        self.missing_task_ids: list[str] = []
        self.failed_task_ids: list[str] = []

    # ------------------------------------------------------------------ inputs
    def _load_problems(self, data_path: Path) -> list[PHYBenchProblem]:
        records = json.loads(data_path.read_text(encoding="utf-8"))
        problems: list[PHYBenchProblem] = []
        for record in records:
            if "id" not in record:
                raise ValueError("Source record without an id")
            answer = str(record.get("answer") or "")
            content = str(record.get("content") or "")
            if not answer.strip() or not content.strip():
                logger.warning("Skipping record %s: missing question or answer", record.get("id"))
                continue
            if HEREDOC_DELIMITER in answer:
                raise ValueError(f"Answer of record {record['id']} contains the heredoc delimiter")
            problems.append(
                PHYBenchProblem(
                    source_id=str(record["id"]),
                    tag=str(record.get("tag") or "UNKNOWN"),
                    content=content,
                    answer=answer,
                )
            )
        ids = [p.task_id for p in problems]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate task ids derived from the source data")
        return sorted(problems, key=lambda p: (len(p.source_id), p.source_id))

    def _select(self, problems: list[PHYBenchProblem]) -> list[PHYBenchProblem]:
        selected = problems
        if self.task_ids:
            wanted = {make_task_id(t.lower().removeprefix(f"{TASK_PREFIX}-")) for t in self.task_ids}
            selected = [p for p in problems if p.task_id in wanted]
            found = {p.task_id for p in selected}
            self.missing_task_ids = sorted(wanted - found)
            for missing in self.missing_task_ids:
                logger.error("Unknown task id: %s", missing)
        if self.limit is not None:
            selected = selected[: max(0, self.limit)]
        return selected

    def _fetch_inputs(self, work_dir: Path) -> tuple[Path, Path]:
        data_path = self.data_file
        if data_path is None:
            data_path = work_dir / HF_DATA_FILE
            _download(DATA_URL, data_path)
        eed_dir = work_dir / "eed"
        eed_dir.mkdir(parents=True, exist_ok=True)
        for rel in EED_FILES:
            dest = eed_dir / Path(rel).name
            if self.eed_dir is not None:
                shutil.copy2(self.eed_dir / rel, dest)
            else:
                _download(EED_URL.format(path=rel), dest)
        return data_path, eed_dir

    # ------------------------------------------------------------------ output
    @staticmethod
    def _render(template_rel: str, replacements: dict[str, str]) -> str:
        text = (TEMPLATE_DIR / template_rel).read_text(encoding="utf-8")
        for key, value in replacements.items():
            text = text.replace("{" + key + "}", value)
        return text

    def _write_task(self, problem: PHYBenchProblem, eed_dir: Path) -> None:
        task_dir = self.output_dir / problem.task_id
        tmp_dir = self.output_dir / f".{problem.task_id}.tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        (tmp_dir / "environment").mkdir(parents=True)
        (tmp_dir / "solution").mkdir()
        (tmp_dir / "tests" / "eed").mkdir(parents=True)

        (tmp_dir / "instruction.md").write_text(
            self._render("instruction.md", {"problem": problem.content.strip()}),
            encoding="utf-8",
        )
        (tmp_dir / "task.toml").write_text(
            self._render(
                "task.toml",
                {
                    "task_id": problem.task_id,
                    "tag": problem.tag,
                    "tag_lower": problem.tag.lower(),
                },
            ),
            encoding="utf-8",
        )
        shutil.copy2(TEMPLATE_DIR / "environment" / "Dockerfile", tmp_dir / "environment" / "Dockerfile")

        solve = tmp_dir / "solution" / "solve.sh"
        solve.write_text(self._render("solution/solve.sh", {"answer": problem.answer}), encoding="utf-8")
        solve.chmod(0o755)

        tests = tmp_dir / "tests"
        for name in ("test.sh", "score.py", "phybench_answer.py"):
            shutil.copy2(TEMPLATE_DIR / "tests" / name, tests / name)
        (tests / "test.sh").chmod(0o755)
        (tests / "ground_truth.json").write_text(
            json.dumps({"answer": problem.answer}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for src in eed_dir.iterdir():
            shutil.copy2(src, tests / "eed" / src.name)

        if task_dir.exists():
            shutil.rmtree(task_dir)
        tmp_dir.rename(task_dir)

    def run(self) -> None:
        """Generate one Harbor task per selected PHYBench problem under self.output_dir."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="phybench-adapter-") as tmp:
            data_path, eed_dir = self._fetch_inputs(Path(tmp))
            problems = self._load_problems(data_path)
            selected = self._select(problems)
            generated = skipped = 0
            for problem in selected:
                if (self.output_dir / problem.task_id).exists() and not self.overwrite:
                    skipped += 1
                    continue
                try:
                    self._write_task(problem, eed_dir)
                    generated += 1
                except (OSError, ValueError) as exc:
                    logger.error("Failed to generate %s: %s", problem.task_id, exc)
                    shutil.rmtree(self.output_dir / f".{problem.task_id}.tmp", ignore_errors=True)
                    self.failed_task_ids.append(problem.task_id)
        logger.info(
            "PHYBench: %d source problems, %d selected, %d generated, %d skipped (exists), %d failed -> %s",
            len(problems),
            len(selected),
            generated,
            skipped,
            len(self.failed_task_ids),
            self.output_dir,
        )
