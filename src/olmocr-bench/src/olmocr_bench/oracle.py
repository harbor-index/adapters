"""Oracle synthesis for olmOCR-Bench tasks.

olmOCR-Bench ships no reference transcriptions: every PDF only comes with
pass/fail unit tests. The "oracle" built here is therefore derived from those
assertions alone. It is a Markdown document constructed so that each unit test
of one PDF passes; it is *not* a faithful transcription of the page.

How each upstream test type (``olmocr/bench/tests.py``) is satisfied:

``present``
    The asserted text is emitted verbatim on its own line(s).
``order``
    ``before`` and ``after`` are emitted verbatim, each once, in an order that
    is consistent with every reading-order assertion of the page.
``table``
    One small HTML table per test that places the asserted neighbours and
    headings around the target cell.
``math``
    The asserted LaTeX is emitted verbatim as a ``\\[ ... \\]`` display equation.
    The upstream test accepts it by exact string comparison or, when the
    asserted LaTeX has surrounding whitespace, by comparing the renderings.
``absent``
    Nothing is emitted. Pages whose only assertions are ``absent`` tests get a
    one-line placeholder so that the baseline "page is not blank" test passes.
``baseline``
    Satisfied by any non-blank output without CJK/emoji characters.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from typing import Any

# Emitted for pages that only carry ``absent`` (header/footer) assertions.
PLACEHOLDER = "Body text."

_SUPPORTED_TYPES = {"present", "absent", "order", "table", "math", "baseline"}

TestRecord = Mapping[str, Any]


class UnsupportedTestError(ValueError):
    """Raised for test definitions this synthesiser does not know how to satisfy."""


def build_oracle_markdown(tests: Sequence[TestRecord]) -> str:
    """Return a Markdown document that satisfies all ``tests`` of one PDF."""
    for test in tests:
        _check_supported(test)

    blocks = _text_blocks(tests)
    blocks += [_table_block(test) for test in tests if test["type"] == "table"]
    blocks += [_math_block(test) for test in tests if test["type"] == "math"]
    if not blocks:
        blocks = [PLACEHOLDER]
    return "\n\n".join(blocks) + "\n"


def _check_supported(test: TestRecord) -> None:
    test_type = test.get("type")
    if test_type not in _SUPPORTED_TYPES:
        raise UnsupportedTestError(
            f"Test {test.get('id')!r} has unsupported type {test_type!r}"
        )
    if test_type == "baseline" and test.get("max_length") is not None:
        raise UnsupportedTestError(
            f"Test {test.get('id')!r} is a blank-page check (max_length), "
            "which this synthesiser does not handle"
        )
    if test_type == "present" and (test.get("first_n") or test.get("last_n")):
        raise UnsupportedTestError(
            f"Test {test.get('id')!r} restricts a present test to a window "
            "(first_n/last_n), which this synthesiser does not handle"
        )


def _text_blocks(tests: Sequence[TestRecord]) -> list[str]:
    """Order every asserted text snippet so that all ``order`` tests hold.

    Each distinct snippet is emitted once. Snippets are sorted topologically
    (stable with respect to first appearance in the test file). Tests that take
    part in a cycle, which is legitimate when a phrase occurs twice on a page,
    are appended as explicit before/after pairs instead.
    """
    snippets: list[str] = []
    successors: dict[str, list[str]] = {}
    indegree: dict[str, int] = {}

    def add(snippet: str) -> None:
        if snippet not in indegree:
            snippets.append(snippet)
            successors[snippet] = []
            indegree[snippet] = 0

    order_tests = [test for test in tests if test["type"] == "order"]
    for test in tests:
        if test["type"] == "present":
            add(test["text"])
        elif test["type"] == "order":
            add(test["before"])
            add(test["after"])
    for test in order_tests:
        before, after = test["before"], test["after"]
        if before != after and after not in successors[before]:
            successors[before].append(after)
            indegree[after] += 1

    ordered: list[str] = []
    ready = [snippet for snippet in snippets if indegree[snippet] == 0]
    while ready:
        snippet = ready.pop(0)
        ordered.append(snippet)
        for successor in successors[snippet]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)

    sorted_snippets = set(ordered)
    for test in order_tests:
        before, after = test["before"], test["after"]
        in_cycle = before not in sorted_snippets or after not in sorted_snippets
        if in_cycle or before == after:
            ordered += [before, after]
    return ordered


def _table_block(test: TestRecord) -> str:
    """Build an HTML table in which the target cell has the asserted neighbours.

    Layout (rows and columns are only present when the test asks for them)::

                                top_heading (th)
                                up
        left_heading (th)  left  CELL          right
                                down
    """
    up = test.get("up") or ""
    down = test.get("down") or ""
    left = test.get("left") or ""
    right = test.get("right") or ""
    top_heading = test.get("top_heading") or ""
    left_heading = test.get("left_heading") or ""

    columns = [
        name
        for name, wanted in (
            ("left_heading", left_heading),
            ("left", left),
            ("cell", True),
            ("right", right),
        )
        if wanted
    ]
    rows = [
        name
        for name, wanted in (
            ("top_heading", top_heading),
            ("up", up),
            ("cell", True),
            ("down", down),
        )
        if wanted
    ]
    content = {
        ("top_heading", "cell"): ("th", top_heading),
        ("up", "cell"): ("td", up),
        ("cell", "left_heading"): ("th", left_heading),
        ("cell", "left"): ("td", left),
        ("cell", "cell"): ("td", test["cell"]),
        ("cell", "right"): ("td", right),
        ("down", "cell"): ("td", down),
    }

    lines = ["<table>"]
    for row in rows:
        cells = []
        for column in columns:
            tag, text = content.get((row, column), ("td", ""))
            cells.append(f"<{tag}>{html.escape(text, quote=False)}</{tag}>")
        lines.append("<tr>" + "".join(cells) + "</tr>")
    lines.append("</table>")
    return "\n".join(lines)


def _math_block(test: TestRecord) -> str:
    """Emit the asserted LaTeX as a display equation.

    The upstream test strips whitespace from every extracted equation before
    comparing it. If the asserted LaTeX ends in a control space (a backslash
    followed by trailing whitespace), stripping would leave a dangling
    backslash that KaTeX cannot parse, so an empty group is appended to keep
    the control space intact. It renders to nothing.
    """
    math = test["math"]
    stripped = math.rstrip()
    trailing_backslashes = len(stripped) - len(stripped.rstrip("\\"))
    if stripped != math and trailing_backslashes % 2 == 1:
        math += "{}"
    return "\\[\n" + math + "\n\\]"
