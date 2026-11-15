"""Generate Harbor tasks from HumanEvalPack (OctoPack, arXiv:2308.07124).

One Harbor task is one problem of one upstream task in one language, for
example ``fixtests-python-0`` for problem ``Python/0`` of the upstream task
``humanevalfixtests-python``. See ``upstream.py`` for the ported prompt
construction and the pinned sources, and ``task-template/tests/verify.py`` for
how a submission is scored.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import upstream
from .instructions import LANGUAGE_TEXT, SOLUTION_DIR, SUBSET_TEXT, TOOLCHAIN_DIR

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "task-template"
TASK_NAME_PREFIX = "bigcode/humanevalpack__"
PROBLEMS_PER_LANGUAGE = 164
# A task directory without these files (and its solution file) is treated as
# incomplete.
REQUIRED_TASK_FILES = (
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "solution/solve.sh",
    "tests/test.sh",
    "tests/verify.py",
    "tests/execute.py",
    "tests/execute.NOTICE.txt",
    "tests/task.json",
)
_STAGING_PREFIX = ".staging-"
_STAGING_NAME = re.compile(re.escape(_STAGING_PREFIX) + r"[0-9a-f]{8}")
DATASET_COLUMNS = (
    "task_id",
    "prompt",
    "declaration",
    "canonical_solution",
    "buggy_solution",
    "bug_type",
    "failure_symptoms",
    "entry_point",
    "import",
    "test_setup",
    "test",
    "instruction",
)

# Problems left out of every subset because the benchmark's own reference solution
# (``prompt`` + ``canonical_solution``) does not pass the original harness
# (``--check_references``) every time, so no reliable oracle can be shipped for
# them. Keys are upstream ``task_id`` values; the README gives the measurements.
EXCLUDED_PROBLEMS = {
    "Go/27": (
        'the prompt imports "strings" but the canonical solution never uses it, '
        "so the reference solution does not compile"
    ),
    "Go/95": (
        "the canonical solution stops after the first two keys of a Go map, whose "
        "iteration order is random, and fails its own tests in a part of the runs"
    ),
    "Rust/32": (
        "the canonical solution runs Newton's method from x = 0 on polynomials that "
        "the test draws from an unseeded random generator; for some of them it never "
        "converges, so the reference solution times out in a part of the runs"
    ),
}

# The parity subset takes this many problems from every (subset, language)
# stratum: the ones whose sha256("<seed>:<task id>") sorts first.
PARITY_SEED = "humanevalpack-parity-v1"
PARITY_TASKS_PER_STRATUM = 20

# Harbor's limit for running tests/test.sh. The original harness stops scoring
# after its own timeout plus one second, and verify.py kills whatever is left
# 30 seconds later; the rest is head room for a slow container.
VERIFIER_TIMEOUT_SEC = {
    language: float(seconds + 120)
    for language, seconds in upstream.LANGUAGE_TO_TIMEOUT.items()
}


@dataclass(frozen=True)
class Subset:
    """One upstream task family."""

    key: str  # prefix of the Harbor task ids
    upstream_prefix: str  # upstream task name is "<upstream_prefix>-<language>"
    title: str
    category: str
    with_docs: bool | None  # argument of the upstream fix prompt; None for synthesis
    default: bool  # part of the dataset generated without --subsets

    def upstream_task(self, language: str) -> str:
        return f"{self.upstream_prefix}-{language}"

    def prompt(self, doc: upstream.Doc, language: str) -> str:
        if self.with_docs is None:
            return upstream.synthesize_prompt(doc, language)
        return upstream.fix_prompt(doc, language, self.with_docs)


SUBSETS = {
    subset.key: subset
    for subset in (
        Subset(
            "fixtests", "humanevalfixtests", "HumanEvalFix", "code-repair", False, True
        ),
        Subset(
            "fixdocs",
            "humanevalfixdocs",
            "HumanEvalFixDocs",
            "code-repair",
            True,
            False,
        ),
        Subset(
            "synthesize",
            "humanevalsynthesize",
            "HumanEvalSynthesize",
            "code-synthesis",
            None,
            True,
        ),
    )
}
DEFAULT_SUBSETS = tuple(key for key, subset in SUBSETS.items() if subset.default)


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    subset: Subset
    language: str
    doc: dict[str, str]

    @property
    def stratum(self) -> tuple[str, str]:
        return (self.subset.key, self.language)

    @property
    def solution_file(self) -> str:
        return "solution." + upstream.LANGUAGE_TO_EXTENSION[self.language]


def sanitize_id(value: str) -> str:
    """Lowercase an upstream identifier and reduce it to ``[a-z0-9-]``."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def make_task_id(subset: Subset, upstream_task_id: str) -> str:
    """``("fixtests", "Python/0")`` -> ``"fixtests-python-0"``."""
    return f"{subset.key}-{sanitize_id(upstream_task_id)}"


_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def render(template: str, values: dict[str, str]) -> str:
    """Fill ``{name}`` placeholders in one pass, so substituted text is never rescanned."""

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise KeyError(f"Template placeholder {{{name}}} has no value")
        return values[name]

    return _PLACEHOLDER.sub(substitute, template)


def code_fence(text: str) -> str:
    """A backtick fence longer than any backtick run inside ``text``."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def toml_string(value: str) -> str:
    """A TOML basic string (JSON string syntax is a subset of it)."""
    return json.dumps(value, ensure_ascii=False)


def write_text(path: Path, content: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755 if executable else 0o644)


class DatasetError(RuntimeError):
    """The pinned dataset could not be downloaded or read."""


def load_language(language: str, cache_dir: Path | None = None) -> list[dict[str, str]]:
    """Download (or reuse from the Hugging Face cache) and read one dataset config."""
    import polars as pl
    from huggingface_hub import hf_hub_download

    filename = f"{language}/test-00000-of-00001.parquet"
    try:
        path = hf_hub_download(
            repo_id=upstream.DATASET_REPO,
            repo_type="dataset",
            revision=upstream.DATASET_REVISION,
            filename=filename,
            cache_dir=cache_dir,
        )
        rows = pl.read_parquet(path).to_dicts()
    except Exception as error:  # network, cache and file-format errors alike
        raise DatasetError(
            f"Could not load {filename} of {upstream.DATASET_REPO} at revision "
            f"{upstream.DATASET_REVISION}: {error}"
        ) from error

    if len(rows) != PROBLEMS_PER_LANGUAGE:
        raise ValueError(
            f"{language}: expected {PROBLEMS_PER_LANGUAGE} problems, found {len(rows)}"
        )
    for row in rows:
        for column in DATASET_COLUMNS:
            if not isinstance(row.get(column), str):
                # Malformed source data, reported like the other data errors.
                raise ValueError(  # noqa: TRY004
                    f"{language}: {row.get('task_id')!r} has no text in column {column!r}"
                )
        if language == "go" and not upstream.go_imports_are_supported(row):
            raise ValueError(
                f"{row['task_id']}: import outside the harness's Go import list"
            )
    if len({row["task_id"] for row in rows}) != len(rows):
        raise ValueError(f"{language}: duplicate task_id values")
    return rows


def parity_rank(task_id: str) -> str:
    return hashlib.sha256(f"{PARITY_SEED}:{task_id}".encode()).hexdigest()


def select_parity(tasks: Sequence[TaskSpec]) -> list[TaskSpec]:
    """Seeded, stratified subset: the same number of problems per subset and language."""
    chosen: set[str] = set()
    for stratum in dict.fromkeys(task.stratum for task in tasks):
        members = [task.task_id for task in tasks if task.stratum == stratum]
        chosen.update(sorted(members, key=parity_rank)[:PARITY_TASKS_PER_STRATUM])
    return [task for task in tasks if task.task_id in chosen]


class HumanEvalPackAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        subsets: Iterable[str] | None = None,
        languages: Iterable[str] | None = None,
        split: str = "full",
        cache_dir: Path | None = None,
    ):
        """
        Args:
            output_dir: Directory the task directories are written to.
            limit: Generate only the first N selected tasks.
            overwrite: Regenerate task directories that already exist (they are
                skipped otherwise).
            task_ids: Generate only these tasks (``fixtests-python-0`` or the full
                name ``bigcode/humanevalpack__fixtests-python-0``).
            subsets: Any of ``fixtests``, ``fixdocs``, ``synthesize``. Defaults to
                ``fixtests`` and ``synthesize``; with ``task_ids`` it defaults to all
                three so that any task can be requested by id.
            languages: Dataset configs to include; defaults to all six.
            split: ``full`` or ``parity`` (a seeded, stratified subset).
            cache_dir: Hugging Face cache directory for the dataset download.
        """
        if split not in ("full", "parity"):
            raise ValueError(f"Unknown split {split!r}; expected 'full' or 'parity'")
        if limit is not None and limit < 0:
            raise ValueError("limit must not be negative")

        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = (
            [self._local_id(task_id) for task_id in task_ids] if task_ids else None
        )
        if subsets is None:
            subsets = SUBSETS if self.task_ids else DEFAULT_SUBSETS
        self.subsets = self._ordered(subsets, SUBSETS, "subset")
        self.languages = self._ordered(
            languages or upstream.LANGUAGES, upstream.LANGUAGES, "language"
        )
        self.split = split
        self.cache_dir = Path(cache_dir) if cache_dir else None

    @staticmethod
    def _ordered(selected: Iterable[str], known: Iterable[str], kind: str) -> list[str]:
        selected = list(selected)
        known = list(known)
        unknown = [item for item in selected if item not in known]
        if unknown:
            raise ValueError(
                f"Unknown {kind}(s): {', '.join(unknown)}; expected {', '.join(known)}"
            )
        return [item for item in known if item in selected]

    @staticmethod
    def _local_id(task_id: str) -> str:
        return task_id.removeprefix(TASK_NAME_PREFIX)

    def list_tasks(self) -> list[TaskSpec]:
        """All tasks of the selected subsets and languages, in generation order."""
        docs = {
            language: load_language(language, self.cache_dir)
            for language in self.languages
        }
        return [
            TaskSpec(
                make_task_id(SUBSETS[key], doc["task_id"]), SUBSETS[key], language, doc
            )
            for key in self.subsets
            for language in self.languages
            for doc in docs[language]
            if doc["task_id"] not in EXCLUDED_PROBLEMS
        ]

    def excluded_task_ids(self) -> dict[str, str]:
        """Task ids that exist upstream but are not generated, with the reason."""
        return {
            make_task_id(subset, upstream_task_id): reason
            for subset in SUBSETS.values()
            for upstream_task_id, reason in EXCLUDED_PROBLEMS.items()
        }

    def select_tasks(self) -> list[TaskSpec]:
        """Apply ``split``, then ``task_ids``, then ``limit``."""
        tasks = self.list_tasks()
        if len({task.task_id for task in tasks}) != len(tasks):
            raise ValueError("Task ids are not unique")
        if self.split == "parity":
            tasks = select_parity(tasks)
        if self.task_ids is not None:
            available = {task.task_id for task in tasks}
            excluded = self.excluded_task_ids()
            for task_id in self.task_ids:
                if task_id in excluded:
                    raise ValueError(
                        f"{task_id} is excluded from the adapter: {excluded[task_id]}"
                    )
            missing = [task_id for task_id in self.task_ids if task_id not in available]
            if missing:
                raise ValueError(
                    f"Task id(s) not in the selected subsets, languages and split: {', '.join(missing)}"
                )
            wanted = set(self.task_ids)
            tasks = [task for task in tasks if task.task_id in wanted]
        if self.limit is not None:
            tasks = tasks[: self.limit]
        return tasks

    def run(self) -> None:
        """Write one Harbor task directory per selected task into ``self.output_dir``."""
        tasks = self.select_tasks()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        lock, locked = self._lock_output_dir()
        staging_root = None
        try:
            pending = self._pending_tasks(tasks)
            if locked:
                # Only the lock holder knows that a leftover is not in use.
                for entry in os.scandir(self.output_dir):
                    if _STAGING_NAME.fullmatch(entry.name) and entry.is_dir(
                        follow_symlinks=False
                    ):
                        shutil.rmtree(entry.path)
            staging_root = self.output_dir / f"{_STAGING_PREFIX}{secrets.token_hex(4)}"
            staging_root.mkdir()
            for task in pending:
                # Build the task aside and move it into place only when it is
                # complete, so an interrupted run never leaves a partial task.
                staged = staging_root / task.task_id
                self._write_task(task, staged)
                self._move_into_place(staged, self.output_dir / task.task_id)
        finally:
            if staging_root is not None:
                shutil.rmtree(staging_root, ignore_errors=True)
            os.close(lock)

        logger.info(
            "Selected %d task(s): generated %d, skipped %d existing (use --overwrite to regenerate) in %s",
            len(tasks),
            len(pending),
            len(tasks) - len(pending),
            self.output_dir,
        )

    def _lock_output_dir(self) -> tuple[int, bool]:
        """Take an exclusive lock on the output directory for the rest of the run.

        Returns the open descriptor and whether the lock is held (some file
        systems do not support locks).
        """
        descriptor = os.open(self.output_dir, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise ValueError(
                f"Another humanevalpack run is writing to {self.output_dir}; wait for it "
                "to finish and run the adapter again."
            ) from None
        except OSError as error:
            logger.warning(
                "Could not lock %s (%s); make sure no other humanevalpack run writes to it "
                "at the same time.",
                self.output_dir,
                error,
            )
            return descriptor, False
        return descriptor, True

    def _pending_tasks(self, tasks: list[TaskSpec]) -> list[TaskSpec]:
        """The tasks to write: all of them with ``overwrite``, otherwise the missing ones."""
        if self.overwrite:
            return tasks
        pending, incomplete = [], []
        for task in tasks:
            task_dir = self.output_dir / task.task_id
            if not os.path.lexists(task_dir):
                pending.append(task)
            elif not all(
                (task_dir / name).is_file()
                for name in (*REQUIRED_TASK_FILES, f"solution/{task.solution_file}")
            ):
                incomplete.append(task.task_id)
        if incomplete:
            raise ValueError(
                f"{len(incomplete)} existing task director(ies) in {self.output_dir} are "
                f"incomplete (for example {incomplete[0]}); run again with --overwrite to "
                "regenerate them."
            )
        return pending

    @staticmethod
    def _move_into_place(staged: Path, task_dir: Path) -> None:
        """Rename a finished task into the dataset; an existing copy is parked and
        put back if the rename fails."""
        previous = staged.with_name(staged.name + ".previous")
        if os.path.lexists(task_dir):
            task_dir.rename(previous)
        try:
            staged.rename(task_dir)
        except BaseException:
            if os.path.lexists(previous) and not os.path.lexists(task_dir):
                previous.rename(task_dir)
            raise
        if previous.is_symlink() or previous.is_file():
            previous.unlink()
        elif previous.is_dir():
            shutil.rmtree(previous)

    # --- one task -------------------------------------------------------------

    def _write_task(self, task: TaskSpec, task_dir: Path) -> None:
        write_text(task_dir / "task.toml", self._task_toml(task))
        write_text(task_dir / "instruction.md", self._instruction(task))
        self._write_environment(task, task_dir / "environment")
        self._write_solution(task, task_dir / "solution")
        self._write_tests(task, task_dir / "tests")
        # The same modes whatever the umask: write_text() sets the file modes.
        task_dir.chmod(0o755)
        for path in task_dir.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)

    def _task_toml(self, task: TaskSpec) -> str:
        doc, subset, language = task.doc, task.subset, task.language
        extra_metadata = toml_line("entry_point", doc["entry_point"])
        if subset.with_docs is not None:  # the bug annotations describe the fix tasks
            extra_metadata += toml_line("bug_type", doc["bug_type"])
            extra_metadata += toml_line("failure_symptoms", doc["failure_symptoms"])
        values = {
            "task_id": task.task_id,
            "solution_artifact": toml_string(f"{SOLUTION_DIR}/{task.solution_file}"),
            "description": toml_string(
                f"{subset.title} ({upstream.LANGUAGE_TO_NAME[language]}), problem {doc['task_id']} of HumanEvalPack"
            ),
            "keywords": json.dumps(
                ["humanevalpack", subset.upstream_prefix, subset.category, language]
            ),
            "category": toml_string(subset.category),
            "language": toml_string(language),
            "upstream_task": toml_string(subset.upstream_task(language)),
            "upstream_task_id": toml_string(doc["task_id"]),
            "extra_metadata": extra_metadata,
            "verifier_timeout_sec": str(VERIFIER_TIMEOUT_SEC[language]),
        }
        rendered = render(
            (TEMPLATE_DIR / "task.toml").read_text(encoding="utf-8"), values
        )
        # Fail here rather than in Harbor if the file is not valid TOML.
        tomllib.loads(rendered)
        return rendered

    def _instruction(self, task: TaskSpec) -> str:
        language_name = upstream.LANGUAGE_TO_NAME[task.language]
        solution_path = f"{SOLUTION_DIR}/{task.solution_file}"
        subset_text = SUBSET_TEXT[task.subset.key]
        language_text = LANGUAGE_TEXT[task.language]
        prompt = task.subset.prompt(task.doc, task.language)
        inner = {
            "language_name": language_name,
            "solution_path": solution_path,
            "tests_phrase": subset_text.tests_phrase,
            "toolchain_dir": TOOLCHAIN_DIR,
        }
        values = {
            "title": render(subset_text.title, inner),
            "summary": render(subset_text.summary, inner),
            "fence": code_fence(prompt),
            "prompt": prompt,
            "action": subset_text.action,
            "solution_path": solution_path,
            "language_name": language_name,
            "declaration_extras": language_text.declaration_extras,
            "exclusions": language_text.exclusions,
            "grading": render(language_text.grading, inner),
            "tools": render(language_text.tools, inner),
        }
        return render(
            (TEMPLATE_DIR / "instruction.md").read_text(encoding="utf-8"), values
        )

    def _write_environment(self, task: TaskSpec, environment_dir: Path) -> None:
        template = TEMPLATE_DIR / "environment"
        dockerfile = (template / "Dockerfile").read_text(encoding="utf-8")
        dockerfile += (template / f"Dockerfile.{task.language}").read_text(
            encoding="utf-8"
        )
        write_text(environment_dir / "Dockerfile", dockerfile)
        # Files the language section copies into the image (the cargo manifest).
        context = template / task.language
        if context.is_dir():
            for source in sorted(context.iterdir()):
                write_text(
                    environment_dir / source.name, source.read_text(encoding="utf-8")
                )

    def _write_solution(self, task: TaskSpec, solution_dir: Path) -> None:
        values = {"solution_dir": SOLUTION_DIR, "solution_file": task.solution_file}
        script = render(
            (TEMPLATE_DIR / "solution" / "solve.sh").read_text(encoding="utf-8"), values
        )
        write_text(solution_dir / "solve.sh", script, executable=True)
        write_text(
            solution_dir / task.solution_file, upstream.reference_solution(task.doc)
        )

    def _write_tests(self, task: TaskSpec, tests_dir: Path) -> None:
        template = TEMPLATE_DIR / "tests"
        write_text(
            tests_dir / "test.sh",
            (template / "test.sh").read_text(encoding="utf-8"),
            executable=True,
        )
        write_text(
            tests_dir / "verify.py",
            (template / "verify.py").read_text(encoding="utf-8"),
        )
        # The original executor and its notice are copied byte for byte.
        for name in ("execute.py", "execute.NOTICE.txt"):
            shutil.copyfile(template / name, tests_dir / name)
            (tests_dir / name).chmod(0o644)
        doc = task.doc
        task_data = {
            "task_id": task.task_id,
            "upstream_task": task.subset.upstream_task(task.language),
            "upstream_task_id": doc["task_id"],
            "language": task.language,
            "solution_file": task.solution_file,
            "entry_point": doc["entry_point"],
            "source": {
                "dataset": upstream.DATASET_REPO,
                "dataset_revision": upstream.DATASET_REVISION,
                "harness_commit": upstream.HARNESS_COMMIT,
                "metric_commit": upstream.METRIC_COMMIT,
            },
            # The dataset fields the original scoring code reads.
            "declaration": doc["declaration"],
            "test_setup": doc["test_setup"],
            "test": doc["test"],
        }
        write_text(
            tests_dir / "task.json",
            json.dumps(task_data, indent=2, ensure_ascii=False) + "\n",
        )


def toml_line(key: str, value: str) -> str:
    return f"{key} = {toml_string(value)}\n"
