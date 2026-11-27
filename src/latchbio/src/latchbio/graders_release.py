"""The pinned latch-eval-tools release: graders and prompt suffix.

LatchBio publishes its graders and the data instructions it appends to every
prompt in the ``latch-eval-tools`` package. The adapter does not copy either
into this repository. It depends on the PyPI release pinned in
``latchbio.sources`` and reads both from the installed package; every task image
installs the same release for its verifier.

``task-template/tests/graders.lock.json`` records the SHA-256 of each file the
adapter relies on. The verifier checks the grader files against it before
grading, and :func:`data_instructions` checks the prompt suffix, so a different
release can never be used silently.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

LOCK_PATH = Path(__file__).parent / "task-template" / "tests" / "graders.lock.json"
DATA_INSTRUCTIONS = "prompts/data_instructions.md"


class GradersReleaseError(RuntimeError):
    """Raised when the installed latch-eval-tools is not the pinned release."""


def load_lock() -> dict[str, Any]:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def package_dir() -> Path:
    """Directory of the installed ``latch_eval_tools`` package.

    The package is located without importing it: its ``__init__`` imports the
    agent harness, whose dependencies the adapter does not install.
    """
    loaded = sys.modules.get("latch_eval_tools")
    if loaded is not None:
        # Already imported, or registered as a bare parent package by the verifier.
        return Path(next(iter(loaded.__path__)))
    spec = importlib.util.find_spec("latch_eval_tools")
    if spec is None or not spec.submodule_search_locations:
        raise GradersReleaseError(
            "latch-eval-tools is not installed. Run the adapter with `uv run` from "
            "src/latchbio so that its pinned dependencies are used."
        )
    return Path(next(iter(spec.submodule_search_locations)))


def mismatches(files: dict[str, str], root: Path | None = None) -> list[str]:
    """Relative paths in ``files`` that are missing or differ from their recorded SHA-256."""
    root = package_dir() if root is None else root
    return [
        rel
        for rel, digest in sorted(files.items())
        if not (root / rel).is_file()
        or hashlib.sha256((root / rel).read_bytes()).hexdigest() != digest
    ]


def data_instructions() -> str:
    """The prompt suffix upstream's CLI harnesses append to every task.

    ``latch_eval_tools.harness.utils.load_data_instructions`` reads this same file.
    """
    lock = load_lock()
    if mismatches(lock["prompt_files"]):
        raise GradersReleaseError(
            f"{DATA_INSTRUCTIONS} of the installed latch-eval-tools differs from the "
            f"pinned release {lock['version']}."
        )
    return (package_dir() / DATA_INSTRUCTIONS).read_text(encoding="utf-8")
