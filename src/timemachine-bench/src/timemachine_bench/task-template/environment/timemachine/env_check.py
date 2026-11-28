"""Compare the built environment with the record's `dependency_versions`.

Upstream records `dependency_versions` as the `pip freeze` of its built image.
Run once at image build, after the setup script: writes
/opt/timemachine/env_check.json with the packages missing from or extra to the
image. The verifier copies the result into verifier/metrics.json, so a build
whose dependency resolution drifted from upstream (for example after index
timeouts) is visible in every trial.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

HERE = Path("/opt/timemachine")
WORK = Path("/work")


def uses_poetry() -> bool:
    pyproject = WORK / "pyproject.toml"
    setup = next(WORK.glob("setup_*.sh"), None)
    return (
        setup is not None
        and "poetry install" in setup.read_text()
        and pyproject.is_file()
        and "[tool.poetry" in pyproject.read_text()
    )


def requirements(lines: list[str]) -> set[str]:
    """Normalized pins, without the project itself (`-e /work`, `pkg @ file:///work`)."""
    out = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-e ") or "@ file://" in line:
            continue
        name, sep, rest = line.partition("==")
        out.add(re.sub(r"[-_.]+", "-", name).lower() + sep + rest)
    return out


def main() -> int:
    command = ["poetry", "run", "pip", "freeze"] if uses_poetry() else ["pip", "freeze"]
    freeze = subprocess.run(command, cwd=WORK, capture_output=True, text=True, check=True)
    expected = requirements((HERE / "dependency_versions.txt").read_text().splitlines())
    actual = requirements(freeze.stdout.splitlines())
    result = {
        "matches_upstream": expected == actual,
        "missing": sorted(expected - actual),
        "extra": sorted(actual - expected),
    }
    (HERE / "env_check.json").write_text(json.dumps(result, indent=2))
    status = "matches" if result["matches_upstream"] else "DIFFERS FROM"
    print(f"environment {status} the record's dependency_versions: {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
