#!/usr/bin/env python3
"""Original side of the LatchBio parity experiment.

Runs the adapted evaluations with LatchBio's own harness (latch-eval-tools:
``EvalRunner`` + ``run_claudecode_task``) on the exact eval files, container
image and Claude Code release the Harbor tasks are built from, and records one
pass/fail per evaluation.

Requirements: Docker able to run linux/amd64 images, Latch credentials (the
harness downloads evaluation data with the Latch SDK), and Claude credentials:
either ``ANTHROPIC_API_KEY`` (optionally with ``ANTHROPIC_BASE_URL``) or a
Claude Code subscription token in ``CLAUDE_CODE_OAUTH_TOKEN``
(``claude setup-token``).

    cd src/latchbio
    export CLAUDE_CODE_OAUTH_TOKEN=...           # or ANTHROPIC_API_KEY=...
    uv run --extra parity \\
        python parity/run_original.py --model claude-haiku-5-5 \\
        --run-name run1 --output-dir ../../parity-experiments/latchbio/original_parity

The defaults are the Harbor tasks' settings for all 44 evaluations, i.e. the
run setup LatchBio documents (``--split full --eval-timeout 21600 --memory-gb 32
--cpus 4``); the harness's own defaults differ, see the README. The reduced-resource
replication used ``--split small-data --eval-timeout 1800 --memory-gb 6``.

Settings that are aligned with the Harbor side (parity/run_latchbio_parity.yaml):

- eval files:      pinned upstream commits from ``latchbio.sources``
- container image: ``latchbio.sources.AGENT_IMAGE`` (pinned digest; ships Claude Code 2.1.174)
- prompt:          task + data instructions + Claude Code background-process note
- CLI flags:       ``--effort <effort> --thinking-display summarized``, permissions bypassed
- environment:     BASH_DEFAULT_TIMEOUT_MS / BASH_MAX_TIMEOUT_MS = timeout; same CPU and
                   memory limits as the Harbor run (--cpus / --memory-gb)
- single attempt:  the harness's automatic re-invocations (restart after an
                   out-of-memory kill, resume after a provider error) are switched
                   off, because Harbor's claude-code agent invokes the CLI exactly
                   once. Pass ``--harness-retries`` to keep upstream's defaults.
                   (Upstream's "you ended your turn without an answer" re-prompt
                   only exists in its hosted benchmark mode, never in this path.)

Evals with a top-level ``graders`` list are not graded by upstream's public
``EvalRunner`` (it only reads ``grader``); they are graded here with upstream's
own ``grade_multiple_graders_single_answer`` and pass when every grader passes,
which is the rule the Harbor verifier applies.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER_ROOT / "src"))

from latchbio.adapter import LatchBioAdapter  # noqa: E402
from latchbio.data import DataManifest  # noqa: E402
from latchbio.sources import AGENT_IMAGE, LATCH_EVAL_TOOLS_VERSION  # noqa: E402
from latchbio.upstream import UpstreamEval, ensure_checkout  # noqa: E402

WORKSPACE_NAME = ".latchbio_parity"
GIB = 1024**3


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def configure_harness(args: argparse.Namespace) -> None:
    """Align latch-eval-tools' Claude Code harness with Harbor's single invocation."""
    import latch_eval_tools
    from importlib.metadata import version
    from latch_eval_tools.harness import _cli_runner

    installed = version("latch-eval-tools")
    if installed != LATCH_EVAL_TOOLS_VERSION:
        raise SystemExit(
            f"latch-eval-tools {installed} is installed; the adapter is pinned to "
            f"{LATCH_EVAL_TOOLS_VERSION}. Run with `uv run --extra parity` from src/latchbio."
        )
    del latch_eval_tools

    if not args.harness_retries:
        _cli_runner.MAX_CLAUDECODE_ANSWER_RESUMES = 0
        _cli_runner.MAX_OOM_RESTARTS = 0

    if args.effort != "max":
        build = _cli_runner._build_agent_command

        def build_with_effort(*a: Any, **kw: Any) -> list[str]:
            command = build(*a, **kw)
            if "--effort" in command:
                index = command.index("--effort")
                if args.effort == "none":
                    del command[index : index + 2]
                else:
                    command[index + 1] = args.effort
            return command

        _cli_runner._build_agent_command = build_with_effort

    # Container limits. The harness only sets `--memory X --memory-swap X` (no
    # swap) and no CPU limit. Harbor's Docker environment sets a CPU limit and a
    # memory limit without a swap limit, which Docker treats as "swap equal to
    # memory". The extra `docker create` flags below reproduce exactly that (a
    # repeated flag overrides the earlier value).
    mounts = _cli_runner.get_agent_workspace_mount_args
    extra_create_args = ["--memory-swap", str(2 * int(args.memory_gb * GIB))]
    if args.cpus:
        extra_create_args += ["--cpus", str(args.cpus)]

    def mounts_with_limits(agent_dir: Path) -> list[str]:
        return [*mounts(agent_dir), *extra_create_args]

    _cli_runner.get_agent_workspace_mount_args = mounts_with_limits

    # The harness forwards only ANTHROPIC_API_KEY into the container; name the
    # extra variables the parity setup relies on.
    extra = set(json.loads(os.environ.get("EXTRA_ENV_KEYS") or "[]"))
    if args.auth == "oauth":
        # Subscription auth: forward the OAuth token instead of an API key.
        # run_claudecode_task insists on ANTHROPIC_API_KEY being set in this
        # process, so give it a placeholder that is never sent to the container
        # (Claude Code would otherwise prefer it over the token).
        _cli_runner.ANTHROPIC_ENV_KEYS = set()
        os.environ["ANTHROPIC_API_KEY"] = "unused-oauth-token-auth"
        extra.add("CLAUDE_CODE_OAUTH_TOKEN")
    os.environ.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    extra.add("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")
    if os.environ.get("ANTHROPIC_BASE_URL"):
        extra.add("ANTHROPIC_BASE_URL")
    os.environ["EXTRA_ENV_KEYS"] = json.dumps(sorted(extra))


PINNED_NAME_FALLBACKS: set[str] = set()


def install_pinned_name_fallback() -> None:
    """Keep staging already-cached data when the Latch API is unreachable.

    Before every evaluation the harness asks the Latch API for each data node's
    file name (``LPath(uri).name()``), even when the node's bytes are already in
    its download cache. If that call fails, answer with the name pinned in the
    adapter's ``data_manifest.json`` (recorded from the same API call). Nothing
    else changes: a node that is not cached still needs the API to download, and
    the evaluation then fails as before. Each use is recorded in the
    evaluation's ``result.json``.
    """
    from latch_eval_tools.harness import utils as harness_utils

    pinned = DataManifest.load().nodes
    real_lpath = harness_utils.LPath

    class LPathWithPinnedName:
        def __init__(self, uri: str, *args: Any, **kwargs: Any) -> None:
            self._uri = uri
            self._real = real_lpath(uri, *args, **kwargs)

        def name(self) -> Any:
            try:
                return self._real.name()
            except Exception as exc:  # noqa: BLE001 -- any API failure
                node = pinned.get(self._uri)
                if node is None:
                    raise
                PINNED_NAME_FALLBACKS.add(self._uri)
                print(
                    f"Latch API unavailable ({type(exc).__name__}); using the pinned name "
                    f"{node.name!r} for {self._uri}",
                    flush=True,
                )
                return node.name

        def __getattr__(self, attribute: str) -> Any:
            return getattr(self._real, attribute)

    harness_utils.LPath = LPathWithPinnedName


def grade_multi(item: UpstreamEval, answer: Any) -> tuple[bool, list[Any]]:
    from latch_eval_tools.graders import grade_multiple_graders_single_answer

    results = grade_multiple_graders_single_answer(answer, item.raw["graders"])
    passed = all(result is not None and result.passed for result in results)
    return passed, results


def run_eval(item: UpstreamEval, eval_path: Path, args: argparse.Namespace, run_dir: Path) -> dict:
    from latch_eval_tools import EvalRunner
    from latch_eval_tools.harness import run_claudecode_task
    from latch_eval_tools.harness.utils import get_project_root

    out_dir = run_dir / item.task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    record: dict[str, Any] = {
        "task_id": item.task_id,
        "eval_id": item.eval_id,
        "eval_path": item.path,
        "run": args.run_name,
        "model": args.model,
        "started_at": _utc_now(),
    }

    def agent(task_prompt: str, work_dir: Path) -> dict:
        return run_claudecode_task(
            task_prompt,
            work_dir,
            model_name=args.model,
            eval_timeout=args.eval_timeout,
            docker_image=AGENT_IMAGE,
            memory_limit_bytes=int(args.memory_gb * GIB),
            provider_retry_wait_seconds=(1800.0 if args.harness_retries else 0.0),
        )

    try:
        runner = EvalRunner(
            eval_path,
            keep_workspace=True,
            run_id=args.run_name,
            cache_name=WORKSPACE_NAME,
            workspace_name=WORKSPACE_NAME,
            benchmark_name=item.source.display_name,
        )
        result = runner.run(agent_function=agent)
        answer = result.get("agent_answer")
        grader_result: Any = result.get("grader_result")
        upstream_passed = result.get("passed")
        if "graders" in item.raw and answer is not None:
            try:
                passed, grader_result = grade_multi(item, answer)
            except Exception as exc:  # noqa: BLE001 -- malformed answer: a failed eval
                # Same rule as upstream's EvalRunner and the Harbor verifier: a
                # grader that raises on the agent's answer scores it as failed.
                passed, grader_result = False, {"grader_error": f"{type(exc).__name__}: {exc}"}
        else:
            # Upstream counts an ungraded evaluation (passed is None) as not passed.
            passed = upstream_passed is True
        record.update(
            {
                "passed": bool(passed),
                "upstream_passed": upstream_passed,
                "agent_answer": answer,
                "grader_result": _jsonable(grader_result),
                "metadata": _jsonable(result.get("metadata")),
            }
        )
    except Exception as exc:  # noqa: BLE001 -- record infrastructure failures separately
        record.update(
            {
                "passed": False,
                "harness_error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
    used_pinned_names = sorted(PINNED_NAME_FALLBACKS.intersection(item.data_nodes))
    if used_pinned_names:
        record["latch_name_from_manifest"] = used_pinned_names
    record["finished_at"] = _utc_now()
    record["duration_s"] = round(time.time() - started, 1)

    work_dir = get_project_root() / WORKSPACE_NAME / "workspace" / args.run_name / item.eval_id
    for name in ("trajectory.json", "agent_output.log"):
        if (work_dir / name).is_file():
            shutil.copy2(work_dir / name, out_dir / name)
    for name in ("analysis_output.json", "eval_answer.json"):
        answer_file = work_dir / "agent_workspace" / name
        if answer_file.is_file():
            shutil.copy2(answer_file, out_dir / name)
    if not args.keep_workspace:
        shutil.rmtree(work_dir, ignore_errors=True)

    (out_dir / "result.json").write_text(json.dumps(record, indent=2, default=str) + "\n")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", required=True, help="e.g. claude-haiku-5-5")
    parser.add_argument("--run-name", required=True, help="e.g. run1")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--split", choices=["full", "small-data"], default="full")
    parser.add_argument("--task-ids", nargs="+", default=None)
    parser.add_argument("--benchmarks", nargs="+", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--upstream-root", type=Path, default=None)
    parser.add_argument("--n-concurrent", type=int, default=1)
    parser.add_argument("--eval-timeout", type=int, default=21600, help="seconds (Harbor: [agent].timeout_sec)")
    parser.add_argument("--memory-gb", type=float, default=32.0, help="container memory (Harbor: memory_mb)")
    parser.add_argument("--cpus", type=float, default=4.0, help="container CPUs (Harbor: cpus); 0 = unlimited")
    parser.add_argument(
        "--effort",
        default="max",
        choices=["max", "xhigh", "high", "medium", "low", "none"],
        help="Claude Code --effort (Harbor: reasoning_effort); 'none' omits the flag",
    )
    parser.add_argument(
        "--harness-retries",
        action="store_true",
        help="Keep upstream's automatic re-invocations (not symmetric with Harbor)",
    )
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Run the evaluations in reverse order. Useful when the Harbor side runs on the same "
        "machine at the same time: the memory-heavy evaluations then do not coincide",
    )
    parser.add_argument("--skip-existing", action="store_true", help="Reuse results already in the run directory")
    args = parser.parse_args()

    if os.environ.get("ANTHROPIC_API_KEY"):
        args.auth = "api-key"
    elif os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        args.auth = "oauth"
    else:
        raise SystemExit("Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN")

    adapter = LatchBioAdapter(
        Path("unused"),
        task_ids=args.task_ids,
        benchmarks=args.benchmarks,
        split=args.split,
        limit=args.limit,
        data_source="none",
        upstream_root=args.upstream_root,
    )
    evals = adapter.select_evals()
    paths = {
        item.task_id: ensure_checkout(
            item.source, cache_dir=adapter.cache_dir, upstream_root=adapter.upstream_root
        )
        / item.path
        for item in evals
    }

    configure_harness(args)
    install_pinned_name_fallback()
    run_dir = args.output_dir / args.run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "side": "original",
                "harness": f"latch-eval-tools=={LATCH_EVAL_TOOLS_VERSION}",
                "agent": "claude-code",
                "docker_image": AGENT_IMAGE,
                "model": args.model,
                "effort": args.effort,
                "eval_timeout": args.eval_timeout,
                "memory_gb": args.memory_gb,
                "cpus": args.cpus,
                "harness_retries": args.harness_retries,
                "auth": args.auth,
                "split": args.split,
                "reverse_order": args.reverse,
                "anthropic_base_url_set": bool(os.environ.get("ANTHROPIC_BASE_URL")),
                "tasks": [item.task_id for item in evals],
            },
            indent=2,
        )
        + "\n"
    )

    # Stage data serially first: the harness's download cache is not safe to
    # populate from several threads at once.
    from latch_eval_tools.harness import batch_download_datasets

    batch_download_datasets(
        sorted({uri for item in evals for uri in item.data_nodes}), cache_name=WORKSPACE_NAME
    )

    records: dict[str, dict] = {}
    pending = []
    for item in evals:
        existing = run_dir / item.task_id / "result.json"
        if args.skip_existing and existing.is_file():
            records[item.task_id] = json.loads(existing.read_text())
        else:
            pending.append(item)
    if args.reverse:
        pending.reverse()

    with ThreadPoolExecutor(max_workers=max(1, args.n_concurrent)) as pool:
        futures = {pool.submit(run_eval, item, paths[item.task_id], args, run_dir): item for item in pending}
        for future in as_completed(futures):
            record = future.result()
            records[record["task_id"]] = record
            status = "PASS" if record["passed"] else ("ERROR" if record.get("harness_error") else "FAIL")
            print(f"[{len(records)}/{len(evals)}] {status} {record['task_id']}", flush=True)

    ordered = [records[item.task_id] for item in evals]
    passed = sum(record["passed"] for record in ordered)
    errors = [record["task_id"] for record in ordered if record.get("harness_error")]
    summary = {
        "side": "original",
        "run": args.run_name,
        "model": args.model,
        "n_tasks": len(ordered),
        "n_passed": passed,
        "accuracy": round(100.0 * passed / len(ordered), 4) if ordered else None,
        "harness_errors": errors,
        "results": {record["task_id"]: bool(record["passed"]) for record in ordered},
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\n{passed}/{len(ordered)} passed ({summary['accuracy']}%), {len(errors)} harness error(s)")
    print(f"Results: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
