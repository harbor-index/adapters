"""Unified test case classification logic.

Single source of truth for F2P/P2P test status classification across all
dispatcher scripts.  Uses rewrite_script_verify.py as the reference standard.

Status definitions (aligned with rewrite_script_verify.py):
  - F2P before fail: FAILED, ERROR          (NOT XFAIL, NOT MISSING)
  - F2P after pass:  PASSED, XPASS          (NOT XFAIL)
  - P2P before pass: PASSED only             (NOT XPASS, NOT XFAIL)
  - P2P after pass:  PASSED, XPASS           (NOT XFAIL)
  - XFAIL:           neither pass nor fail   (excluded from 4-category)
  - MISSING before + build failure: F2P → count as fail; P2P → skip
  - MISSING before + no build failure: record as missing
  - MISSING after:   record as missing (not pass, not fail)
  - Synthetic entries (_xxx_): stripped before matching
"""

import os
import re
import sys
from typing import Dict, List, Optional, Tuple

# active_swe is an installable package — sibling modules importable via relative refs
from .f2p_p2p import _expand_suite_fails, _remove_file_level_noise
from .log_parsers import parse_test_output
from .container import detect_build_failure, is_synthetic_status


# =============================================================================
# Flaky test patterns — tests known to be environment-sensitive
# =============================================================================
# matplotlib [png] image comparison tests fail due to rendering differences
# in container environments (font/DPI/backend), not due to actual regressions.
_FLAKY_PATTERNS = (
    # matplotlib [png] image comparison tests — rendering env-sensitive
    # Path may be "lib/mpl_toolkits/..." without "matplotlib" keyword, so match [png] alone
    lambda name: "[png" in name,
)


def _is_flaky_test(test_name: str) -> bool:
    """Return True if the test is a known flaky/environment-sensitive test."""
    return any(pat(test_name) for pat in _FLAKY_PATTERNS)


# =============================================================================
# Status constants — the single source of truth
# =============================================================================
FAIL_BEFORE_STATUSES = frozenset(("FAILED", "ERROR"))
PASS_AFTER_STATUSES = frozenset(("PASSED", "XPASS"))
PASS_BEFORE_P2P_STATUSES = frozenset(("PASSED",))
XFAIL_STATUSES = frozenset(("XFAIL",))


# =============================================================================
# Core helpers
# =============================================================================
def strip_synthetic_entries(status_map: Dict[str, str]) -> Dict[str, str]:
    """Remove synthetic entries (_xxx_) from a status map.

    Returns a new dict without synthetic keys.  These are markers like
    _maven_build_failure_ that are not real test names and should never
    be matched against GT F2P/P2P lists or counted in test classifications.
    """
    return {k: v for k, v in status_map.items()
            if not (k.startswith("_") and k.endswith("_"))}


def parse_and_prepare(
    before_log: str,
    after_log: str,
    language: str = "",
    test_framework: str = "",
    log_parser: str = "",
) -> Tuple[Dict[str, str], Dict[str, str], bool, bool]:
    """Common pipeline: parse logs → expand → clean → strip synthetic → detect build failure.

    Args:
        before_log: Test output log before applying patch (empty string if unavailable).
        after_log: Test output log after applying patch.
        language: Programming language hint.
        test_framework: Known test framework.
        log_parser: Known parser name.

    Returns:
        (before_real, after_real, before_build_fail, after_build_fail)
        where *_real has synthetic entries stripped.
    """
    before_status = parse_test_output(
        before_log, language=language, test_framework=test_framework, log_parser=log_parser
    ) if before_log else {}
    after_status = parse_test_output(
        after_log, language=language, test_framework=test_framework, log_parser=log_parser
    )

    _expand_suite_fails(before_status, after_status)
    _remove_file_level_noise(before_status)
    _remove_file_level_noise(after_status)

    before_real = strip_synthetic_entries(before_status)
    after_real = strip_synthetic_entries(after_status)

    before_build_fail = detect_build_failure(before_log, before_status) if before_log else False
    after_build_fail = detect_build_failure(after_log, after_status)

    return before_real, after_real, before_build_fail, after_build_fail


def parse_and_prepare_after_only(
    after_log: str,
    language: str = "",
    test_framework: str = "",
    log_parser: str = "",
) -> Tuple[Dict[str, str], bool]:
    """Parse only the after log (for dispatcher_eval.py which has no before log).

    Returns:
        (after_real, after_build_fail)
    """
    after_status = parse_test_output(
        after_log, language=language, test_framework=test_framework, log_parser=log_parser
    )
    # Expand suite fails with empty before map (still processes after's own suite markers)
    _expand_suite_fails({}, after_status)
    _remove_file_level_noise(after_status)

    after_real = strip_synthetic_entries(after_status)
    after_build_fail = detect_build_failure(after_log, after_status)

    return after_real, after_build_fail


# =============================================================================
# GT-based evaluation (dispatcher_eval.py style — after log only)
# =============================================================================
def evaluate_patch_against_gt(
    gt_f2p: List[str],
    gt_p2p: List[str],
    after_log: str,
    language: str = "",
    test_framework: str = "",
    log_parser: str = "",
) -> Dict[str, list]:
    """Evaluate a predicted patch against GT F2P/P2P lists using after-log only.

    This is the unified replacement for dispatcher_eval.py's evaluate_patch().

    Returns a dict with:
      - FAIL_TO_PASS: list of GT F2P tests that passed in after
      - PASS_TO_PASS: list of GT P2P tests that passed in after
      - f2p_passed:  same as FAIL_TO_PASS
      - f2p_failed:  list of GT F2P tests that did NOT pass in after
      - p2p_passed:  same as PASS_TO_PASS
      - p2p_failed:  list of GT P2P tests that did NOT pass in after
    """
    after_real, _ = parse_and_prepare_after_only(
        after_log, language=language, test_framework=test_framework, log_parser=log_parser
    )

    after_passed = set()
    for name, status in after_real.items():
        if status and status.upper() in PASS_AFTER_STATUSES:
            after_passed.add(name)

    def _test_passes(gt_name):
        return _gt_test_matches_passed(gt_name, after_passed)

    f2p_passed = [t for t in gt_f2p if _test_passes(t)]
    f2p_failed = [t for t in gt_f2p if not _test_passes(t)]
    p2p_passed = [t for t in gt_p2p if _test_passes(t)]
    p2p_raw_failed = [t for t in gt_p2p if not _test_passes(t)]
    p2p_failed = [t for t in p2p_raw_failed if not _is_flaky_test(t)]
    p2p_flaky = [t for t in p2p_raw_failed if _is_flaky_test(t)]

    return {
        "FAIL_TO_PASS": f2p_passed,
        "PASS_TO_PASS": p2p_passed,
        "f2p_passed": f2p_passed,
        "f2p_failed": f2p_failed,
        "p2p_passed": p2p_passed,
        "p2p_failed": p2p_failed,
        "p2p_flaky": p2p_flaky,
    }


def evaluate_patch_against_gt_parsed(
    gt_f2p: List[str],
    gt_p2p: List[str],
    after_real: Dict[str, str],
) -> Dict[str, list]:
    """Same as evaluate_patch_against_gt but accepts pre-parsed after_real dict.

    Use this when the caller has already parsed the log (e.g., for build failure
    detection) to avoid parsing the same log twice.

    Returns the same dict as evaluate_patch_against_gt.
    """
    after_passed = set()
    for name, status in after_real.items():
        if status and status.upper() in PASS_AFTER_STATUSES:
            after_passed.add(name)

    def _test_passes(gt_name):
        return _gt_test_matches_passed(gt_name, after_passed)

    f2p_passed = [t for t in gt_f2p if _test_passes(t)]
    f2p_failed = [t for t in gt_f2p if not _test_passes(t)]
    p2p_passed = [t for t in gt_p2p if _test_passes(t)]
    p2p_raw_failed = [t for t in gt_p2p if not _test_passes(t)]
    p2p_failed = [t for t in p2p_raw_failed if not _is_flaky_test(t)]
    p2p_flaky = [t for t in p2p_raw_failed if _is_flaky_test(t)]

    return {
        "FAIL_TO_PASS": f2p_passed,
        "PASS_TO_PASS": p2p_passed,
        "f2p_passed": f2p_passed,
        "f2p_failed": f2p_failed,
        "p2p_passed": p2p_passed,
        "p2p_failed": p2p_failed,
        "p2p_flaky": p2p_flaky,
    }


def _gt_test_matches_passed(gt_name: str, after_passed: set[str]) -> bool:
    """Return whether an official GT test has a passing parsed log entry.

    Test identifiers are not formatted consistently by all supported runners.
    The allowed equivalences are deliberately narrow: a full identifier may
    differ only by a pytest parameter suffix, or a runner may omit the file
    prefix while retaining the exact class+method or method identity.  This
    prevents a broad substring such as ``test_constructor`` from satisfying a
    distinct passing test named ``test_constructor_with_coords``.
    """
    if gt_name in after_passed:
        return True
    if not after_passed:
        return False

    expected = _canonical_gt_test_id(gt_name)
    return any(_canonical_gt_test_id(name) == expected for name in after_passed)


def _canonical_gt_test_id(name: str) -> str:
    """Canonicalize only unambiguous runner formatting differences for GT."""
    value = str(name).strip()
    # pytest parameterization is not part of the test-function identity.
    value = re.sub(r"\[.*\]$", "", value)

    if "::" in value:
        parts = value.split("::")
        # Preserve a test class where present; file paths are presentation only.
        if len(parts) >= 2 and parts[-2].startswith("Test"):
            value = "::".join(parts[-2:])
        else:
            value = parts[-1]
    else:
        # unittest may display ``test_name (package.Class)``.  Keep the method,
        # which is also the stable part of the official instance identifier.
        display = re.match(r"^(test_[A-Za-z0-9_]+)\s*\(.*\)$", value)
        if display:
            value = display.group(1)
        elif "." in value and value.rsplit(".", 1)[-1].startswith("test_"):
            value = value.rsplit(".", 1)[-1]

    return value.lower()


# =============================================================================
# All-test classification (dispatcher_test_patch.py style — no GT, 4-category)
# =============================================================================
def classify_all_tests(
    before_real: Dict[str, str],
    after_real: Dict[str, str],
    before_build_fail: bool = False,
) -> Dict[str, int]:
    """Classify ALL tests from before/after status maps into categories.

    Used by dispatcher_test_patch.py for evaluating LLM-generated test patches.

    Uses the standard status sets:
      - p2p_success:  before PASSED          → after PASSED/XPASS
      - p2p_failure:  before PASSED          → after FAILED/ERROR/MISSING
      - f2p_success:  before FAILED/ERROR    → after PASSED/XPASS
      - invalid:      before FAILED/ERROR    → after FAILED/ERROR/MISSING
      - xfail_count:  any test with XFAIL status (excluded from 4-cat)
      - before_missing_build_fail: F2P tests missing before + build failure

    MISSING handling (aligned with rewrite_script_verify.py):
      - MISSING before + build_fail: treated as fail (for F2P context)
      - MISSING before + no build_fail: excluded from classification
      - MISSING after: excluded from classification

    Args:
        before_real: Status map with synthetic entries already stripped.
        after_real: Status map with synthetic entries already stripped.
        before_build_fail: Whether before log had a build failure.

    Returns:
        Dict with: p2p_success_count, p2p_failure_count, f2p_success_count,
        invalid_count, xfail_count, test_count
    """
    p2p_success_count = 0  # pass before → pass after
    p2p_failure_count = 0  # pass before → fail after (regression)
    f2p_success_count = 0  # fail before → pass after
    invalid_count = 0       # fail before → fail after
    xfail_count = 0         # any XFAIL status (excluded from 4-cat)
    before_missing_bf = 0   # MISSING before + build failure (counted as F2P context)

    all_test_names = set(before_real.keys()) | set(after_real.keys())

    for name in all_test_names:
        bs = before_real.get(name)  # None = MISSING
        as_ = after_real.get(name)  # None = MISSING

        # Check for XFAIL — excluded from the 4-category classification
        if (bs is not None and bs in XFAIL_STATUSES) or (as_ is not None and as_ in XFAIL_STATUSES):
            xfail_count += 1
            continue

        # Before status classification
        bp = bs in PASS_BEFORE_P2P_STATUSES if bs is not None else False
        bf = bs in FAIL_BEFORE_STATUSES if bs is not None else False

        # After status classification
        ap = as_ in PASS_AFTER_STATUSES if as_ is not None else False

        # Handle MISSING before
        if bs is None:
            if before_build_fail:
                # Build failure → F2P missing counts as fail
                before_missing_bf += 1
                # Check after status
                if as_ is not None and ap:
                    f2p_success_count += 1
                else:
                    invalid_count += 1
            # else: MISSING before + no build failure → exclude
            continue

        # Handle MISSING after (exclude from classification)
        if as_ is None:
            # After is missing — cannot determine after status, exclude
            continue

        # 4-category classification
        if bp and ap:
            p2p_success_count += 1
        elif bp and not ap:
            p2p_failure_count += 1
        elif bf and ap:
            f2p_success_count += 1
        elif bf and not ap:
            invalid_count += 1
        # else: before has some other status (not PASSED, not FAILED/ERROR) → exclude

    test_count = p2p_success_count + p2p_failure_count + f2p_success_count + invalid_count

    return {
        "p2p_success_count": p2p_success_count,
        "p2p_failure_count": p2p_failure_count,
        "f2p_success_count": f2p_success_count,
        "invalid_count": invalid_count,
        "xfail_count": xfail_count,
        "before_missing_bf": before_missing_bf,
        "test_count": test_count,
    }


# =============================================================================
# F2P/P2P derivation from before/after status maps (rewrite_script_extract.py style)
# =============================================================================
def derive_f2p_p2p_from_maps(
    before_real: Dict[str, str],
    after_real: Dict[str, str],
) -> Tuple[List[str], List[str], List[str]]:
    """Derive FAIL_TO_PASS, XFAIL_TO_PASS, PASS_TO_PASS lists from status maps.

    Uses the standard status sets (aligned with rewrite_script_verify.py):
      - F2P:  before FAILED/ERROR  → after PASSED/XPASS
      - XFAIL_TO_PASS: before XFAIL → after PASSED/XPASS
      - P2P:  before PASSED        → after PASSED/XPASS

    Note: XFAIL after is NOT counted as P2P — XFAIL is neither pass nor fail
    per the unified classification standard. A PASSED→XFAIL transition is
    excluded from all categories (similar to how XFAIL is excluded from
    the 4-category classification in classify_all_tests).

    Args:
        before_real: Status map (synthetic entries already stripped).
        after_real: Status map (synthetic entries already stripped).

    Returns:
        (f2p_list, xfail_to_pass_list, p2p_list)
    """
    all_test_names = set(before_real.keys()) | set(after_real.keys())

    f2p = []
    xfail_to_pass = []
    p2p = []

    for name in sorted(all_test_names):
        bs = before_real.get(name)
        as_ = after_real.get(name)

        if bs is None or as_ is None:
            # MISSING before or after → cannot classify, skip
            continue

        # F2P: FAILED/ERROR before → PASSED/XPASS after
        if bs in FAIL_BEFORE_STATUSES and as_ in PASS_AFTER_STATUSES:
            f2p.append(name)
        # XFAIL_TO_PASS: XFAIL before → PASSED/XPASS after
        elif bs in XFAIL_STATUSES and as_ in PASS_AFTER_STATUSES:
            xfail_to_pass.append(name)
        # P2P: PASSED before → PASSED/XFAIL/XPASS after
        elif bs in PASS_BEFORE_P2P_STATUSES and as_ in PASS_AFTER_STATUSES:
            p2p.append(name)

    return f2p, xfail_to_pass, p2p
