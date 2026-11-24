"""Byte-exact workspace verifier for Explicit Edit Benchmark tasks.

Port of upstream `compareExplicitFiles` (src/suites/explicit-edit/files.ts @ 6d2769d):
walk the whole workspace without following symlinks and fail on differing bytes,
files not in the expected set, missing files, non-regular entries, and directories
that contain no expected file.

Usage: python3 verify.py <workspace> <expected-dir> <verifier-log-dir>

Writes to <verifier-log-dir>:
  reward.json  {"reward": exact (0/1), "eof_normalized": diagnostic (0/1)}
  reward.txt   exact (0/1)
  comparison.json  the full comparison report
"""

import json
import os
import re
import sys
from pathlib import Path


def load_expected(expected_dir: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for root, _dirs, names in os.walk(expected_dir):
        for name in names:
            path = Path(root) / name
            files[path.relative_to(expected_dir).as_posix()] = path.read_bytes()
    return files


def compare_explicit_files(directory: Path, expected: dict[str, bytes]) -> dict:
    actual: set[str] = set()
    differing: list[str] = []
    unexpected: list[str] = []
    invalid: list[str] = []

    def visit(relative: str) -> None:
        with os.scandir(directory / relative if relative else directory) as entries:
            for entry in entries:
                name = f"{relative}/{entry.name}" if relative else entry.name
                if entry.is_dir(follow_symlinks=False):
                    if not any(f.startswith(f"{name}/") for f in expected):
                        unexpected.append(f"{name}/")
                    visit(name)
                elif not entry.is_file(follow_symlinks=False):
                    invalid.append(name)
                else:
                    actual.add(name)
                    target = expected.get(name)
                    if target is None:
                        unexpected.append(name)
                    elif Path(entry.path).read_bytes() != target:
                        differing.append(name)

    visit("")
    missing = [name for name in expected if name not in actual]
    return {
        "exactMatch": not (differing or unexpected or missing or invalid),
        "differingFiles": sorted(differing),
        "unexpectedFiles": sorted(unexpected),
        "missingFiles": sorted(missing),
        "invalidEntries": sorted(invalid),
    }


_TRAILING_EOLS = re.compile(r"(?:\r\n|\n)+\Z")


def _trim_eof(data: bytes) -> str:
    # Upstream compares decoded text (Node's default UTF-8 decoding, lossy).
    return _TRAILING_EOLS.sub("", data.decode("utf-8", errors="replace"))


def eof_normalized(directory: Path, expected: dict[str, bytes], comparison: dict) -> bool:
    """Upstream EOF-normalized diagnostic (scripts/oracle-failure-evidence.mjs).

    Passes when exact, or when the file set is correct and every differing file
    matches after stripping trailing (\\r\\n|\\n)+. Upstream additionally requires the
    agent to exit 0 without timing out; Harbor's verifier cannot observe that, so this
    value is diagnostic only and never used as the reward.
    """
    if comparison["exactMatch"]:
        return True
    if comparison["unexpectedFiles"] or comparison["missingFiles"] or comparison["invalidEntries"]:
        return False
    return all(
        _trim_eof((directory / name).read_bytes()) == _trim_eof(expected[name])
        for name in comparison["differingFiles"]
    )


def main() -> None:
    workspace, expected_dir, log_dir = (Path(arg) for arg in sys.argv[1:4])
    expected = load_expected(expected_dir)
    comparison = compare_explicit_files(workspace, expected)
    exact = 1 if comparison["exactMatch"] else 0
    eof = 1 if eof_normalized(workspace, expected, comparison) else 0

    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
    (log_dir / "reward.txt").write_text(f"{exact}\n")
    (log_dir / "reward.json").write_text(json.dumps({"reward": exact, "eof_normalized": eof}) + "\n")
    print(json.dumps({"reward": exact, "eof_normalized": eof, **comparison}, indent=2))


if __name__ == "__main__":
    main()
