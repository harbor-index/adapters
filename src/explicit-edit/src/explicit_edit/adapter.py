"""Generate Harbor tasks from the Explicit Edit Benchmark.

The upstream TypeScript generator (`explicitEditTasks()` in
src/suites/explicit-edit/fixtures.ts) is the single source of truth. This adapter
checks out upstream at a pinned commit, serializes the task matrix with
`export_tasks.mjs`, and writes one Harbor task directory per upstream task. Prompts,
fixtures, and scoring are not modified; all files are written as raw UTF-8 bytes so
CRLF and missing-final-newline fixtures stay byte-exact.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator

UPSTREAM_REPO = "https://github.com/alexshpunt/explicit-edit-benchmark.git"
UPSTREAM_COMMIT = "6d2769d2fbb547627b6f232541ff82ddfe81680c"

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "task-template"
EXPORT_SCRIPT = PACKAGE_DIR / "export_tasks.mjs"


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr}")
    return result.stdout


def _safe_relative(path: str) -> PurePosixPath:
    rel = PurePosixPath(path)
    # Reject anything PurePosixPath would normalize (e.g. "a//b", "./a"), so the
    # written path always equals the upstream key the verifier compares against.
    if rel.is_absolute() or rel.as_posix() != path or ".." in rel.parts:
        raise ValueError(f"Unsafe fixture path: {path!r}")
    return rel


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        target = root.joinpath(*_safe_relative(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))


class ExplicitEditAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        upstream_dir: Path | None = None,
        **kwargs,
    ):
        """
        Args:
            output_dir: Directory to write the generated tasks to.
            limit: Generate at most this many tasks (after --task-ids filtering).
            overwrite: Replace task directories that already exist.
            task_ids: Only generate these upstream task IDs.
            upstream_dir: Existing upstream checkout with `node_modules` installed.
                If omitted, upstream is cloned at UPSTREAM_COMMIT into a temp dir.
        """
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.upstream_dir = Path(upstream_dir).resolve() if upstream_dir else None

    # ------------------------------------------------------------------ upstream

    @contextmanager
    def _upstream_checkout(self) -> Iterator[Path]:
        if self.upstream_dir is not None:
            head = _run(["git", "rev-parse", "HEAD"], cwd=self.upstream_dir).strip()
            if head != UPSTREAM_COMMIT:
                print(
                    f"WARNING: upstream checkout is at {head}, adapter is pinned to "
                    f"{UPSTREAM_COMMIT}",
                    file=sys.stderr,
                )
            yield self.upstream_dir
            return
        with tempfile.TemporaryDirectory(prefix="explicit-edit-upstream-") as tmp:
            checkout = Path(tmp) / "explicit-edit-benchmark"
            print(f"Cloning {UPSTREAM_REPO} @ {UPSTREAM_COMMIT[:7]} ...", file=sys.stderr)
            _run(["git", "clone", "--quiet", UPSTREAM_REPO, str(checkout)])
            _run(["git", "checkout", "--quiet", UPSTREAM_COMMIT], cwd=checkout)
            print("Installing upstream npm dependencies ...", file=sys.stderr)
            _run(["npm", "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=checkout)
            yield checkout

    def load_tasks(self) -> list[dict]:
        with self._upstream_checkout() as checkout:
            out = _run(["node", "--import", "tsx", str(EXPORT_SCRIPT), str(checkout)], cwd=checkout)
        tasks = json.loads(out)
        if self.task_ids:
            known = {t["id"] for t in tasks}
            unknown = sorted(set(self.task_ids) - known)
            if unknown:
                raise ValueError(f"Unknown task IDs: {', '.join(unknown)}")
            wanted = set(self.task_ids)
            tasks = [t for t in tasks if t["id"] in wanted]
        if self.limit is not None:
            tasks = tasks[: self.limit]
        return tasks

    # ------------------------------------------------------------------ writing

    def _render_task_toml(self, task: dict) -> str:
        text = (TEMPLATE_DIR / "task.toml").read_text()
        keywords = ["explicit-edit", "file-editing", task["category"], task["language"], task["variant"]]
        replacements = {
            "{{TASK_ID}}": task["id"],
            "{{KEYWORDS}}": json.dumps(list(dict.fromkeys(keywords))),
            "{{CATEGORY}}": task["category"],
            "{{LANGUAGE}}": task["language"],
            "{{VARIANT}}": task["variant"],
            "{{SCALE}}": str(int(task["scale"])),
            "{{SCALE_UNIT}}": task["scaleUnit"],
            "{{FIXTURE_SHA256}}": task["fixtureSha256"],
        }
        for key, value in replacements.items():
            text = text.replace(key, value)
        return text

    def write_task(self, task: dict) -> Path:
        task_id = task["id"]
        if set(task["input"]) != set(task["expected"]):
            raise ValueError(f"{task_id}: input and expected file sets differ")
        task_dir = self.output_dir / task_id
        if task_dir.exists():
            shutil.rmtree(task_dir)

        shutil.copytree(TEMPLATE_DIR, task_dir, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
        (task_dir / "task.toml").write_text(self._render_task_toml(task))
        # Byte-for-byte upstream prompt, no trailing newline added.
        (task_dir / "instruction.md").write_bytes(task["prompt"].encode("utf-8"))
        _write_tree(task_dir / "environment" / "workspace", task["input"])
        _write_tree(task_dir / "solution" / "expected", task["expected"])
        _write_tree(task_dir / "tests" / "expected", task["expected"])
        return task_dir

    def run(self) -> None:
        tasks = self.load_tasks()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        written = skipped = 0
        for task in tasks:
            if (self.output_dir / task["id"]).exists() and not self.overwrite:
                skipped += 1
                continue
            self.write_task(task)
            written += 1
        print(
            f"Wrote {written} task(s), skipped {skipped} existing, to {self.output_dir}",
            file=sys.stderr,
        )
