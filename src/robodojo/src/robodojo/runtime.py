"""Run native RoboDojo evaluation against an existing XPolicyLab policy server.

This bridge reads simulator-produced artifacts; it never accepts submitted scores.
Infrastructure and incomplete-run errors produce no Harbor reward.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
import json
import keyword
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable
import uuid


from .catalog import DIMENSION_TASKS, UPSTREAM_REVISION, XPOLICYLAB_REVISION


SIMULATOR_TIMEOUT = 36_000


class EvaluationError(RuntimeError):
    """A run cannot yield a trustworthy, complete native evaluation result."""


@dataclass(frozen=True)
class Variant:
    name: str
    episodes: int


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    dimension: str
    seed: int
    upstream_revision: str
    variants: tuple[Variant, ...]


@dataclass(frozen=True)
class Submission:
    policy_name: str
    host: str
    port: int


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    """Read one JSON object, rejecting duplicate keys and nonstandard numbers."""
    def reject_constant(value: str) -> None:
        raise EvaluationError(f"Non-finite JSON value: {value}")

    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(
                source, object_pairs_hook=_unique_object,
                parse_constant=reject_constant,
            )
    except (OSError, ValueError) as exc:
        raise EvaluationError(f"Cannot read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"Expected a JSON object in {path}")
    return value


def load_spec(path: Path) -> TaskSpec:
    value = read_json(path)
    task_id, dimension = value.get("task_id"), value.get("dimension")
    if not isinstance(dimension, str) or dimension not in DIMENSION_TASKS:
        raise EvaluationError("Task specification has an unknown dimension")
    if task_id not in DIMENSION_TASKS[dimension]:
        raise EvaluationError("Task is not canonical for its capability dimension")
    seed = value.get("seed")
    if type(seed) is not int or seed < 0:
        raise EvaluationError("Task seed must be a nonnegative integer")
    if value.get("upstream_revision") != UPSTREAM_REVISION:
        raise EvaluationError("Task specification does not use the pinned upstream revision")
    expected = (
        [{"name": task_id, "episodes": 25}, {"name": f"{task_id}_random", "episodes": 25}]
        if dimension == "generalization"
        else [{"name": task_id, "episodes": 50}]
    )
    variants = value.get("variants")
    if variants != expected or not isinstance(variants, list):
        raise EvaluationError("Task variants must match the complete native evaluation protocol")
    for variant in variants:
        if type(variant["episodes"]) is not int:
            raise EvaluationError("Episode count must be an integer")
    return TaskSpec(
        task_id, dimension, seed, UPSTREAM_REVISION,
        tuple(Variant(**variant) for variant in variants),
    )


def load_submission(path: Path) -> Submission:
    value = read_json(path)
    if set(value) != {"policy_name", "host", "port"}:
        raise EvaluationError("Submission must contain only policy_name, host, and port")
    policy = value["policy_name"]
    if (
        not isinstance(policy, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", policy)
        or keyword.iskeyword(policy)
    ):
        raise EvaluationError("policy_name must be a safe Python module identifier")
    host = value["host"]
    if not isinstance(host, str) or not host or len(host) > 253:
        raise EvaluationError("host must be an IPv4 address or DNS hostname")
    if re.fullmatch(r"[0-9.]+", host):
        try:
            ipaddress.IPv4Address(host)
        except ipaddress.AddressValueError as exc:
            raise EvaluationError("host is not a valid IPv4 address") from exc
    elif not all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in host.removesuffix(".").split(".")
    ):
        raise EvaluationError("host must be an IPv4 address or DNS hostname")
    port = value["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise EvaluationError("port must be an integer from 1 through 65535")
    return Submission(policy, host, port)


def _number(value: Any, field: str, upper: float = 1.0) -> float:
    if type(value) not in (int, float) or not 0 <= value <= upper or not math.isfinite(value):
        raise EvaluationError(f"{field} must be a finite number in [0, {upper:g}]")
    return float(value)


def validate_result(value: dict[str, Any], episodes: int) -> tuple[int, float]:
    """Validate native totals and unique evaluated layouts, returning raw totals.

    Upstream stores per-episode scores in [0, 1] but its aggregate score in
    [0, 100]. Harbor's score is normalized to [0, 1] only after validation.
    Native recovery can replace unstable layouts with later layout IDs, so
    IDs need not form a contiguous range even for a complete evaluation.
    """
    if type(value.get("eval_time")) is not int or value["eval_time"] != episodes:
        raise EvaluationError("Native result is incomplete or has an unexpected eval_time")
    details = value.get("details")
    if not isinstance(details, dict) or len(details) != episodes:
        raise EvaluationError("Native result must contain exactly one detail per episode")
    layouts: set[int] = set()
    successes, scores = 0, []
    for detail in details.values():
        if not isinstance(detail, dict):
            raise EvaluationError("Each episode detail must be an object")
        layout = detail.get("layout_id")
        if type(layout) is not int or layout < 0 or layout in layouts:
            raise EvaluationError("Native layout IDs must be unique nonnegative integers")
        layouts.add(layout)
        if type(detail.get("success")) is not bool:
            raise EvaluationError("Each episode success value must be boolean")
        score = _number(detail.get("score"), "Episode score")
        if detail["success"] and score != 1.0:
            raise EvaluationError("A successful native episode must have a score of 1")
        successes += detail["success"]
        scores.append(score)
    total_score = math.fsum(scores)
    expected_success = successes / episodes
    expected_score = total_score / episodes * 100
    if not math.isclose(
        _number(value.get("success_rate"), "success_rate"), expected_success,
        rel_tol=1e-9, abs_tol=1e-9,
    ):
        raise EvaluationError("Native success_rate does not match episode details")
    if not math.isclose(
        _number(value.get("score"), "Aggregate score", upper=100), expected_score,
        rel_tol=1e-9, abs_tol=1e-9,
    ):
        raise EvaluationError("Native score does not match episode details")
    return successes, total_score


def result_path(root: Path, variant: Variant, submission: Submission, seed: int, run_id: str) -> Path:
    return (
        root / "eval_result" / "RoboDojo" / variant.name / submission.policy_name
        / "arx_x5" / f"{seed}_ckpt_name=external,action_type=ee" / run_id / "_result.json"
    )


def find_result(
    root: Path, variant: Variant, submission: Submission, seed: int,
    run_id: str, started_ns: int,
) -> Path:
    """Locate only this invocation's artifact, rejecting stale or ambiguous output."""
    scope = root / "eval_result" / "RoboDojo" / variant.name / submission.policy_name
    candidates = [path for path in scope.rglob("_result.json") if run_id in path.parts]
    expected = result_path(root, variant, submission, seed, run_id)
    if len(candidates) != 1 or candidates[0] != expected:
        raise EvaluationError("Expected exactly one native result for this task, policy, seed, and run ID")
    if expected.is_symlink() or expected.resolve() != expected.absolute():
        raise EvaluationError("Native result path must not contain symlinks")
    if expected.stat().st_mtime_ns < started_ns:
        raise EvaluationError("Native result is stale")
    return expected


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def evaluate(
    spec_path: Path, submission_path: Path, root: Path, output_dir: Path,
    *, runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> dict[str, float]:
    """Run each native variant sequentially and publish a reward only on completion."""
    runner = runner or subprocess.run
    root, output_dir = root.resolve(), output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # A retry must never leave a previous successful reward after a failed run.
    for filename in ("reward.json", "reward.txt", "evaluation.json"):
        (output_dir / filename).unlink(missing_ok=True)
    spec, submission = load_spec(spec_path), load_submission(submission_path)
    for filename, expected_revision in (
        (".harbor-upstream-revision", spec.upstream_revision),
        (".harbor-xpolicylab-revision", XPOLICYLAB_REVISION),
    ):
        try:
            revision = (root / filename).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise EvaluationError(f"Cannot verify image revision marker {filename}: {exc}") from exc
        if revision != expected_revision:
            raise EvaluationError(f"Image marker {filename} does not match the pinned upstream revision")
    script = root / "scripts" / "robodojo.sh"
    deploy = root / "XPolicyLab" / "policy" / submission.policy_name / "deploy.py"
    if not script.is_file() or not deploy.is_file():
        raise EvaluationError("Simulator entry point or installed policy deploy adapter is missing")
    successes, total_score, total_episodes = 0, 0.0, 0
    evaluated_variants = []
    for variant in spec.variants:
        run_id = f"harbor_{uuid.uuid4().hex}"
        expected = result_path(root, variant, submission, spec.seed, run_id)
        if expected.parent.exists():
            raise EvaluationError("Invocation run directory already exists")
        command = [
            "bash", str(script), "client", "--task", variant.name,
            "--policy-name", submission.policy_name, "--policy-host", submission.host,
            "--policy-port", str(submission.port), "--seed", str(spec.seed),
            "--eval-num", "native", "--ckpt", "external", "--action-type", "ee",
        ]
        environment = os.environ.copy()
        environment.update(EVAL_NUM="native", ROBODOJO_RUN_ID=run_id)
        started_ns = time.time_ns()
        _write_json(output_dir / f"{variant.name}.run.json", {
            "command": command, "run_id": run_id, "started_ns": started_ns,
            "expected_result": str(expected), "upstream_revision": spec.upstream_revision,
        })
        try:
            with (output_dir / f"{variant.name}.log").open("w", encoding="utf-8") as log:
                runner(
                    command, cwd=root, env=environment, stdout=log,
                    stderr=subprocess.STDOUT, check=True, timeout=SIMULATOR_TIMEOUT,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise EvaluationError(f"Simulator failed for {variant.name}: {exc}") from exc
        artifact = find_result(root, variant, submission, spec.seed, run_id, started_ns)
        # Preserve the raw run artifacts before validating them for diagnosis.
        if any(path.is_symlink() for path in artifact.parent.rglob("*")):
            raise EvaluationError("Native artifacts must not contain symlinks")
        destination = output_dir / "artifacts" / variant.name / run_id
        shutil.copytree(artifact.parent, destination)
        variant_successes, variant_score = validate_result(read_json(artifact), variant.episodes)
        evaluated_variants.append({
            "name": variant.name, "episodes": variant.episodes,
            "run_id": run_id, "result_path": str(destination / "_result.json"),
        })
        successes += variant_successes
        total_score += variant_score
        total_episodes += variant.episodes
    if total_episodes != 50:
        raise EvaluationError("A canonical RoboDojo task must have exactly 50 evaluation episodes")
    reward = {"success_rate": successes / total_episodes, "score": total_score / total_episodes}
    provenance = {
        "task_id": spec.task_id, "dimension": spec.dimension, "seed": spec.seed,
        "upstream_revision": spec.upstream_revision,
        "xpolicylab_revision": XPOLICYLAB_REVISION,
        "submission": {"policy_name": submission.policy_name, "host": submission.host, "port": submission.port},
        "variants": evaluated_variants, "rewards": reward,
    }
    evaluation_temporary = output_dir / "evaluation.json.tmp"
    _write_json(evaluation_temporary, provenance)
    evaluation_temporary.replace(output_dir / "evaluation.json")
    temporary = output_dir / "reward.json.tmp"
    _write_json(temporary, reward)
    temporary.replace(output_dir / "reward.json")
    return reward


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=Path("/tests/task_spec.json"))
    parser.add_argument("--submission", type=Path, default=Path("/workspace/submission.json"))
    parser.add_argument("--root", type=Path, default=Path("/workspace/RoboDojo"))
    parser.add_argument("--output-dir", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args(argv)
    try:
        evaluate(args.spec, args.submission, args.root, args.output_dir)
    except (EvaluationError, OSError) as exc:
        print(f"RoboDojo verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
