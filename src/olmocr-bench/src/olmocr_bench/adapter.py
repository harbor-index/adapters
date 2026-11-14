"""Generate Harbor tasks for olmOCR-Bench: one task per single-page PDF.

Source data is the Hugging Face dataset ``allenai/olmOCR-bench`` (ODC-BY) at a
pinned revision. ``bench_data/*.jsonl`` hold the pass/fail unit tests and
``bench_data/pdfs/<category>/*.pdf`` the single-page PDFs they refer to.

Each generated task asks the agent to convert ``/workspace/document.pdf`` to
Markdown at ``/workspace/output.md``. The verifier runs the page's unit tests
with the unmodified upstream test classes from ``allenai/olmocr`` (Apache-2.0),
vendored under ``task-template/tests/upstream``, and reports the fraction of
tests that pass.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .oracle import build_oracle_markdown

logger = logging.getLogger(__name__)

DATASET_REPO_ID = "allenai/olmOCR-bench"
DATASET_REVISION = "54a96a6fb6a2bd3b297e59869491db4d3625b711"

TEMPLATE_DIR = Path(__file__).parent / "task-template"

SPLITS = ("full", "parity")
PARITY_SEED = 42
PARITY_TASKS_PER_CATEGORY = 20

# KaTeX files are only needed to render equations, i.e. by ``math`` tests.
_KATEX_FILES = {"katex.min.js", "katex.min.css", "LICENSE-KaTeX"}


@dataclass(frozen=True)
class OlmOCRBenchTask:
    """One PDF of the benchmark together with its unit tests."""

    task_id: str  # e.g. "arxiv-math-2503-04048-pg46"
    pdf: str  # path below bench_data/pdfs, e.g. "arxiv_math/2503.04048_pg46.pdf"
    category: str  # PDF directory, e.g. "arxiv_math"
    # Raw JSONL lines of this PDF's tests, keyed by the JSONL file they come from.
    test_lines: dict[str, tuple[str, ...]]

    @property
    def tests(self) -> list[dict]:
        return [
            json.loads(line) for lines in self.test_lines.values() for line in lines
        ]


def make_task_id(pdf: str) -> str:
    """Derive a registry-safe task ID from a PDF path below ``bench_data/pdfs``.

    ``arxiv_math/2503.04048_pg46.pdf`` -> ``arxiv-math-2503-04048-pg46``
    """
    stem = PurePosixPath(pdf).with_suffix("").as_posix()
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


def select_parity_subset(tasks: list[OlmOCRBenchTask]) -> list[OlmOCRBenchTask]:
    """Pick a deterministic subset with the same number of PDFs per category.

    Within each category, tasks are ranked by ``sha256("<seed>:<task_id>")`` and
    the first ``PARITY_TASKS_PER_CATEGORY`` are kept. Ranking by a keyed hash
    (instead of drawing from a PRNG) keeps the selection independent of the
    Python version and of which other tasks exist.
    """
    by_category: dict[str, list[OlmOCRBenchTask]] = defaultdict(list)
    for task in tasks:
        by_category[task.category].append(task)

    def rank(task: OlmOCRBenchTask) -> str:
        return hashlib.sha256(f"{PARITY_SEED}:{task.task_id}".encode()).hexdigest()

    subset: list[OlmOCRBenchTask] = []
    for category in sorted(by_category):
        subset.extend(
            sorted(by_category[category], key=rank)[:PARITY_TASKS_PER_CATEGORY]
        )
    return sorted(subset, key=lambda task: task.task_id)


class OlmOCRBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "full",
        source_dir: Path | None = None,
    ):
        """
        Args:
            output_dir: Directory to write the generated tasks to.
            limit: Generate at most this many tasks (after all other filters).
            overwrite: Regenerate task directories that already exist.
            task_ids: Only generate these tasks. Accepts Harbor task IDs
                (``arxiv-math-2503-04048-pg46``) and upstream PDF paths
                (``arxiv_math/2503.04048_pg46.pdf``).
            split: ``full`` for every PDF, ``parity`` for the parity subset.
            source_dir: Local copy of the dataset (the directory that contains
                ``bench_data/``). Downloaded from Hugging Face when omitted.
        """
        if split not in SPLITS:
            raise ValueError(f"Unknown split {split!r}; expected one of {SPLITS}")
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split
        self.source_dir = Path(source_dir) if source_dir is not None else None

    def run(self) -> None:
        """Load the benchmark and write one Harbor task directory per selected PDF."""
        bench_dir = self._resolve_bench_data_dir()
        tasks = self._select_tasks(self._load_tasks(bench_dir))

        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated = skipped = 0
        for task in tasks:
            task_dir = self.output_dir / task.task_id
            if task_dir.exists():
                if not self.overwrite:
                    skipped += 1
                    continue
                shutil.rmtree(task_dir)
            self._write_task(task, bench_dir, task_dir)
            generated += 1

        logger.info(
            "Generated %d task(s), skipped %d existing, in %s",
            generated,
            skipped,
            self.output_dir,
        )

    def _resolve_bench_data_dir(self) -> Path:
        """Return the ``bench_data`` directory, downloading the dataset if needed."""
        if self.source_dir is not None:
            root = self.source_dir.expanduser()
            logger.info("Using local dataset copy at %s", root)
        else:
            from huggingface_hub import snapshot_download

            logger.info(
                "Downloading %s at revision %s", DATASET_REPO_ID, DATASET_REVISION
            )
            try:
                root = Path(
                    snapshot_download(
                        repo_id=DATASET_REPO_ID,
                        repo_type="dataset",
                        revision=DATASET_REVISION,
                        allow_patterns=["bench_data/*.jsonl", "bench_data/pdfs/*.pdf"],
                    )
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Could not download {DATASET_REPO_ID}@{DATASET_REVISION} from "
                    "Hugging Face. Check network access, or pass --source-dir with "
                    "a local copy of the dataset."
                ) from exc

        for candidate in (root / "bench_data", root):
            if (candidate / "pdfs").is_dir() and any(candidate.glob("*.jsonl")):
                return candidate
        raise FileNotFoundError(
            f"No olmOCR-Bench data found under {root}: expected bench_data/*.jsonl "
            "and bench_data/pdfs/"
        )

    def _load_tasks(self, bench_dir: Path) -> list[OlmOCRBenchTask]:
        """Group the unit tests of every JSONL file by the PDF they refer to.

        Mirrors the upstream harness, which evaluates every PDF found below
        ``pdfs/`` against the tests of every ``*.jsonl`` file next to it.
        """
        pdf_root = bench_dir / "pdfs"
        pdfs = sorted(
            path.relative_to(pdf_root).as_posix() for path in pdf_root.rglob("*.pdf")
        )
        if not pdfs:
            raise FileNotFoundError(f"No PDF files found under {pdf_root}")

        lines_by_pdf: dict[str, dict[str, list[str]]] = {pdf: {} for pdf in pdfs}
        num_tests = 0
        for jsonl_path in sorted(bench_dir.glob("*.jsonl")):
            with jsonl_path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    where = f"{jsonl_path.name}:{line_number}"
                    try:
                        test = json.loads(line)
                        pdf, page = test["pdf"], test["page"]
                    except (json.JSONDecodeError, KeyError, TypeError) as exc:
                        raise ValueError(f"{where}: malformed test: {exc}") from exc
                    if pdf not in lines_by_pdf:
                        raise ValueError(
                            f"{where}: test refers to {pdf!r}, which does not "
                            f"exist under {pdf_root}"
                        )
                    if page != 1:
                        raise ValueError(
                            f"{where}: test targets page {page!r}; every task "
                            "is built around a single-page PDF"
                        )
                    lines_by_pdf[pdf].setdefault(jsonl_path.name, []).append(line)
                    num_tests += 1

        tasks: dict[str, OlmOCRBenchTask] = {}
        for pdf in pdfs:
            task_id = make_task_id(pdf)
            if not task_id:
                raise ValueError(f"Cannot derive a task ID from PDF path {pdf!r}")
            if task_id in tasks:
                raise ValueError(
                    f"Task ID collision: {pdf!r} and {tasks[task_id].pdf!r} both "
                    f"map to {task_id!r}"
                )
            tasks[task_id] = OlmOCRBenchTask(
                task_id=task_id,
                pdf=pdf,
                category=PurePosixPath(pdf).parent.name or "uncategorized",
                test_lines={
                    name: tuple(lines) for name, lines in lines_by_pdf[pdf].items()
                },
            )

        logger.info("Loaded %d PDFs with %d unit tests", len(tasks), num_tests)
        return [tasks[task_id] for task_id in sorted(tasks)]

    def _select_tasks(self, tasks: list[OlmOCRBenchTask]) -> list[OlmOCRBenchTask]:
        if self.split == "parity":
            tasks = select_parity_subset(tasks)
        if self.task_ids is not None:
            requested = set(self.task_ids)
            tasks = [
                task
                for task in tasks
                if task.task_id in requested or task.pdf in requested
            ]
            matched = {task.task_id for task in tasks} | {task.pdf for task in tasks}
            for unknown in sorted(requested - matched):
                logger.warning("No task matches %r in split %r", unknown, self.split)
        if self.limit is not None:
            tasks = tasks[: max(self.limit, 0)]
        return tasks

    def _write_task(
        self, task: OlmOCRBenchTask, bench_dir: Path, task_dir: Path
    ) -> None:
        tests = task.tests
        has_math = any(test.get("type") == "math" for test in tests)

        task_dir.mkdir(parents=True)
        (task_dir / "task.toml").write_text(
            self._render_task_toml(task), encoding="utf-8"
        )
        shutil.copyfile(TEMPLATE_DIR / "instruction.md", task_dir / "instruction.md")

        environment_dir = task_dir / "environment"
        environment_dir.mkdir()
        shutil.copyfile(
            TEMPLATE_DIR / "environment" / "Dockerfile", environment_dir / "Dockerfile"
        )
        shutil.copyfile(bench_dir / "pdfs" / task.pdf, environment_dir / "document.pdf")

        solution_dir = task_dir / "solution"
        solution_dir.mkdir()
        _copy_script(TEMPLATE_DIR / "solution" / "solve.sh", solution_dir / "solve.sh")
        (solution_dir / "oracle.md").write_text(
            build_oracle_markdown(tests), encoding="utf-8", newline="\n"
        )

        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        _copy_script(TEMPLATE_DIR / "tests" / "test.sh", tests_dir / "test.sh")
        shutil.copyfile(
            TEMPLATE_DIR / "tests" / "evaluate.py", tests_dir / "evaluate.py"
        )
        (tests_dir / "task.json").write_text(
            json.dumps({"pdf": task.pdf}, indent=2) + "\n", encoding="utf-8"
        )
        # Keep the upstream JSONL file names: the upstream harness reports a
        # pass rate per JSONL file and averages those for its headline score.
        bench_data_dir = tests_dir / "bench_data"
        bench_data_dir.mkdir()
        for jsonl_name, lines in task.test_lines.items():
            (bench_data_dir / jsonl_name).write_text(
                "".join(line + "\n" for line in lines), encoding="utf-8", newline="\n"
            )

        def ignore(_directory: str, names: list[str]) -> set[str]:
            ignored = {name for name in names if name == "__pycache__"}
            return ignored if has_math else ignored | (_KATEX_FILES & set(names))

        shutil.copytree(
            TEMPLATE_DIR / "tests" / "upstream", tests_dir / "upstream", ignore=ignore
        )

    @staticmethod
    def _render_task_toml(task: OlmOCRBenchTask) -> str:
        rendered = (TEMPLATE_DIR / "task.toml").read_text(encoding="utf-8")
        for placeholder, value in (
            ("{task_id}", task.task_id),
            ("{category}", _toml_escape(task.category)),
            ("{source_pdf}", _toml_escape(task.pdf)),
        ):
            rendered = rendered.replace(placeholder, value)
        tomllib.loads(rendered)  # fail early if the rendered file is not valid TOML
        return rendered


def _copy_script(source: Path, destination: Path) -> None:
    shutil.copyfile(source, destination)
    destination.chmod(0o755)


def _toml_escape(value: str) -> str:
    """Escape ``value`` for use inside a double-quoted TOML string."""
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"Control character in {value!r}")
    return value.replace("\\", "\\\\").replace('"', '\\"')
