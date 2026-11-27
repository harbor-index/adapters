"""CLI: generate Harbor tasks for TaxCalcBench.

    uv run taxcalcbench --output-dir ../../datasets/taxcalcbench
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from .adapter import PINNED_REF, TAX_YEARS, TaxCalcBenchAdapter, check_pinned_checkout

TAXCALCBENCH_REPO_URL = "https://github.com/column-tax/tax-calc-bench.git"
TAXCALCBENCH_REF = PINNED_REF

# <repo>/src/taxcalcbench/src/taxcalcbench/main.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[4]

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _clone(temp_dir: Path, ref: str) -> Path:
    repo = temp_dir / "tax-calc-bench"
    logger.info(f"Cloning {TAXCALCBENCH_REPO_URL} at {ref} ...")
    subprocess.run(["git", "clone", "-q", TAXCALCBENCH_REPO_URL, str(repo)], check=True)
    subprocess.run(["git", "checkout", "-q", ref], cwd=repo, check=True)
    return repo


def _non_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate Harbor tasks for TaxCalcBench")
    p.add_argument("--output-dir", type=Path, default=REPO_ROOT / "datasets" / "taxcalcbench")
    p.add_argument("--taxcalcbench-root", type=Path, default=None,
                   help="Existing tax-calc-bench checkout (default: clone the pinned ref).")
    p.add_argument("--ref", default=TAXCALCBENCH_REF, help="Git ref to clone.")
    p.add_argument("--allow-other-ref", action="store_true",
                   help="Skip the check that --taxcalcbench-root is the pinned commit with no local changes.")
    p.add_argument("--tax-year", choices=[*TAX_YEARS, "all"], default="all",
                   help="ty25 (50 PDF cases), ty24 (51 JSON cases) or all (default).")
    p.add_argument("--split", choices=["full", "sanity"], default="full",
                   help="full (default); sanity: six fixed cases for the pre-run check.")
    p.add_argument("--task-ids", nargs="+", default=None,
                   help="Task ids (e.g. ty25-ca-001) or upstream case names.")
    p.add_argument("--limit", type=_non_negative, default=None, help="Generate only the first N tasks.")
    p.add_argument("--overwrite", action="store_true", help="Regenerate existing task dirs.")
    return p.parse_args()


def _generate(args: argparse.Namespace, root: Path) -> None:
    years = TAX_YEARS if args.tax_year == "all" else (args.tax_year,)
    adapter = TaxCalcBenchAdapter(
        output_dir=args.output_dir,
        benchmark_root=root,
        task_ids=args.task_ids,
        limit=args.limit,
        overwrite=args.overwrite,
        tax_years=years,
        split=args.split,
    )
    written = adapter.run()
    logger.info(f"Generated {len(written)} task(s) under {args.output_dir}")


def main() -> None:
    args = _parse_args()
    try:
        if args.taxcalcbench_root is not None:
            if args.allow_other_ref:
                logger.warning("--allow-other-ref: not checking the upstream checkout against the pin")
            else:
                check_pinned_checkout(args.taxcalcbench_root, PINNED_REF)
            _generate(args, args.taxcalcbench_root)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                _generate(args, _clone(Path(tmp), args.ref))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Task generation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
