"""Generate Harbor tasks from TaxCalcBench (column-tax/tax-calc-bench).

Each upstream test case becomes one Harbor task:

- TY25 cases (``tax_calc_bench/ty25/test_data/ty25-<jurisdiction>-<nnn>``): the
  taxpayer's documents are PDFs plus ``remaining_data.json``; the agent prepares
  one return (federal Form 1040 or one state return).
- TY24 cases (``tax_calc_bench/ty24/test_data/<name>``): the taxpayer data is a
  single ``input.json``; the agent prepares federal Form 1040.

What is taken from upstream unchanged (imported from the pinned checkout at
generation time, never retyped):

- the prompt text (``build_ty25_tax_return_prompt`` /
  ``TAX_RETURN_GENERATION_PROMPT``), including the per-case Virginia notes;
- the scored lines and their XPaths (``get_ty25_scoring_fields`` /
  ``LINES_TO_XPATH_VALUES``), written to ``tests/scored_fields.json``;
- the expected return ``output.xml``, copied to ``tests/expected.xml``.

The only additions are a short "Harbor task notes" block after the upstream
prompt (where the input files are and where to write the answer), and the
oracle, which renders each scored line from ``output.xml`` in the upstream
output format.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "task-template"
TY25 = "ty25"
TY24 = "ty24"
TAX_YEARS = (TY25, TY24)
TEST_DATA_DIRS = {
    TY25: Path("tax_calc_bench/ty25/test_data"),
    TY24: Path("tax_calc_bench/ty24/test_data"),
}
OUTPUT_PATH = "/app/output/return.txt"
INPUT_DIR = "/app/input"


@dataclass(frozen=True)
class SourceCase:
    tax_year: str
    source_id: str
    case_dir: Path

    @property
    def task_id(self) -> str:
        """Stable, registry-safe task id: ``ty25-ca-001`` / ``ty24-<name>``."""
        raw = self.source_id if self.tax_year == TY25 else f"{TY24}-{self.source_id}"
        return re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")


class TaxCalcBenchAdapter:
    """Writes one Harbor task directory per TaxCalcBench test case."""

    def __init__(
        self,
        output_dir: Path,
        benchmark_root: Path,
        task_ids: list[str] | None = None,
        limit: int | None = None,
        overwrite: bool = False,
        tax_years: tuple[str, ...] = TAX_YEARS,
        split: str = "full",
    ) -> None:
        if split not in ("full", "parity"):
            raise ValueError(f"Unknown split: {split}")
        self.output_dir = Path(output_dir)
        self.benchmark_root = Path(benchmark_root).resolve()
        self.task_ids = task_ids
        self.limit = limit
        self.overwrite = overwrite
        self.tax_years = tax_years
        self.split = split
        self._upstream = _load_upstream(self.benchmark_root)

    # ------------------------------------------------------------------ cases
    def discover(self) -> list[SourceCase]:
        cases: list[SourceCase] = []
        for tax_year in self.tax_years:
            data_dir = self.benchmark_root / TEST_DATA_DIRS[tax_year]
            for case_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
                if (case_dir / "output.xml").exists():
                    cases.append(SourceCase(tax_year, case_dir.name, case_dir))
        if self.split == "parity":
            cases = parity_subset(cases)
        if self.task_ids:
            wanted = set(self.task_ids)
            known = {c.task_id for c in cases} | {c.source_id for c in cases}
            unknown = sorted(wanted - known)
            if unknown:
                raise ValueError(f"Unknown task ids: {', '.join(unknown)}")
            cases = [c for c in cases if c.task_id in wanted or c.source_id in wanted]
        if self.limit is not None:
            cases = cases[: self.limit]
        return cases

    def run(self) -> list[Path]:
        written: list[Path] = []
        for case in self.discover():
            task_dir = self.output_dir / case.task_id
            if task_dir.exists():
                if not self.overwrite:
                    logger.info(f"Skipping existing task {case.task_id}")
                    continue
                shutil.rmtree(task_dir)
            # Build in a staging folder and move it into place only when the
            # task is complete, so a failed run never leaves a partial task.
            staging = self.output_dir / f".staging-{case.task_id}"
            if staging.exists():
                shutil.rmtree(staging)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._write_task(case, staging)
                staging.rename(task_dir)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            written.append(task_dir)
        return written

    # ------------------------------------------------------------ generation
    def _write_task(self, case: SourceCase, task_dir: Path) -> None:
        shutil.copytree(TEMPLATE_DIR, task_dir)

        # Inputs the agent sees, under environment/input -> /app/input.
        input_dst = task_dir / "environment" / "input"
        input_dst.mkdir(parents=True, exist_ok=True)
        if case.tax_year == TY25:
            src_inputs = sorted((case.case_dir / "input").iterdir())
            input_files = [p for p in src_inputs if p.is_file()]
        else:
            input_files = [case.case_dir / "input.json"]
        for src in input_files:
            shutil.copy2(src, input_dst / src.name)

        # Instruction = upstream prompt + Harbor task notes.
        prompt = self._upstream_prompt(case)
        notes = _harbor_notes(case, [p.name for p in input_files])
        (task_dir / "instruction.md").write_text(f"{prompt}\n{notes}")

        # Verifier data.
        fields = self._scored_fields(case)
        tests = task_dir / "tests"
        shutil.copy2(case.case_dir / "output.xml", tests / "expected.xml")
        (tests / "scored_fields.json").write_text(
            json.dumps({"tax_year": case.tax_year, "fields": fields}, indent=2) + "\n"
        )

        # Oracle: render every scored line from the expected return.
        oracle = _render_oracle(case, fields, (case.case_dir / "output.xml").read_text())
        (task_dir / "solution" / "oracle_return.txt").write_text(oracle)

        # task.toml placeholders.
        toml_path = task_dir / "task.toml"
        toml_path.write_text(
            toml_path.read_text()
            .replace("{task_id}", case.task_id)
            .replace("{form_name}", _form_name(case))
        )
        for script in (task_dir / "solution" / "solve.sh", tests / "test.sh"):
            script.chmod(0o755)

    def _upstream_prompt(self, case: SourceCase) -> str:
        up = self._upstream
        if case.tax_year == TY25:
            input_dir = case.case_dir / "input"
            pdfs = sorted(input_dir.glob("*.pdf"))
            jurisdiction = up["jurisdiction_from_test_name"](case.source_id)
            return up["build_ty25_tax_return_prompt"](
                jurisdiction,
                (input_dir / "remaining_data.json").read_text(),
                [p.name for p in pdfs],
                case.source_id,
            )
        # Upstream (run_tax_return_test) loads input.json and passes
        # json.dumps(...) of it into the TY24 template, with an empty
        # tool_use_hint when web search is off.
        with open(case.case_dir / "input.json") as f:
            input_data = json.dumps(json.load(f))
        return up["TAX_RETURN_GENERATION_PROMPT"].format(
            tax_year=up["TAX_YEAR"], tool_use_hint="", input_data=input_data
        )

    def _scored_fields(self, case: SourceCase) -> list[dict[str, str]]:
        up = self._upstream
        if case.tax_year == TY25:
            jurisdiction = up["jurisdiction_from_test_name"](case.source_id)
            return [
                {"label": f.label, "xpath": f.xpath}
                for f in up["get_ty25_scoring_fields"](jurisdiction)
            ]
        return [
            {"label": label, "xpath": xpath}
            for label, xpath in up["LINES_TO_XPATH_VALUES"].items()
        ]


# ---------------------------------------------------------------------- helpers
PARITY_SEED = 42
PARITY_TY25_PER_JURISDICTION = 2
PARITY_TY24 = 10


def parity_subset(cases: list[SourceCase]) -> list[SourceCase]:
    """Fixed 20-case parity subset.

    Two TY25 cases per jurisdiction (us, ca, il, ny, va) and ten TY24 cases,
    each drawn with ``random.Random(42)`` from the sorted case names, so the
    subset is the same on every run and on both parity sides.
    """
    import random

    rng = random.Random(PARITY_SEED)
    picked: list[SourceCase] = []
    ty25 = sorted((c for c in cases if c.tax_year == TY25), key=lambda c: c.source_id)
    for jurisdiction in ("us", "ca", "il", "ny", "va"):
        pool = [c for c in ty25 if c.source_id.split("-")[1] == jurisdiction]
        picked += rng.sample(pool, min(PARITY_TY25_PER_JURISDICTION, len(pool)))
    ty24 = sorted((c for c in cases if c.tax_year == TY24), key=lambda c: c.source_id)
    picked += rng.sample(ty24, min(PARITY_TY24, len(ty24)))
    order = {id(c): i for i, c in enumerate(cases)}
    return sorted(picked, key=lambda c: order[id(c)])


def _load_upstream(root: Path) -> dict:
    """Import the prompt and scoring definitions from the upstream checkout."""
    if not (root / "tax_calc_bench").is_dir():
        raise FileNotFoundError(f"Not a TaxCalcBench checkout: {root}")
    sys.path.insert(0, str(root))
    try:
        from tax_calc_bench.config import TAX_YEAR, jurisdiction_from_test_name
        from tax_calc_bench.tax_return_evaluator import LINES_TO_XPATH_VALUES
        from tax_calc_bench.ty24_prompt import TAX_RETURN_GENERATION_PROMPT
        from tax_calc_bench.ty25_prompt import build_ty25_tax_return_prompt
        from tax_calc_bench.ty25_scoring import get_ty25_scoring_fields
    finally:
        sys.path.remove(str(root))
    return {
        "TAX_YEAR": TAX_YEAR,
        "jurisdiction_from_test_name": jurisdiction_from_test_name,
        "LINES_TO_XPATH_VALUES": LINES_TO_XPATH_VALUES,
        "TAX_RETURN_GENERATION_PROMPT": TAX_RETURN_GENERATION_PROMPT,
        "build_ty25_tax_return_prompt": build_ty25_tax_return_prompt,
        "get_ty25_scoring_fields": get_ty25_scoring_fields,
    }


_FORM_NAMES = {
    "us": "federal Form 1040",
    "ca": "California Form 540",
    "il": "Illinois Form IL-1040",
    "ny": "New York Form IT-201",
    "va": "Virginia Form 760",
}


def _form_name(case: SourceCase) -> str:
    if case.tax_year == TY24:
        return "federal Form 1040 (tax year 2024)"
    return f"{_FORM_NAMES[case.source_id.split('-')[1]]} (tax year 2025)"


def _harbor_notes(case: SourceCase, input_names: list[str]) -> str:
    files = "\n".join(f"- `{INPUT_DIR}/{name}`" for name in input_names)
    if case.tax_year == TY25:
        where = (
            f"The attached PDFs and remaining_data.json are in `{INPUT_DIR}/`:\n{files}"
        )
    else:
        where = f"The taxpayer data above is also saved as:\n{files}"
    return (
        "## Harbor task notes\n\n"
        f"{where}\n\n"
        f"Write your final output, in exactly the format described above, to "
        f"`{OUTPUT_PATH}`. Only that file is graded.\n"
    )


def _expected_value(tree, xpath: str) -> float:
    """Same as upstream ``TaxReturnEvaluator.parse_xml_value``."""
    from lxml import etree

    result = tree.xpath(xpath)
    if isinstance(result, (int, float)) and not isinstance(result, bool):
        return float(result) if math.isfinite(result) else 0.0
    if isinstance(result, list) and result:
        value = result[0]
        raw = value.text if isinstance(value, etree._Element) else str(value)
        return _parse_money(raw or "")
    if isinstance(result, str):
        return _parse_money(result)
    return 0.0


def _parse_money(text: str) -> float:
    cleaned = text.replace("$", "").replace(",", "").strip()
    if not cleaned:
        return 0.0
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = f"-{cleaned[1:-1]}"
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _format_amount(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(value)


def _render_oracle(case: SourceCase, fields: list[dict[str, str]], expected_xml: str) -> str:
    from lxml import etree

    tree = etree.fromstring(expected_xml.encode("utf-8"))
    lines = [_form_name(case), "=================="]
    for field in fields:
        amount = _format_amount(_expected_value(tree, field["xpath"]))
        lines.append(f"{field['label']} | from the reference return | {amount}")
    return "\n".join(lines) + "\n"
