"""Check that this adapter reproduces upstream CoT-Control-QA exactly.

Usage (from ``src/cot-control``)::

    # 1. vendored files + every prompt / answer key against the upstream functions
    uv run --with python-dotenv python -m cot_control.check_fidelity \
        --upstream-dir /path/to/CoTControl

    # 2. additionally check generated task directories and their oracle traces
    uv run --with python-dotenv python -m cot_control.check_fidelity \
        --upstream-dir /path/to/CoTControl --dataset-dir ../../datasets/cot-control

Checks
------
vendored   The files copied from upstream are byte-identical to the pinned commit.
upstream   For every dataset x instruction type, the adapter selects the same rows and
           builds the same user prompt, control value and answer letter as upstream's
           own ``load_evaluation_data`` / ``create_prompt_for_sample`` /
           ``convert_answer_to_letter`` (imported from ``--upstream-dir``).
answer     The verifier's answer extraction equals upstream's on a set of responses.
request    The request body the CoT-Control agent sends, and the way it reads the reply,
           equal upstream's ``OpenRouterLLM`` for a range of model names.
tasks      Each generated task holds exactly that prompt and configuration, Harbor-facing
           files are well formed, and the oracle trace is graded as compliant and correct
           by the task's own verifier code (rule-based instruction types; the LLM-judged
           ``ignore_question`` type needs a real run).
grader     A few thousand synthetic model outputs (compliant, violating and edge cases
           such as empty or NA-like reasoning) are graded twice: by the upstream
           ``grade_compliance_csv.py`` command line, CSV round trip included, and by the
           task verifier. Every row and the macro-averages must agree.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import os
import sys
import tomllib
from collections import Counter
from pathlib import Path
from types import ModuleType

from .adapter import CoTControlAdapter
from .constants import (
    DATASETS,
    MODES,
    UPSTREAM_COMMIT,
    UPSTREAM_FILE_SHA256,
    UPSTREAM_QA_DIR,
    task_name_for,
)
from .upstream_graders import TEMPLATE_TESTS_DIR

PACKAGE_DIR = Path(__file__).resolve().parent

# vendored copy -> upstream path
VENDORED_FILES = {
    PACKAGE_DIR / "upstream" / "prompts.py": "CoT-Control-QA/prompts.py",
    TEMPLATE_TESTS_DIR / "grading.py": "CoT-Control-QA/grading.py",
    TEMPLATE_TESTS_DIR
    / "grade_compliance_csv.py": "CoT-Control-QA/grade_compliance_csv.py",
}

ANSWER_PROBES = [
    "ANSWER: A", "answer: b", "ANSWER:C", "ANSWER:   Q", "The answer is D", "D",
    "I think (C)", "ANSWER: AB", "Final: E", "ANSWER: 3", "", "  B  ", "ANSWER: z",
    "Reasoning...\nANSWER: K\nmore text", "option A.", "A B", "no letter here", "*D*",
    "ANSWER - C", "Answer:\nF", "ANSWER: G ANSWER: H",
]  # fmt: skip


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, message: str) -> None:
        print(f"  ok    {message}")

    def fail(self, message: str) -> None:
        print(f"  FAIL  {message}")
        self.failures.append(message)

    def check(self, condition: bool, message: str) -> None:
        (self.ok if condition else self.fail)(message)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_vendored(report: Report, upstream_dir: Path | None) -> None:
    print("[vendored] files copied from upstream")
    for local, upstream_rel in VENDORED_FILES.items():
        digest = _sha256(local)
        report.check(
            digest == UPSTREAM_FILE_SHA256[upstream_rel],
            f"{local.relative_to(PACKAGE_DIR)} matches pinned sha256 of {upstream_rel}",
        )
        if upstream_dir is not None:
            report.check(
                digest == _sha256(upstream_dir / upstream_rel),
                f"{local.relative_to(PACKAGE_DIR)} is byte-identical to the checkout",
            )
    if upstream_dir is not None:
        for upstream_rel, expected in UPSTREAM_FILE_SHA256.items():
            report.check(
                _sha256(upstream_dir / upstream_rel) == expected,
                f"checkout file {upstream_rel} is at the pinned commit {UPSTREAM_COMMIT[:7]}",
            )


def _import_upstream(upstream_dir: Path) -> tuple[ModuleType, ModuleType]:
    """Import upstream ``run_cceval`` and ``llm`` from a checkout."""
    qa_dir = str(upstream_dir / UPSTREAM_QA_DIR)
    sys.path.insert(0, qa_dir)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            run_cceval = importlib.import_module("run_cceval")
            llm = importlib.import_module("llm")
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"Could not import upstream code ({exc}). Run this check with "
            "`uv run --with python-dotenv python -m cot_control.check_fidelity ...`"
        ) from exc
    finally:
        sys.path.remove(qa_dir)
    return run_cceval, llm


def check_against_upstream(
    report: Report, upstream_dir: Path, prompt_model_name: str
) -> None:
    print(f"[upstream] prompts and answer keys (model name {prompt_model_name!r})")
    run_cceval, _ = _import_upstream(upstream_dir)

    adapter = CoTControlAdapter(
        output_dir=Path(os.devnull),
        source_dir=upstream_dir,
        prompt_model_name=prompt_model_name,
    )
    ours = {
        (s.dataset.key, s.mode.name, s.row_index): s for s in adapter.build_task_specs()
    }

    total = 0
    mismatches: Counter[str] = Counter()
    for dataset in DATASETS:
        data_file = upstream_dir / UPSTREAM_QA_DIR / "datasets" / dataset.filename
        for mode in MODES:
            samples = run_cceval.load_evaluation_data(data_file, False, mode.name)
            upstream_rows = {int(s["id"]) for s in samples}
            our_rows = {
                row for (d, m, row) in ours if d == dataset.key and m == mode.name
            }
            if upstream_rows != our_rows:
                mismatches["row selection"] += 1
            for sample in samples:
                total += 1
                spec = ours.get((dataset.key, mode.name, int(sample["id"])))
                if spec is None:
                    continue
                system_prompt, user_prompt = run_cceval.create_prompt_for_sample(
                    sample,
                    mode.name,
                    prompt_model_name,
                    False,  # non_reasoning
                    "na",  # adversarial_pressure (CLI default)
                    False,  # output_no_letter_no_grading
                    False,  # monitor_aware
                    False,  # eval_aware
                    None,  # custom_adversarial_prompt
                )
                if system_prompt:
                    mismatches["non-empty system prompt"] += 1
                if user_prompt != spec.prompt:
                    mismatches["prompt"] += 1
                if (run_cceval.get_control_value(mode.name, sample) or "") != (
                    spec.control_value or ""
                ):
                    mismatches["control value"] += 1
                target = run_cceval.convert_answer_to_letter(
                    sample.get("correct_answer", ""),
                    sample.get("options", []),
                    sample["question"],
                )
                if target != spec.correct_letter:
                    mismatches["answer letter"] += 1

    report.check(
        total == len(ours),
        f"same number of instances ({total} upstream, {len(ours)} adapter)",
    )
    for kind in (
        "row selection",
        "non-empty system prompt",
        "prompt",
        "control value",
        "answer letter",
    ):
        report.check(mismatches[kind] == 0, f"{kind}: {mismatches[kind]} mismatches")


def _load_run_grader() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "cot_control_run_grader", TEMPLATE_TESTS_DIR / "run_grader.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_answer_extraction(report: Report, upstream_dir: Path) -> None:
    print(
        "[answer] verifier answer extraction vs upstream llm.extract_answer_from_response"
    )
    _, llm = _import_upstream(upstream_dir)
    run_grader = _load_run_grader()
    probes = ANSWER_PROBES + [f"ANSWER: {chr(65 + i)}" for i in range(26)]
    different = [
        probe
        for probe in probes
        if run_grader.extract_answer(probe)
        != llm.extract_answer_from_response(llm.LLMResponse(content=probe))
    ]
    report.check(
        not different, f"{len(probes)} probe responses, {len(different)} differ"
    )


def check_tasks(
    report: Report,
    dataset_dir: Path,
    source_dir: Path | None,
    split: str,
    prompt_model_name: str,
) -> None:
    print(f"[tasks] generated task directories under {dataset_dir} (split {split!r})")
    adapter = CoTControlAdapter(
        output_dir=dataset_dir,
        source_dir=source_dir,
        split=split,
        prompt_model_name=prompt_model_name,
    )
    specs = adapter.build_task_specs()
    on_disk = {p.name for p in dataset_dir.iterdir() if (p / "task.toml").is_file()}
    expected = {spec.task_id for spec in specs}
    report.check(
        on_disk == expected,
        f"task directories match the split ({len(on_disk)} on disk, {len(expected)} expected)",
    )

    os.environ["COT_CONTROL_META_DISCUSSION"] = "skip"
    os.environ.pop("OPENAI_API_KEY", None)
    run_grader = _load_run_grader()

    problems: Counter[str] = Counter()
    oracle_checked = 0
    template_hashes = {
        name: _sha256(TEMPLATE_TESTS_DIR / name)
        for name in (
            "grading.py",
            "grade_compliance_csv.py",
            "run_grader.py",
            "test.sh",
        )
    }
    for spec in specs:
        task_dir = dataset_dir / spec.task_id
        if not task_dir.is_dir():
            continue
        if (task_dir / "instruction.md").read_bytes() != spec.prompt.encode("utf-8"):
            problems["instruction.md differs from the upstream prompt"] += 1
        config = json.loads(
            (task_dir / "tests" / "config.json").read_text(encoding="utf-8")
        )
        if (
            config["prompt"] != spec.prompt
            or config["mode"] != spec.mode.name
            or config["correct_answer"] != spec.correct_letter
        ):
            problems["tests/config.json differs"] += 1
        task_toml = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        if task_toml["task"]["name"] != task_name_for(spec.task_id):
            problems["task.toml name"] += 1
        for name, digest in template_hashes.items():
            if _sha256(task_dir / "tests" / name) != digest:
                problems[f"tests/{name} differs from the template"] += 1
        for script in ("tests/test.sh", "solution/solve.sh"):
            if not os.access(task_dir / script, os.X_OK):
                problems[f"{script} not executable"] += 1

        if spec.mode.llm_judged:
            continue
        oracle = json.loads(
            (task_dir / "solution" / "oracle_trace.json").read_text(encoding="utf-8")
        )
        with contextlib.redirect_stdout(io.StringIO()):
            rewards, _ = run_grader.grade(
                config, oracle["reasoning"], oracle["response"]
            )
        oracle_checked += 1
        if rewards != {"reward": 1, "correctness": 1}:
            problems["oracle trace not graded as compliant and correct"] += 1

    for kind, count in sorted(problems.items()):
        report.fail(f"{kind}: {count} task(s)")
    if not problems:
        report.ok(
            f"{len(specs)} tasks: prompt, config, task.toml and verifier files are consistent"
        )
        report.ok(
            f"{oracle_checked} rule-based oracle traces graded reward=1, correctness=1 by the task verifier code"
        )
    n_judged = sum(1 for spec in specs if spec.mode.llm_judged)
    print(
        f"  note  {n_judged} ignore_question oracle traces need the LLM judge (not checked here)"
    )


REQUEST_PROBE_MODELS = [
    "openai/gpt-oss-120b", "openai/gpt-oss-20b", "deepseek/deepseek-r1",
    "deepseek/deepseek-r1-0528", "qwen/qwen3-32b", "qwen/qwen3-14b",
    "anthropic/claude-3.7-sonnet:thinking", "anthropic/claude-sonnet-4.5",
    "mistralai/magistral-medium-2506", "openai/gpt-4o", "openai/o3",
    "meta-llama/llama-3.3-70b-instruct", "x-ai/grok-4",
]  # fmt: skip

RESPONSE_PROBES = [
    {"choices": [{"message": {"content": "ANSWER: B", "reasoning": "think"}}]},
    {"choices": [{"message": {"content": "ANSWER: B", "reasoning_content": "rc", "reasoning": "r"}}]},
    {"choices": [{"message": {"content": "ANSWER: B"}}]},
    {"choices": [{"message": {"content": None, "reasoning": "only reasoning"}}]},
    {"choices": [{"message": {"content": "x", "reasoning": None}}]},
]  # fmt: skip


def check_request(report: Report, upstream_dir: Path) -> None:
    """The agent's request body and response parsing against upstream llm.py."""
    from . import request

    print(
        "[request] agent request body and response parsing vs upstream llm.OpenRouterLLM"
    )
    run_cceval, llm = _import_upstream(upstream_dir)
    messages = [{"role": "user", "content": "Question: 2+2?\n\nA) 3\nB) 4\n"}]

    payload_diff = []
    for model in REQUEST_PROBE_MODELS:
        # What run_cceval.run_evaluation does before calling the model.
        llm_kwargs = {"default_max_tokens": run_cceval.DEFAULT_MAX_TOKENS}
        if any(
            kw in model.lower()
            for kw in ["r1", "thinking", "qwen3", "magistral", "oss"]
        ):
            llm_kwargs["reasoning_max_tokens"] = run_cceval.DEFAULT_REASONING_TOKENS
        upstream_llm = llm.create_llm(
            model=model, backend="openrouter", api_key="unused", **llm_kwargs
        )
        expected = upstream_llm._build_payload(
            messages, run_cceval.DEFAULT_MAX_TOKENS, 0.0
        )
        if request.build_payload(model, messages) != expected:
            payload_diff.append(model)
    report.check(
        not payload_diff,
        f"request body for {len(REQUEST_PROBE_MODELS)} model names: {len(payload_diff)} differ {payload_diff or ''}".rstrip(),
    )

    upstream_llm = llm.create_llm(model="probe", backend="openrouter", api_key="unused")
    parse_diff = 0
    for probe in RESPONSE_PROBES:
        parsed = upstream_llm._parse_response(probe, 1.0)
        if request.parse_response(probe) != (parsed.content, parsed.reasoning):
            parse_diff += 1
    report.check(
        parse_diff == 0,
        f"response parsing for {len(RESPONSE_PROBES)} response shapes: {parse_diff} differ",
    )
    report.check(
        upstream_llm.base_url == request.DEFAULT_API_BASE,
        f"default endpoint is upstream's ({request.DEFAULT_API_BASE})",
    )


def _reasoning_variants(
    oracle_reasoning: str, prompt: str, control_value: str
) -> list[str]:
    """Reasoning traces that exercise compliant, violating and edge-case inputs."""
    question = prompt.split("\n\nRequirement:")[0]
    keyword = control_value.split("' or its synonyms")[0].split(",")[0].strip()
    good = oracle_reasoning
    return [
        good,
        good + " Extra sentence With Mixed Case.",
        question,
        "", "nan", "None", "   \n", "42",
        good.upper(),
        good.lower(),
        "Line one.\r\nLine two safe.\r\n" + good,
        f"{keyword} appears here. {good}" if keyword else f"{good}\n\n{good}",
        good.replace("meow ", "", 1).replace(" safe.", ".", 1).replace("<This", "This", 1),
        f"ünïcödé \U0001f600 {good} ünïcödé",
        good + "\n",
        "  " + good,
    ]  # fmt: skip


def check_grader_equivalence(
    report: Report, upstream_dir: Path, dataset_dir: Path, questions_per_pair: int = 6
) -> None:
    """Grade identical outputs with the upstream CLI and with the task verifier."""
    import subprocess
    import tempfile

    import pandas as pd

    from . import aggregate

    print(
        "[grader] upstream grade_compliance_csv.py CLI vs task verifier on identical outputs"
    )
    os.environ["COT_CONTROL_META_DISCUSSION"] = "skip"
    os.environ.pop("OPENAI_API_KEY", None)
    run_grader = _load_run_grader()

    rows: list[dict] = []
    ours: list[tuple[str, dict]] = []
    for dataset in DATASETS:
        for mode in MODES:
            if mode.llm_judged:
                continue
            # Exact 4-digit row: a bare "*" would also match e.g. "...-multiple-word-suppression".
            task_dirs = sorted(
                dataset_dir.glob(f"{dataset.key}-[0-9][0-9][0-9][0-9]-{mode.slug}")
            )
            for task_dir in task_dirs[:questions_per_pair]:
                config = json.loads(
                    (task_dir / "tests" / "config.json").read_text(encoding="utf-8")
                )
                oracle = json.loads(
                    (task_dir / "solution" / "oracle_trace.json").read_text(
                        encoding="utf-8"
                    )
                )
                variants = _reasoning_variants(
                    oracle["reasoning"], config["prompt"], config["control_value"] or ""
                )
                for index, reasoning in enumerate(variants):
                    response = oracle["response"] if index % 3 else "ANSWER: A"
                    extracted = run_grader.extract_answer(response)
                    # Same columns as upstream run_cceval.py writes.
                    rows.append(
                        {
                            "model": "check/model",
                            "dataset": dataset.filename,
                            "mode": mode.name,
                            "question": config["prompt"],
                            "reasoning": reasoning,
                            "response": response,
                            "extracted_answer": extracted or "",
                            "correct_answer": config["correct_answer"],
                            "correctness": extracted == config["correct_answer"],
                            "source": config["source"],
                            "domain": config["domain"],
                            "control_value": config["control_value"] or "",
                            "latency_ms": float(len(rows)),  # unique per record
                        }
                    )
                    with contextlib.redirect_stdout(io.StringIO()):
                        rewards, _ = run_grader.grade(config, reasoning, response)
                    ours.append((config["task_id"], rewards))
    if not rows:
        report.fail("no rule-based tasks found to compare")
        return

    with tempfile.TemporaryDirectory() as tmp:
        results_csv = Path(tmp) / "results.csv"
        graded_csv = Path(tmp) / "graded.csv"
        pd.DataFrame(rows).to_csv(
            results_csv, index=False
        )  # as upstream writes results
        qa_dir = upstream_dir / UPSTREAM_QA_DIR
        proc = subprocess.run(
            [sys.executable, str(qa_dir / "grade_compliance_csv.py"), "--input_file", str(results_csv),
             "--output_file", str(graded_csv), "--skip_meta_discussion"],
            cwd=qa_dir, capture_output=True, text=True, check=False,
        )  # fmt: skip
        if proc.returncode != 0:
            report.fail(f"upstream grading CLI failed: {proc.stderr[-400:]}")
            return
        graded = pd.read_csv(graded_csv)
        upstream_summary = aggregate.summarize(
            aggregate.load_upstream_csvs([graded_csv])
        )

        job_dir = Path(tmp) / "job"
        for index, (task_id, rewards) in enumerate(ours):
            trial_dir = job_dir / f"trial-{index:05d}"
            trial_dir.mkdir(parents=True)
            (trial_dir / "result.json").write_text(
                json.dumps(
                    {
                        "task_name": task_name_for(task_id),
                        "verifier_result": {"rewards": rewards},
                    }
                )
            )
        harbor_summary = aggregate.summarize(aggregate.load_job_dir(job_dir))

    compliance_diff = sum(
        int(up) != rewards["reward"]
        for up, (_, rewards) in zip(graded["compliance"], ours)
    )
    correctness_diff = sum(
        int(bool(up)) != rewards["correctness"]
        for up, (_, rewards) in zip(graded["correctness"], ours)
    )
    n_compliant = sum(rewards["reward"] for _, rewards in ours)
    report.check(
        len(graded) == len(ours),
        f"{len(ours)} outputs graded by both ({n_compliant} compliant)",
    )
    report.check(compliance_diff == 0, f"compliance: {compliance_diff} row(s) differ")
    report.check(
        correctness_diff == 0, f"correctness: {correctness_diff} row(s) differ"
    )
    report.check(
        upstream_summary["macro"] == harbor_summary["macro"],
        "aggregate.py gives the same macro-averages for the upstream CSV and the Harbor job "
        f"({upstream_summary['macro']['controllability']} vs {harbor_summary['macro']['controllability']})",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--upstream-dir",
        type=Path,
        default=None,
        help="Checkout of the upstream CoTControl repository at the pinned commit",
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help="Generated task directory to verify",
    )
    parser.add_argument(
        "--split", default="full", help="Split the dataset dir was generated with"
    )
    parser.add_argument(
        "--prompt-model-name",
        default="",
        help="Model name the dataset was generated for (see cot-control --help)",
    )
    args = parser.parse_args()
    if args.upstream_dir is not None:
        # The upstream grading command line is run from its own directory.
        args.upstream_dir = args.upstream_dir.resolve()

    report = Report()
    check_vendored(report, args.upstream_dir)
    if args.upstream_dir is not None:
        check_against_upstream(report, args.upstream_dir, args.prompt_model_name)
        if not args.prompt_model_name:
            # Also cover the wording upstream uses for gpt-oss models.
            check_against_upstream(report, args.upstream_dir, "openai/gpt-oss-120b")
        check_answer_extraction(report, args.upstream_dir)
        check_request(report, args.upstream_dir)
    else:
        print(
            "[upstream] skipped (pass --upstream-dir to compare against upstream code)"
        )
    if args.dataset_dir is not None:
        check_tasks(
            report,
            args.dataset_dir,
            args.upstream_dir,
            args.split,
            args.prompt_model_name,
        )
        if args.upstream_dir is not None:
            check_grader_equivalence(report, args.upstream_dir, args.dataset_dir)

    if report.failures:
        print(f"\n{len(report.failures)} check(s) FAILED")
        raise SystemExit(1)
    print("\nAll checks passed")


if __name__ == "__main__":
    main()
