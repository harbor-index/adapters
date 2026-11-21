"""TaxCalcBench scoring, vendored from column-tax/tax-calc-bench.

The parsing and comparison code below is copied from
``tax_calc_bench/tax_return_evaluator.py`` (``TaxReturnEvaluator``) at the
upstream commit pinned in the adapter (``main.py: TAXCALCBENCH_REF``), MIT
license. Only the I/O wrapper at the bottom is new. The scored lines and
XPaths come from ``scored_fields.json``, which the adapter generates from the
upstream definitions (``get_ty25_scoring_fields`` for TY25,
``LINES_TO_XPATH_VALUES`` for TY24).

Upstream metrics per return:
- strictly correct return: every scored line equals the expected value;
- lenient correct return: every scored line within $5;
- correct by line / by line lenient: share of scored lines.
The Harbor reward is the strict one (the leaderboard's headline metric).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from lxml import etree

TY25 = "ty25"


# ---- copied from TaxReturnEvaluator (upstream) ------------------------------
def parse_xml_value(tree, xpath: str) -> float:
    """Extract value from XML using XPath"""
    xpath_result = tree.xpath(xpath)
    if isinstance(xpath_result, (int, float)) and not isinstance(xpath_result, bool):
        return float(xpath_result) if math.isfinite(xpath_result) else 0.0
    if isinstance(xpath_result, list) and len(xpath_result) > 0:
        value = xpath_result[0]
        if isinstance(value, etree._Element):
            raw_value = value.text
        else:
            raw_value = str(value)
        return parse_money_amount(raw_value or "")
    if isinstance(xpath_result, str):
        return parse_money_amount(xpath_result)
    return 0.0


def parse_generated_value(generated_return: str, line: str) -> float:
    """Extract value from generated tax return for a specific line"""
    if line not in generated_return:
        return 0.0

    # Find referenced line in generated_tax_return and get text until newline
    referenced_line = generated_return.split(line)[1].split("\n")[0]

    # Parse amount from referenced line after pipe character |
    if "|" in referenced_line:
        return parse_money_amount(referenced_line.split("|")[-1].strip())
    return 0.0


def parse_generated_scored_value(generated_return: str, label: str) -> float:
    """Extract a TY25 value using exact first-column label matching."""
    matching_amounts = []
    for line in generated_return.splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) >= 3 and parts[0] == label:
            matching_amounts.append(parts[-1])

    if len(matching_amounts) != 1:
        return 0.0
    return parse_money_amount(matching_amounts[0])


def parse_money_amount(dollar_string: str) -> float:
    """Parse money amount from dollar string"""
    if not dollar_string or dollar_string.strip() == "":
        return 0.0

    # Remove dollar signs, commas, and convert to float
    cleaned = dollar_string.replace("$", "").replace(",", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = f"-{cleaned[1:-1]}"
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def evaluate(generated_tax_return: str, expected_xml: str, tax_year: str, fields: list[dict]) -> dict:
    """Same loop as TaxReturnEvaluator.evaluate, returning the metrics."""
    tree = etree.fromstring(expected_xml.encode("utf-8"))

    correct_count = 0
    lenient_correct_count = 0
    total_count = 0
    lines = []

    for field in fields:
        label, xpath = field["label"], field["xpath"]
        expected_value = parse_xml_value(tree, xpath)
        if tax_year == TY25:
            generated_value = parse_generated_scored_value(generated_tax_return, label)
        else:
            generated_value = parse_generated_value(generated_tax_return, label)

        is_correct = generated_value == expected_value
        is_lenient_correct = abs(generated_value - expected_value) <= 5
        correct_count += int(is_correct)
        lenient_correct_count += int(is_correct or is_lenient_correct)
        total_count += 1
        lines.append(
            {
                "line": label if tax_year == TY25 else label.split(":")[0],
                "expected": expected_value,
                "actual": generated_value,
                "correct": is_correct,
                "lenient_correct": is_correct or is_lenient_correct,
            }
        )

    return {
        "strictly_correct_return": correct_count == total_count,
        "lenient_correct_return": lenient_correct_count == total_count,
        "correct_by_line_score": correct_count / total_count if total_count > 0 else 0.0,
        "lenient_correct_by_line_score": (
            lenient_correct_count / total_count if total_count > 0 else 0.0
        ),
        "lines": lines,
    }


# ---- Harbor I/O wrapper ------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--fields", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--reward", type=Path, required=True)
    args = parser.parse_args()

    spec = json.loads(args.fields.read_text())
    if args.output.is_file():
        generated = args.output.read_text(errors="replace")
        found = True
    else:
        generated = ""
        found = False

    result = evaluate(generated, args.expected.read_text(), spec["tax_year"], spec["fields"])
    result["output_found"] = found
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    reward = 1 if (found and result["strictly_correct_return"]) else 0
    args.reward.write_text(f"{reward}\n")
    print(
        f"output_found={found} strict={result['strictly_correct_return']} "
        f"lenient={result['lenient_correct_return']} "
        f"by_line={result['correct_by_line_score']:.4f} reward={reward}"
    )


if __name__ == "__main__":
    main()
