"""Generate Harbor tasks from Samaya AI's FrontierFinance benchmark.

Each FrontierFinance query becomes one Harbor task. The agent researches the
query as of its ``query_date`` and writes a financial report to
``/app/report.md``. The verifier grades that report against the query's
expert-written rubrics with the upstream ``frontier-finance`` grader (LLM judge
panel, majority vote), unchanged. The task reward is the query's rubric
qualification rate (qualified rubrics / total rubrics), so the Harbor mean over
tasks equals the upstream ``macro_avg_qualification_rate_on_all_queries``.

The dataset is gated on Hugging Face (``samaya-ai/FrontierFinance``). Accept the
terms on the dataset page and log in (``hf auth login`` or ``HF_TOKEN``) before
running the adapter, or pass a local copy with ``--data-path``.
"""

from __future__ import annotations

import json
import math
import random
import re
import shutil
import sys
from pathlib import Path

HF_REPO_ID = "samaya-ai/FrontierFinance"
HF_FILENAME = "frontier_finance_public.jsonl"
# Dataset commit the adapter was built and validated against.
HF_REVISION = "36afd6dc1cbb2c4512536daf917dc28b1225a720"

TEMPLATE_DIR = Path(__file__).parent / "task-template"
TASK_PREFIX = "frontier-finance-"
# Parity subset: 5% of the 220 queries (see the parity plan on the PR), stratified by
# use case (each query has exactly one), sampled with a fixed seed.
PARITY_SIZE = 11
PARITY_SEED = 0

# Upstream grader settings (eval.example.yaml): three judges, 30 rubrics per call,
# 120 s request timeout, 2 SDK retries. Calls run sequentially, so the verifier
# budget is every call timing out on every attempt, plus a fixed margin.
JUDGE_PANEL_SIZE = 3
MAX_RUBRICS_PER_CALL = 30
JUDGE_REQUEST_TIMEOUT_SEC = 120
JUDGE_ATTEMPTS_PER_CALL = 3
VERIFIER_MARGIN_SEC = 600
MIN_VERIFIER_TIMEOUT_SEC = 1800

# Leading phrases stripped when turning a rubric into a report sentence for the
# oracle report. Only used to make the oracle report read naturally; grading is
# unaffected.
_LEADING_PHRASES = re.compile(
    r"^(states|indicates|mentions|notes|explains|confirms|reports|highlights)"
    r"\s+that\s+",
    re.IGNORECASE,
)


def load_queries(data_path: Path | None = None) -> list[dict]:
    """Load FrontierFinance queries from a local JSONL file or Hugging Face."""
    if data_path is None:
        from huggingface_hub import hf_hub_download

        data_path = Path(
            hf_hub_download(
                repo_id=HF_REPO_ID,
                filename=HF_FILENAME,
                repo_type="dataset",
                revision=HF_REVISION,
            )
        )
    queries = []
    with open(data_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                queries.append(json.loads(line))
    return queries


def task_id_for(query_id: str) -> str:
    """Stable, registry-safe local task id derived from the upstream query_id."""
    safe = re.sub(r"[^a-z0-9-]+", "-", str(query_id).lower()).strip("-")
    return f"{TASK_PREFIX}{safe}"


def verifier_timeout_sec(num_rubrics: int) -> int:
    """Verifier timeout sized to the number of sequential judge calls."""
    calls = JUDGE_PANEL_SIZE * math.ceil(num_rubrics / MAX_RUBRICS_PER_CALL)
    per_call = JUDGE_REQUEST_TIMEOUT_SEC * JUDGE_ATTEMPTS_PER_CALL
    return max(MIN_VERIFIER_TIMEOUT_SEC, calls * per_call + VERIFIER_MARGIN_SEC)


def build_oracle_report(query: dict) -> str:
    """Build a reference report that states each rubric's content.

    FrontierFinance ships rubrics but no reference answers, so the oracle report
    is assembled from the rubrics themselves. Negative constraints ("Does not
    ...") are satisfied by omission and are left out.
    """
    lines = [
        "# Financial Research Report",
        "",
        f"**Query:** {' '.join(query['query'].split())}",
        "",
        f"**As of:** {query['query_date']}",
        "",
        "## Findings",
        "",
    ]
    for rubric in query["rubrics"]:
        text = " ".join(str(rubric["rubric_text"]).split())
        if text.lower().startswith(("does not", "do not", "doesn't")):
            continue
        text = _LEADING_PHRASES.sub("", text)
        if text:
            text = text[0].upper() + text[1:]
        lines.append(f"- {text}")
    lines.append("")
    return "\n".join(lines)


def parity_subset(queries: list[dict]) -> list[dict]:
    """PARITY_SIZE queries, allocated to use cases by largest remainder, then sampled."""
    strata: dict[str, list[dict]] = {}
    for q in sorted(queries, key=lambda q: str(q["query_id"])):
        strata.setdefault(q["use_cases"][0], []).append(q)
    quota = {k: PARITY_SIZE * len(v) / len(queries) for k, v in strata.items()}
    counts = {k: math.floor(v) for k, v in quota.items()}
    for k in sorted(quota, key=lambda k: (counts[k] - quota[k], k))[: PARITY_SIZE - sum(counts.values())]:
        counts[k] += 1
    rng = random.Random(PARITY_SEED)
    return [q for k in sorted(strata) for q in rng.sample(strata[k], counts[k])]


class FrontierFinanceAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        data_path: Path | None = None,
        split: str = "full",
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.data_path = Path(data_path) if data_path else None
        self.split = split

    def _selected(self, queries: list[dict]) -> list[dict]:
        queries = sorted(queries, key=lambda q: str(q["query_id"]))
        if self.split == "parity":
            queries = sorted(parity_subset(queries), key=lambda q: str(q["query_id"]))
        if self.task_ids:
            wanted = set(self.task_ids)
            queries = [
                q
                for q in queries
                if str(q["query_id"]) in wanted or task_id_for(q["query_id"]) in wanted
            ]
            known = {str(q["query_id"]) for q in queries} | {task_id_for(q["query_id"]) for q in queries}
            unknown = sorted(wanted - known)
            if unknown:
                print(f"Warning: unknown task id(s) ignored: {', '.join(unknown)}", file=sys.stderr)
        if self.limit is not None:
            queries = queries[: self.limit]
        return queries

    def run(self) -> None:
        queries = self._selected(load_queries(self.data_path))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        written = skipped = 0
        for query in queries:
            task_dir = self.output_dir / task_id_for(query["query_id"])
            if task_dir.exists():
                if not self.overwrite:
                    skipped += 1
                    continue
                shutil.rmtree(task_dir)
            self._write_task(task_dir, query)
            written += 1
        print(
            f"Wrote {written} task(s) to {self.output_dir}"
            + (f" (skipped {skipped} existing; use --overwrite)" if skipped else "")
        )

    def _write_task(self, task_dir: Path, query: dict) -> None:
        shutil.copytree(TEMPLATE_DIR, task_dir)
        query_id = str(query["query_id"])

        # task.toml
        toml_path = task_dir / "task.toml"
        toml_path.write_text(
            toml_path.read_text(encoding="utf-8")
            .replace("{task_id}", task_id_for(query_id))
            .replace("{query_id}", query_id)
            .replace("{use_cases}", json.dumps(query.get("use_cases") or []))
            .replace("{capabilities}", json.dumps(query.get("capabilities") or []))
            .replace("{verifier_timeout_sec}", f"{verifier_timeout_sec(len(query['rubrics']))}.0"),
            encoding="utf-8",
        )

        # instruction.md
        instr_path = task_dir / "instruction.md"
        instr_path.write_text(
            instr_path.read_text(encoding="utf-8")
            .replace("{query_date}", str(query["query_date"]))
            .replace("{query}", query["query"].strip()),
            encoding="utf-8",
        )

        # tests/: one-line rubrics file in the upstream JSONL format.
        tests_dir = task_dir / "tests"
        record = {
            k: query[k]
            for k in ("query_id", "query", "query_date", "use_cases", "capabilities", "rubrics")
            if k in query
        }
        (tests_dir / "rubrics.jsonl").write_text(
            json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        # Oracle report: written by solve.sh; a byte-identical copy lives in
        # tests/ so the verifier can recognise the oracle without a judge call.
        report = build_oracle_report(query)
        (task_dir / "solution" / "oracle_report.md").write_text(report, encoding="utf-8")
        (tests_dir / "oracle_report.md").write_text(report, encoding="utf-8")
