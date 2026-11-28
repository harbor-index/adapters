"""Recover the upstream agent's inputs from a generated task's instruction.md."""

from __future__ import annotations

import re

# upstream solve_openai.py: TARGET_PYTHON_VERSION
UPSTREAM_PYTHON_VERSION = "3.12.11"


def parse_instruction(instruction: str) -> dict[str, object]:
    """Recover the upstream agent inputs from the task's instruction.md."""

    def block(tag: str) -> str:
        match = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", instruction, re.S)
        if not match:
            raise ValueError(f"instruction has no <{tag}> block")
        return match.group(1)

    script = re.search(r"bash (test_(\S+)\.sh)", instruction)
    if not script:
        raise ValueError("instruction does not name the test script")
    escaped = script.group(2)
    python_version = block("python_version").strip()
    if python_version != UPSTREAM_PYTHON_VERSION:
        raise ValueError(f"unexpected python version {python_version}")
    return {
        "test_script": script.group(1),
        # upstream passes "owner/repo"; only its escaped form is recoverable, and
        # upstream uses the name only to build file names, so any "/" form works
        "repo_name": escaped.replace("__", "/", 1),
        # upstream writes the record's field verbatim; it always ends in one "\n"
        "dependency_versions": block("dependency_versions") + "\n",
        "test_files": [line.strip() for line in block("test_files").splitlines() if line.strip()],
    }
