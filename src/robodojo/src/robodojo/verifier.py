"""Grade one RoboDojo task from the results the simulator container produced.

The verifier runs in its own container and starts no simulation. Harbor copies
the results directory out of the simulator service, which the agent container
cannot write to, and places it here. Rewards come only from the native
``_result.json`` files; the broker ledger is used to check that every scored
episode was a complete Direct episode of the expected layout.

Anything incomplete, aborted or inconsistent raises ``EvaluationError`` and
leaves no reward.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
import json
from pathlib import Path
import sys
from typing import Any

from .catalog import MOCK_LABEL, MOCK_POLICY_NAME, NATIVE_EPISODES, XPOLICYLAB_REVISION
from .runtime import EvaluationError, load_spec, read_json, validate_result, write_json

POLICY_NAME = "HarborDirect"


def ledger_entries(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise EvaluationError("Broker ledger entries must be objects")
                yield record
    except ValueError as exc:
        raise EvaluationError(f"Broker ledger is corrupt: {exc}") from exc
    except OSError as exc:
        raise EvaluationError(f"Cannot read the broker ledger: {exc}") from exc


def check_variant(native: dict[str, Any], ledger: list[dict[str, Any]], variant: str, seed: int) -> dict[str, int]:
    """Tie every natively scored episode to exactly one Direct episode."""
    by_layout: dict[int, dict[str, Any]] = {}
    for record in ledger:
        if record.get("variant") != variant:
            continue
        layout = record.get("layout_id")
        if type(layout) is not int or layout in by_layout:
            raise EvaluationError(f"Broker ledger repeats or lacks a layout for {variant}")
        by_layout[layout] = record
    decisions = steps = 0
    for detail in native["details"].values():
        record = by_layout.get(detail["layout_id"])
        if record is None:
            raise EvaluationError(f"Native layout {detail['layout_id']} of {variant} has no Direct episode")
        if record.get("ended_by") != "native_termination":
            raise EvaluationError("A scored episode did not end by native termination")
        if record.get("layout_seed_group") != seed:
            raise EvaluationError("A Direct episode used a different layout seed group")
        if record.get("native_success_observed") is not detail["success"]:
            raise EvaluationError("Broker ledger and native result disagree on an episode outcome")
        control_steps, limit = record.get("control_steps"), record.get("max_episode_steps")
        if type(control_steps) is not int or type(limit) is not int or not 0 <= control_steps <= limit:
            raise EvaluationError("A Direct episode reports an impossible number of control steps")
        if record.get("executed_steps") != control_steps:
            raise EvaluationError("Executed control steps are not all covered by accepted decisions")
        decisions += record.get("decisions", 0)
        steps += control_steps
    return {"decisions": decisions, "control_steps": steps}


def verify(spec_path: Path, results_dir: Path, output_dir: Path) -> dict[str, float]:
    """Validate the collected results and publish a reward only if complete."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("reward.json", "reward.txt", "evaluation.json"):
        (output_dir / filename).unlink(missing_ok=True)
    spec = load_spec(spec_path)
    if (results_dir / "broker" / "abort.json").exists():
        raise EvaluationError("The run was aborted before the evaluation completed")
    verdict = read_json(results_dir / "supervisor.json")
    if verdict.get("status") != "completed":
        raise EvaluationError(f"Simulator run did not complete: {verdict.get('error', verdict.get('status'))}")
    expected = {
        "task_id": spec.task_id, "dimension": spec.dimension, "seed": spec.seed,
        "upstream_revision": spec.upstream_revision, "xpolicylab_revision": XPOLICYLAB_REVISION,
        "policy_name": POLICY_NAME,
    }
    if spec.mock:
        # The CPU mock is graded by the same checks but labelled throughout,
        # so that its reward cannot be taken for a benchmark result.
        expected.update(mock=True, xpolicylab_revision=MOCK_LABEL, policy_name=MOCK_POLICY_NAME)
    elif "mock" in verdict:
        raise EvaluationError("A mock simulator cannot grade a benchmark task")
    for field, value in expected.items():
        if verdict.get(field) != value:
            raise EvaluationError(f"Simulator run reports an unexpected {field}")
    reported = verdict.get("variants")
    wanted = [{"name": variant.name, "episodes": variant.episodes} for variant in spec.variants]
    if not isinstance(reported, list) or [
        {key: item.get(key) for key in ("name", "episodes")} if isinstance(item, dict) else None
        for item in reported
    ] != wanted:
        raise EvaluationError("Simulator run does not cover the complete native protocol")
    ledger = list(ledger_entries(results_dir / "broker" / "episodes.jsonl"))
    successes, total_score, total_episodes = 0, 0.0, 0
    variants = []
    for variant, item in zip(spec.variants, reported, strict=True):
        native = read_json(results_dir / "native" / variant.name / "_result.json")
        variant_successes, variant_score = validate_result(native, variant.episodes)
        counts = check_variant(native, ledger, variant.name, spec.seed)
        variants.append({
            "name": variant.name, "episodes": variant.episodes, "run_id": item.get("run_id"),
            "layout_ids": sorted(detail["layout_id"] for detail in native["details"].values()),
            **counts,
        })
        successes += variant_successes
        total_score += variant_score
        total_episodes += variant.episodes
    if not spec.mock and total_episodes != NATIVE_EPISODES:
        raise EvaluationError(
            f"A canonical RoboDojo task must have exactly {NATIVE_EPISODES} evaluation episodes"
        )
    reward = {"success_rate": successes / total_episodes, "score": total_score / total_episodes}
    contracts = {record.get("contract_version") for record in ledger}
    if len(contracts) != 1:
        raise EvaluationError("Episodes were not all run under one action contract")
    write_json(output_dir / "evaluation.json", {
        **expected, "contract_version": contracts.pop(), "variants": variants, "rewards": reward,
    })
    write_json(output_dir / "reward.json", reward)
    return reward


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=Path("/tests/task_spec.json"))
    parser.add_argument("--results-dir", type=Path, default=Path("/harbor-robodojo/results"))
    parser.add_argument("--output-dir", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args(argv)
    try:
        verify(args.spec, args.results_dir, args.output_dir)
    except (EvaluationError, OSError) as exc:
        print(f"RoboDojo verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
