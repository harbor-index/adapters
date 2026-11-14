"""Score one converted PDF page with the upstream olmOCR-Bench unit tests.

This is a thin, single-PDF driver around the unmodified upstream test classes
vendored under ``upstream/`` (allenai/olmocr, ``olmocr/bench/tests.py``). It
mirrors what ``olmocr/bench/benchmark.py`` does for one PDF and one Markdown
candidate ("one repeat"):

* tests are loaded from every ``bench_data/*.jsonl`` file with upstream
  ``load_tests`` (validation, duplicate-id check) and attributed to the JSONL
  file they come from;
* a default ``BaselineTest`` is added when the PDF has no explicit baseline
  test, and is attributed to the pseudo source ``baseline``
  (benchmark.py: ``for pdf in pdf_basenames: ... BaselineTest(...)``);
* each test is run on the Markdown. A test that raises counts as failed and is
  flagged in the results (benchmark.py ``process_test`` also fails that test,
  but then reports "FAILED (errors)" for the whole candidate instead of a
  score);
* a missing output, or one that is not a regular UTF-8 text file, is scored as
  an empty document and flagged in the results. The upstream conversion driver
  writes an empty file whenever a system produces no output for a page
  (``olmocr/bench/convert.py``: ``process_pdf``), so an empty document is what
  the original harness scores in that case; it has no score for a file it
  cannot read.

Outputs, written to the verifier log directory:

* ``reward.txt``                -- fraction of this PDF's tests that passed;
* ``olmocr_bench_results.json`` -- one record per test, including the source
  it is attributed to, so that the upstream "Overall" score (mean of the
  per-source pass rates) can be recomputed across tasks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# The vendored upstream package ``olmocr`` lives in ``upstream/`` next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent / "upstream"))

from olmocr.bench.tests import BaselineTest, BasePDFTest, load_tests

TESTS_DIR = Path(__file__).resolve().parent
RESULTS_FILENAME = "olmocr_bench_results.json"
BASELINE_SOURCE = "baseline"

# How the submitted document was found (``output_status`` in the results).
OUTPUT_OK = "ok"
OUTPUT_MISSING = "missing"
OUTPUT_UNREADABLE = "unreadable"


def load_page_tests(
    bench_data_dir: Path, pdf: str
) -> tuple[list[BasePDFTest], dict[str, str]]:
    """Load this PDF's tests the way benchmark.py does and map each to its source."""
    tests: list[BasePDFTest] = []
    sources: dict[str, str] = {}
    for jsonl_path in sorted(bench_data_dir.glob("*.jsonl")):
        loaded = load_tests(str(jsonl_path))

        # load_tests returns tests in thread-completion order; restore file order.
        with jsonl_path.open(encoding="utf-8") as handle:
            file_order = [json.loads(line)["id"] for line in handle if line.strip()]
        position = {test_id: index for index, test_id in enumerate(file_order)}
        loaded.sort(key=lambda test: position[test.id])

        for test in loaded:
            if test.pdf != pdf:
                raise ValueError(
                    f"Test {test.id} is for {test.pdf}, expected only tests for {pdf}"
                )
            sources[test.id] = jsonl_path.name
        tests.extend(loaded)

    if not any(test.type == "baseline" for test in tests):
        baseline = BaselineTest(id=f"{pdf}_baseline", pdf=pdf, page=1, type="baseline")
        tests.append(baseline)
        sources[baseline.id] = BASELINE_SOURCE
    return tests, sources


def read_markdown(markdown_path: Path) -> tuple[str, str]:
    """Return ``(content, output_status)`` for the submitted document.

    Only a regular file that decodes as UTF-8 is read. Anything else (no file, a
    directory, a link, a special file, bytes that are not UTF-8) is scored as an
    empty document.
    """
    if not os.path.lexists(markdown_path):
        print(f"{markdown_path} does not exist: scoring an empty document")
        return "", OUTPUT_MISSING
    if markdown_path.is_symlink() or not markdown_path.is_file():
        print(f"{markdown_path} is not a regular file: scoring an empty document")
        return "", OUTPUT_UNREADABLE
    try:
        with markdown_path.open("r", encoding="utf-8") as handle:
            return handle.read(), OUTPUT_OK
    except (OSError, ValueError) as exc:
        print(
            f"Cannot read {markdown_path} as UTF-8 text ({exc}): scoring an empty document"
        )
        return "", OUTPUT_UNREADABLE


def run_tests(
    tests: list[BasePDFTest], sources: dict[str, str], markdown: str
) -> list[dict[str, object]]:
    records = []
    for test in tests:
        record: dict[str, object] = {
            "id": test.id,
            "type": test.type,
            "source": sources[test.id],
        }
        try:
            passed, explanation = test.run(markdown)
        except Exception as exc:  # noqa: BLE001 - as upstream: any error fails the test
            passed, explanation = False, f"{type(exc).__name__}: {exc}"
            record["error"] = True
        record["passed"] = bool(passed)
        record["explanation"] = explanation
        records.append(record)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--markdown", type=Path, default=Path("/workspace/output.md"))
    parser.add_argument("--output-dir", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()

    task = json.loads((TESTS_DIR / "task.json").read_text(encoding="utf-8"))
    tests, sources = load_page_tests(TESTS_DIR / "bench_data", task["pdf"])
    markdown, output_status = read_markdown(args.markdown)
    records = run_tests(tests, sources, markdown)

    num_passed = sum(1 for record in records if record["passed"])
    reward = num_passed / len(records)

    for record in records:
        status = "PASS" if record["passed"] else "FAIL"
        detail = f" -- {record['explanation']}" if record["explanation"] else ""
        print(f"[{status}] {record['type']:8s} {record['id']}{detail}")
    print(f"{num_passed}/{len(records)} tests passed, reward = {reward}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = {
        "pdf": task["pdf"],
        "output_status": output_status,
        "num_tests": len(records),
        "num_passed": num_passed,
        "num_errors": sum(1 for record in records if record.get("error")),
        "reward": reward,
        "tests": records,
    }
    (args.output_dir / RESULTS_FILENAME).write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (args.output_dir / "reward.txt").write_text(f"{reward}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
