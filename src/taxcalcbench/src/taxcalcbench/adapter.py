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

The only additions are a short "Input files and answer location" block after the upstream
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
# Heading of the block appended after the upstream prompt in instruction.md.
NOTES_HEADING = "## Input files and answer location"
# Hidden work folder inside the output folder while tasks are being written.
STAGING_DIR = ".staging"
_PREVIOUS = ".previous"

# Pinned upstream commit (2026-11-17 19:07 PST). Update intentionally when re-syncing.
PINNED_REF = "fdfe0db992ae9033f5b8b942633181a3596da64c"
# Upstream's CLI saves model outputs under results/, so those folders may differ.
_RESULTS_EXCLUDES = (
    ":(exclude)tax_calc_bench/ty24/results",
    ":(exclude)tax_calc_bench/ty25/results",
)


def expected_ref() -> str:
    """The upstream commit the parity tooling requires (parity agent and launcher).

    The pinned commit, unless ``TAXCALCBENCH_EXPECTED_REF`` names another full
    commit hash. The override exists for one case: a parity run with a model
    upstream's TY2025 runner does not list, which needs a fork whose only
    change widens that list. The commit used is recorded with every run.
    Task generation does not read the variable: it checks an existing checkout
    against ``PINNED_REF`` (unless ``--allow-other-ref`` is given) and clones
    the ref named by ``--ref``, which defaults to the pin.
    """
    import os

    return os.environ.get("TAXCALCBENCH_EXPECTED_REF") or PINNED_REF


def check_pinned_checkout(root: Path, ref: str | None = None) -> str:
    """Raise unless ``root`` is at the expected commit with unmodified code and test data.

    Checks HEAD and that ``git status`` reports no change outside the
    ``results/`` folders (prompts, scoring code and test data must be the
    committed ones), no file there hidden from ``git status`` by an index flag,
    and no git-ignored file inside the test data. Returns the commit hash.
    ``ref`` defaults to the pinned commit.
    """
    import subprocess

    ref = ref or PINNED_REF

    def git(*args: str) -> str:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"{root} is not a usable git checkout: {proc.stderr.strip()}")
        return proc.stdout.strip()

    head = git("rev-parse", "HEAD")
    if head != ref:
        raise RuntimeError(f"upstream checkout {root} is at {head}, expected {ref}")
    # Files a user told git to stop watching would not show up in git status
    # (same paths as the status check below: a checkout without results/ is fine).
    hidden = [line for line in git("ls-files", "-v", "--", ".", *_RESULTS_EXCLUDES).splitlines()
              if line[:1].islower() or line[:1] == "S"]
    if hidden:
        raise RuntimeError(f"upstream checkout {root} has files hidden from git status (e.g. {hidden[0]!r})")
    # Explicit options, so the user's git config cannot hide untracked files.
    status = ("-c", "core.fileMode=true", "status", "--porcelain", "--untracked-files=all")

    def changed(*args: str) -> list[str]:
        # Finder's .DS_Store folder files are never read by upstream or by the adapter.
        return [line for line in git(*status, *args).splitlines()
                if not (line[:2] in ("??", "!!") and Path(line[3:].strip('"')).name == ".DS_Store")]

    dirty = changed("--", ".", *_RESULTS_EXCLUDES)
    if dirty:
        raise RuntimeError(f"upstream checkout {root} has local changes outside results/ (e.g. {dirty[0]!r})")
    ignored = changed("--ignored", "--", *(str(d) for d in TEST_DATA_DIRS.values()))
    if ignored:
        raise RuntimeError(f"upstream checkout {root} has extra files in the test data (e.g. {ignored[0]!r})")
    return head


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
        if split not in ("full", "sanity"):
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
        for tax_year in TAX_YEARS:
            data_dir = self.benchmark_root / TEST_DATA_DIRS[tax_year]
            for case_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
                if (case_dir / "output.xml").exists():
                    cases.append(SourceCase(tax_year, case_dir.name, case_dir))
        if self.split == "sanity":
            # Chosen over all cases first, so the six cases are the same
            # whatever --tax-year is given.
            cases = sanity_subset(_seeded_sample(cases))
        cases = [c for c in cases if c.tax_year in self.tax_years]
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
        cases = self.discover()
        # Tasks are built in a hidden staging folder inside the output folder
        # (same file system, so the final move is a rename) and moved into
        # place only when complete: a failed run leaves no partial task, and
        # --overwrite keeps the old task until its replacement is ready. The
        # folder holds no task.toml of its own, so it is never read as a task.
        staging_root = self.output_dir / STAGING_DIR
        self._clear_staging(staging_root)  # left by a run that was killed
        try:
            for case in cases:
                task_dir = self.output_dir / case.task_id
                if task_dir.exists() and not self.overwrite:
                    logger.info(f"Skipping existing task {case.task_id}")
                    continue
                staging = staging_root / case.task_id
                staging.parent.mkdir(parents=True, exist_ok=True)
                self._write_task(case, staging)
                backup = None
                if task_dir.exists():
                    backup = staging_root / f"{case.task_id}{_PREVIOUS}"
                    task_dir.rename(backup)
                try:
                    staging.rename(task_dir)
                except BaseException:
                    if backup is not None:
                        backup.rename(task_dir)  # put the old task back
                    raise
                if backup is not None:
                    shutil.rmtree(backup)
                written.append(task_dir)
        finally:
            self._clear_staging(staging_root)
        return written

    def _clear_staging(self, staging_root: Path) -> None:
        """Remove the staging folder, first putting back any task it still holds.

        A ``<task>.previous`` folder is an existing task that was moved aside
        for its replacement. If the task folder is missing (the replacement
        never arrived), the old task goes back; if that fails, the staging
        folder stays and the error names it.
        """
        if not staging_root.is_dir():
            return
        for backup in sorted(staging_root.glob(f"*{_PREVIOUS}")):
            task_dir = self.output_dir / backup.name[: -len(_PREVIOUS)]
            if not task_dir.exists():
                try:
                    backup.rename(task_dir)
                except OSError as exc:
                    raise RuntimeError(
                        f"could not put the previous task back from {backup}; it is kept there"
                    ) from exc
        shutil.rmtree(staging_root)

    # ------------------------------------------------------------ generation
    def _write_task(self, case: SourceCase, task_dir: Path) -> None:
        # Only the template's own files: no Finder or Python by-products.
        shutil.copytree(TEMPLATE_DIR, task_dir, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__", "*.pyc"))

        # Inputs the agent sees, under environment/input -> /app/input.
        input_dst = task_dir / "environment" / "input"
        input_dst.mkdir(parents=True, exist_ok=True)
        if case.tax_year == TY25:
            # Exactly what upstream reads: the PDFs and remaining_data.json.
            input_dir = case.case_dir / "input"
            input_files = sorted([*input_dir.glob("*.pdf"), input_dir / "remaining_data.json"],
                                 key=lambda p: p.name)
        else:
            input_files = [case.case_dir / "input.json"]
        for src in input_files:
            shutil.copy2(src, input_dst / src.name)

        # Instruction = upstream prompt + the input-files / answer-location notes.
        prompt = self._upstream_prompt(case)
        notes = _task_notes(case, [p.name for p in input_files])
        (task_dir / "instruction.md").write_text(f"{prompt}\n{notes}", encoding="utf-8")

        # Verifier data.
        fields = self._scored_fields(case)
        tests = task_dir / "tests"
        shutil.copy2(case.case_dir / "output.xml", tests / "expected.xml")
        # Upstream's licence and its evaluation-only notice travel with every
        # copy of its data (verifier side only; the agent never sees tests/).
        shutil.copy2(self.benchmark_root / "LICENSE", tests / "UPSTREAM_LICENSE")
        shutil.copy2(self.benchmark_root / "CANARY.txt", tests / "UPSTREAM_NOTICE.txt")
        (tests / "scored_fields.json").write_text(
            json.dumps({"tax_year": case.tax_year, "fields": fields}, indent=2) + "\n", encoding="utf-8"
        )

        # Oracle: render every scored line from the expected return.
        oracle = _render_oracle(case, fields, (case.case_dir / "output.xml").read_text(encoding="utf-8"))
        (task_dir / "solution" / "oracle_return.txt").write_text(oracle, encoding="utf-8")

        # task.toml placeholders.
        toml_path = task_dir / "task.toml"
        toml_path.write_text(
            toml_path.read_text(encoding="utf-8")
            .replace("{task_id}", case.task_id)
            .replace("{form_name}", _form_name(case))
            .replace("{input_tag}", "pdf" if case.tax_year == TY25 else "json"),
            encoding="utf-8",
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
                (input_dir / "remaining_data.json").read_text(encoding="utf-8"),
                [p.name for p in pdfs],
                case.source_id,
            )
        # Upstream (run_tax_return_test) loads input.json and passes
        # json.dumps(...) of it into the TY24 template, with an empty
        # tool_use_hint when web search is off.
        with open(case.case_dir / "input.json", encoding="utf-8") as f:
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
SAMPLE_SEED = 42
SAMPLE_TY25_PER_JURISDICTION = 2
SAMPLE_TY24 = 10


def _seeded_sample(cases: list[SourceCase]) -> list[SourceCase]:
    """Fixed sample the sanity cases are taken from.

    Two TY25 cases per jurisdiction (us, ca, il, ny, va) and ten TY24 cases,
    each drawn with ``random.Random(42)`` from the sorted case names, so the
    sample is the same on every run and on both parity sides.
    """
    import random

    rng = random.Random(SAMPLE_SEED)
    picked: list[SourceCase] = []
    ty25 = sorted((c for c in cases if c.tax_year == TY25), key=lambda c: c.source_id)
    for jurisdiction in ("us", "ca", "il", "ny", "va"):
        pool = [c for c in ty25 if c.source_id.split("-")[1] == jurisdiction]
        picked += rng.sample(pool, min(SAMPLE_TY25_PER_JURISDICTION, len(pool)))
    ty24 = sorted((c for c in cases if c.tax_year == TY24), key=lambda c: c.source_id)
    picked += rng.sample(ty24, min(SAMPLE_TY24, len(ty24)))
    order = {id(c): i for i, c in enumerate(cases)}
    return sorted(picked, key=lambda c: order[id(c)])


def sanity_subset(sample: list[SourceCase]) -> list[SourceCase]:
    """Six cases for the pre-run sanity check on both sides.

    From the seeded sample: the first case of three TY25 jurisdictions
    (us, ca, ny) and the first three TY24 cases, so both input formats are
    exercised.
    """
    picked: list[SourceCase] = []
    for jurisdiction in ("us", "ca", "ny"):
        picked.append(next(c for c in sample
                           if c.tax_year == TY25 and c.source_id.split("-")[1] == jurisdiction))
    picked += [c for c in sample if c.tax_year == TY24][:3]
    order = {id(c): i for i, c in enumerate(sample)}
    return sorted(picked, key=lambda c: order[id(c)])


def _load_upstream(root: Path) -> dict:
    """Import the prompt and scoring definitions from the upstream checkout."""
    if not (root / "tax_calc_bench").is_dir():
        raise FileNotFoundError(f"Not a TaxCalcBench checkout: {root}")
    loaded = sys.modules.get("tax_calc_bench")
    if loaded is not None and not _module_under(loaded, root):
        # Python keeps the first checkout's modules; a second one would be ignored.
        raise RuntimeError(
            f"tax_calc_bench is already loaded from {getattr(loaded, '__file__', '?')}; "
            f"use one upstream checkout per process (asked for {root})"
        )
    sys.path.insert(0, str(root))
    try:
        import tax_calc_bench
        from tax_calc_bench.config import TAX_YEAR, jurisdiction_from_test_name
        from tax_calc_bench.tax_return_evaluator import LINES_TO_XPATH_VALUES
        from tax_calc_bench.ty24_prompt import TAX_RETURN_GENERATION_PROMPT
        from tax_calc_bench.ty25_prompt import build_ty25_tax_return_prompt
        from tax_calc_bench.ty25_scoring import get_ty25_scoring_fields
    finally:
        sys.path.remove(str(root))
    if not _module_under(tax_calc_bench, root):
        raise RuntimeError(f"tax_calc_bench was imported from {tax_calc_bench.__file__}, not from {root}")
    return {
        "TAX_YEAR": TAX_YEAR,
        "jurisdiction_from_test_name": jurisdiction_from_test_name,
        "LINES_TO_XPATH_VALUES": LINES_TO_XPATH_VALUES,
        "TAX_RETURN_GENERATION_PROMPT": TAX_RETURN_GENERATION_PROMPT,
        "build_ty25_tax_return_prompt": build_ty25_tax_return_prompt,
        "get_ty25_scoring_fields": get_ty25_scoring_fields,
    }


def _module_under(module, root: Path) -> bool:
    file = getattr(module, "__file__", None)
    return bool(file) and Path(file).resolve().is_relative_to(root.resolve())


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


def _task_notes(case: SourceCase, input_names: list[str]) -> str:
    files = "\n".join(f"- `{INPUT_DIR}/{name}`" for name in input_names)
    if case.tax_year == TY25:
        where = (
            f"The attached PDFs and remaining_data.json are in `{INPUT_DIR}/`:\n{files}"
        )
    else:
        where = f"The taxpayer data above is also saved as:\n{files}"
    return (
        f"{NOTES_HEADING}\n\n"
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
