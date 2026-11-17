"""Aggregate a complete RoboDojo job with equal weight per capability dimension."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .catalog import DIMENSION_TASKS, UPSTREAM_REVISION, XPOLICYLAB_REVISION, task_spec
from .runtime import EvaluationError, read_json


def aggregate(job_dir: Path) -> dict:
    """Require one successful trial per canonical task, with matching provenance.

    All trials must share the layout seed group, the model, the prompt and the
    scaffold settings; otherwise their scores are not one benchmark result.
    """
    expected = {task: dimension for dimension, tasks in DIMENSION_TASKS.items() for task in tasks}
    trials: dict[str, dict] = {}
    provenance: set[str] = set()
    files = sorted(job_dir.glob("*/result.json"))
    if not files:
        raise EvaluationError("No Harbor trial result files found")
    for path in files:
        trial = read_json(path)
        if trial.get("exception_info") is not None:
            raise EvaluationError(f"Trial failed: {path.parent.name}")
        report = read_json(path.parent / "verifier" / "evaluation.json")
        task_id = report.get("task_id")
        if not isinstance(task_id, str) or task_id not in expected or task_id in trials:
            raise EvaluationError("Job contains an unknown or repeated canonical task")
        harbor_name = f"robodojo/{task_id.lower().replace('_', '-')}"
        if trial.get("task_name") != harbor_name:
            raise EvaluationError(f"Task identity mismatch: {path.parent.name}")
        seed = report.get("seed")
        if type(seed) is not int or seed < 0:
            raise EvaluationError("Missing valid simulator seed")
        spec = task_spec(task_id, seed)
        for field in ("dimension", "upstream_revision"):
            if report.get(field) != spec[field]:
                raise EvaluationError(f"Unexpected {field} for {task_id}")
        variants = report.get("variants")
        if not isinstance(variants, list) or any(not isinstance(item, dict) for item in variants):
            raise EvaluationError("Missing native variant provenance")
        if [{key: item.get(key) for key in ("name", "episodes")} for item in variants] != spec["variants"]:
            raise EvaluationError(f"Unexpected native variants for {task_id}")
        if report["upstream_revision"] != UPSTREAM_REVISION:
            raise EvaluationError("Unexpected simulator revision")
        if report.get("xpolicylab_revision") != XPOLICYLAB_REVISION:
            raise EvaluationError("Unexpected XPolicyLab revision")
        if report.get("policy_name") != "HarborDirect" or not isinstance(report.get("contract_version"), str):
            raise EvaluationError("Missing Direct controller provenance")
        agent = trial.get("agent_result")
        metadata = agent.get("metadata") if isinstance(agent, dict) else None
        direct = metadata.get("robodojo_direct") if isinstance(metadata, dict) else None
        if not isinstance(direct, dict):
            raise EvaluationError(f"Missing Direct agent provenance: {path.parent.name}")
        scaffold = {key: direct.get(key) for key in (
            "agent", "agent_version", "model", "contract_version", "prompt_sha256", "settings",
        )}
        if any(value is None for value in scaffold.values()):
            raise EvaluationError(f"Incomplete Direct agent provenance: {path.parent.name}")
        if scaffold["contract_version"] != report["contract_version"]:
            raise EvaluationError("Agent and simulator used different action contracts")
        provenance.add(json.dumps({"seed": seed, "direct": scaffold}, sort_keys=True))
        verifier = trial.get("verifier_result")
        rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
        if rewards != report.get("rewards") or not isinstance(rewards, dict):
            raise EvaluationError("Trial reward does not match native evaluation provenance")
        if set(rewards) != {"success_rate", "score"}:
            raise EvaluationError("Expected native success_rate and normalized score")
        for value in rewards.values():
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise EvaluationError("Rewards must be finite numbers in [0, 1]")
        trials[task_id] = rewards
    if set(trials) != set(expected):
        missing = ", ".join(sorted(set(expected) - set(trials)))
        raise EvaluationError(f"Full benchmark requires 42 tasks; missing: {missing}")
    if len(provenance) != 1:
        raise EvaluationError("Aggregate each layout seed group, model and scaffold separately")
    dimensions = {
        dimension: {
            metric: math.fsum(trials[task][metric] for task in tasks) / len(tasks)
            for metric in ("success_rate", "score")
        }
        for dimension, tasks in DIMENSION_TASKS.items()
    }
    result = {
        metric: math.fsum(values[metric] for values in dimensions.values()) / len(dimensions)
        for metric in ("success_rate", "score")
    }
    return {**result, "dimensions": dimensions, "tasks": len(trials),
            "upstream_revision": UPSTREAM_REVISION, **json.loads(provenance.pop())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = aggregate(args.job_dir)
    except (EvaluationError, OSError) as exc:
        parser.error(str(exc))
    content = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(content)
    else:
        print(content, end="")


if __name__ == "__main__":
    main()
