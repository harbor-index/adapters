"""Grade a LoopsBench run-tests.sh log exactly as the upstream harness does.

Stdlib-only port of LoopsBench's result resolution (microsoft/Loopsbench, MIT):
``parsers/terminal_text_parser.py``, ``parsers/_pytest_tail_session.py``,
``parsers/_pytest_nested_filter.py``, ``parsers/_text_utils.py``,
``parsers/base_parser.normalize_test_case_id`` and ``Harness._is_resolved``.
Every LoopsBench task uses the default ``terminal-text`` result source on the
post-test log, so this is the only parser path the benchmark exercises.

Usage: loopsbench_grade.py <post-test-log> <exit-code> <out-dir>
Writes <out-dir>/reward.txt (1 or 0) and <out-dir>/loopsbench_result.json.
"""

import json
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"

# --- _text_utils.normalize_terminal_text -----------------------------------
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TMUX_PAD_RE = re.compile(r" {4,}")


def normalize_terminal_text(text: str) -> str:
    if not text:
        return text
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TMUX_PAD_RE.sub("\n", text)
    return "\n".join(line.rstrip() for line in text.split("\n"))


# --- base_parser.normalize_test_case_id ------------------------------------
def normalize_test_case_id(case_id: str) -> str:
    case_id = " ".join(case_id.split())
    if "::" not in case_id:
        return case_id
    path_part, remainder = case_id.split("::", 1)
    normalized_path = path_part.replace("\\", "/")
    while normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    while normalized_path.startswith("../"):
        normalized_path = normalized_path[3:]
    tests_marker = "/tests/"
    if tests_marker in normalized_path:
        normalized_path = normalized_path.split(tests_marker, 1)[1]
    elif normalized_path.startswith("tests/"):
        normalized_path = normalized_path[len("tests/"):]
    return f"{normalized_path}::{remainder}"


# --- _pytest_nested_filter -------------------------------------------------
_META_RUNNER_PREFIXES = ("test_unit_runner.py::",)
_NESTED_DOM_PREFIXES = ("test/units/", "test/integration/")


def filter_meta_runner_nested_cases(results: Dict[str, str]) -> Dict[str, str]:
    if not results or not any(
        any(n.startswith(p) for p in _META_RUNNER_PREFIXES) for n in results
    ):
        return results
    return {
        k: v
        for k, v in results.items()
        if not any(k.startswith(p) for p in _NESTED_DOM_PREFIXES)
    }


# --- _pytest_tail_session --------------------------------------------------
TAIL_CASE_ID = "__loopsbench_pytest_session_tail__"
_EXIT_RE = re.compile(r"^__EXIT__(-?\d+)\s*$")
_BANNER_RE = re.compile(r"^=+\s*(.+?)\s*=+\s*$")
_COUNT_TOKEN = re.compile(
    r"(\d+)\s+(failed|passed|error|skipped|warnings?|deselected)", re.IGNORECASE
)


def _count_word(inner: str, kind: str) -> int:
    pat = r"(\d+)\s+failed\b" if kind == "failed" else r"(\d+)\s+errors?\b"
    return sum(int(m.group(1)) for m in re.finditer(pat, inner, flags=re.IGNORECASE))


def extract_pytest_tail_session(output: str) -> Optional[Dict[str, str]]:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return None
    m_exit = _EXIT_RE.match(lines[-1])
    if not m_exit:
        return None
    exit_code = int(m_exit.group(1))
    if exit_code != 0:
        return {TAIL_CASE_ID: FAILED}
    if len(lines) < 2:
        return None
    m_banner = _BANNER_RE.match(lines[-2])
    if not m_banner:
        return None
    inner = m_banner.group(1)
    if not _COUNT_TOKEN.search(inner):
        return None
    if _count_word(inner, "failed") > 0 or _count_word(inner, "error") > 0:
        return {TAIL_CASE_ID: FAILED}
    return {TAIL_CASE_ID: PASSED}


# --- terminal_text_parser.TerminalTextParser -------------------------------
_STATUS_ALIASES = {
    "passed": PASSED,
    "通过": PASSED,
    "failed": FAILED,
    "失败": FAILED,
    "error": ERROR,
    "skipped": SKIPPED,
}
_PYTEST_STATUS = re.compile(
    r"^(PASSED|FAILED|ERROR|SKIPPED)\s+(\S+::\S+)(?:\s+-\s+.*)?$", re.MULTILINE
)
_PYTEST_STATUS_V = re.compile(
    r"^((?:\.{0,2}/)?[^\n]*::[^\n]*?)\s+(PASSED|FAILED|ERROR|SKIPPED)(?:\s+\[\s*\d+%\s*\])?\s*$",
    re.MULTILINE,
)
_PYTEST_SHORT_SUMMARY = re.compile(
    r"^(FAILED|ERROR|SKIPPED)\s+((?:\.{0,2}/)?[^\n]*::[^\n]*?)(?:\s+-\s+.*)?$",
    re.MULTILINE,
)
_CASE_START = re.compile(r"^---\s+(.+?)\s+---\s*$", re.MULTILINE)
_SUMMARY = re.compile(r"=+\s*(.*?)\s*=+\s*$", re.MULTILINE)
_GO_CASE = re.compile(r"^---\s+(PASS|FAIL|SKIP):\s+(.+?)(?:\s+\([^\n]+\))?\s*$", re.MULTILINE)
_JEST_CASE = re.compile(
    r"^\s*([✓✔✕✗])\s+(.+?)(?:\s+\((?:[0-9]+(?:\.[0-9]+)?)(?:\s*(?:ms|s))\))?\s*$",
    re.MULTILINE,
)
_MAVEN_FAILURE = re.compile(r"^\[ERROR\]\s+([^\s]+)\s+.*?<<<\s+(FAILURE|ERROR)!?\s*$", re.MULTILINE)
_PHPUNIT_OK = re.compile(r"^OK\s+\(.+tests?.*\)\s*$", re.MULTILINE)
_PHPUNIT_FAILURES = re.compile(r"^FAILURES!\s*$", re.MULTILINE)
_RAILS_SUMMARY = re.compile(
    r"^Finished in .*,\s*\d+ runs?,\s*\d+ assertions?,\s*(\d+) failures?,\s*(\d+) errors?(?:,\s*(\d+) skips?)?\.\s*$",
    re.MULTILINE,
)
_CASE_STATUS = re.compile(
    rf"^\s*\[({'|'.join(re.escape(a) for a in sorted(_STATUS_ALIASES, key=len, reverse=True))})\]\s+(.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_GO_MAP = {"PASS": PASSED, "FAIL": FAILED, "SKIP": SKIPPED}
_JEST_MAP = {"✓": PASSED, "✔": PASSED, "✕": FAILED, "✗": FAILED}
_MAVEN_MAP = {"FAILURE": FAILED, "ERROR": ERROR}
_PASS_TOKENS = {a for a, s in _STATUS_ALIASES.items() if s == PASSED}
_FAIL_TOKENS = {a for a, s in _STATUS_ALIASES.items() if s == FAILED}
_ERROR_TOKENS = {a for a, s in _STATUS_ALIASES.items() if s == ERROR}


def _pytest_style(output: str) -> Dict[str, str]:
    results: Dict[str, str] = {}
    for m in _PYTEST_STATUS.finditer(output):
        status, name = m.groups()
        results[name.strip()] = _STATUS_ALIASES.get(status.lower(), ERROR)
    for m in _PYTEST_STATUS_V.finditer(output):
        name, status = m.groups()
        results[name.strip()] = _STATUS_ALIASES.get(status.lower(), ERROR)
    for m in _PYTEST_SHORT_SUMMARY.finditer(output):
        status, name = m.groups()
        results[name.strip()] = _STATUS_ALIASES.get(status.lower(), ERROR)
    return filter_meta_runner_nested_cases(results)


def _shell_runner(output: str) -> Dict[str, str]:
    results: Dict[str, str] = {}
    starts = {m.group(1).strip() for m in _CASE_START.finditer(output)}
    if not starts:
        return results
    for m in _CASE_STATUS.finditer(output):
        status, name = m.groups()
        name = name.strip()
        if name not in starts and not any(s.endswith(name) or name.endswith(s) for s in starts):
            continue
        results[name] = _STATUS_ALIASES.get(status.lower(), ERROR)
    return results


def _native(output: str) -> Tuple[Dict[str, str], bool]:
    go = {m.group(2).strip(): _GO_MAP[m.group(1)] for m in _GO_CASE.finditer(output)}
    if go:
        return go, False
    jest = {m.group(2).strip(): _JEST_MAP[m.group(1)] for m in _JEST_CASE.finditer(output)}
    if jest:
        return jest, False
    maven = {m.group(1).strip(): _MAVEN_MAP[m.group(2)] for m in _MAVEN_FAILURE.finditer(output)}
    if maven:
        return maven, False
    if _PHPUNIT_OK.search(output):
        return {"__summary__": PASSED}, True
    if _PHPUNIT_FAILURES.search(output):
        return {"__summary__": FAILED}, True
    m = _RAILS_SUMMARY.search(output)
    if m:
        failures, errors, _ = m.groups()
        ok = int(failures) == 0 and int(errors) == 0
        return {"__summary__": PASSED if ok else FAILED}, True
    return {}, False


def _summary(output: str) -> Dict[str, str]:
    for m in reversed(list(_SUMMARY.finditer(output))):
        s = m.group(1).lower()
        has_pass = any(t in s for t in _PASS_TOKENS)
        has_fail = any(t in s for t in _FAIL_TOKENS)
        has_error = any(t in s for t in _ERROR_TOKENS)
        if not (has_pass or has_fail or has_error):
            continue
        if not re.search(r"\b\d+\b", s):
            continue
        if has_pass and not has_fail and not has_error:
            return {"__summary__": PASSED}
        return {"__summary__": FAILED}
    return {}


def parse(output: str) -> Tuple[Dict[str, str], bool]:
    """Return (results, is_summary_only) like TerminalTextParser._extract_results."""
    results: Dict[str, str] = {}
    results.update(_pytest_style(output))
    results.update(_shell_runner(output))
    tail = extract_pytest_tail_session(output)
    if tail is not None:
        results.update(tail)
    if results:
        return results, False
    native, summary_only = _native(output)
    if native:
        return native, summary_only
    summary = _summary(output)
    if summary:
        return summary, True
    return {}, False


def is_resolved(results: Dict[str, str]) -> bool:
    """Harness._is_resolved applied to the merged result set."""
    if not results:
        return False
    if TAIL_CASE_ID in results:
        return results[TAIL_CASE_ID] == PASSED
    if set(results) == {"__summary__"}:
        return False
    statuses = list(results.values())
    if not any(s == PASSED for s in statuses):
        return False
    return all(s in (PASSED, SKIPPED) for s in statuses)


def merge_results(parsed: Dict[str, str], summary_only: bool) -> Dict[str, str]:
    results: Dict[str, str] = {}
    for case, status in parsed.items():
        case_id = normalize_test_case_id(case)
        if case_id != "__summary__" or summary_only:
            results.setdefault(case_id, status)
    return results


def main() -> int:
    log_path, exit_code, out_dir = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
    raw = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    out = normalize_terminal_text(raw)
    # TestRunner: only the real exit status may supply the __EXIT__ marker.
    out = "\n".join(
        line for line in out.splitlines() if not re.fullmatch(r"__EXIT__-?\d+\s*", line.strip())
    )
    out = out.rstrip() + f"\n__EXIT__{exit_code}\n"
    if exit_code == 124:
        # Upstream records FailureMode.TEST_TIMEOUT before parsing: unresolved.
        results, resolved = {}, False
    else:
        parsed, summary_only = parse(out)
        results = merge_results(parsed, summary_only)
        resolved = is_resolved(results)
    counts: Dict[str, int] = {}
    for status in results.values():
        counts[status] = counts.get(status, 0) + 1
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "reward.txt").write_text("1\n" if resolved else "0\n")
    (out_dir / "loopsbench_result.json").write_text(
        json.dumps(
            {
                "resolved": resolved,
                "exit_code": exit_code,
                "timed_out": exit_code == 124,
                "counts": counts,
                "results": results,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"LoopsBench resolved={resolved} exit_code={exit_code} counts={counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
