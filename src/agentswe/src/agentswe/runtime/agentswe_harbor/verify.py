from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

from . import settings

VERIFIER_DIR = Path("/logs/verifier")
FREEZE_MANIFEST = settings.STATE_DIR / "run" / "freeze_manifest.json"


def hidden_cases() -> list[str]:
    if os.environ.get("AGENTSWE_PROTOCOL_MODE") == "smoke":
        return list(settings.TASK["smoke"]["heldout_cases"])
    return list(settings.PROTOCOL["heldout_cases"])


def write_result(result: dict) -> None:
    VERIFIER_DIR.mkdir(parents=True, exist_ok=True)
    (VERIFIER_DIR / "agentswe_result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (VERIFIER_DIR / "reward.txt").write_text(f"{result['reward']}\n", encoding="utf-8")


def main() -> int:
    settings.apply_process_environment()
    from . import pipeline

    adapter = pipeline.upstream_adapter()
    if not FREEZE_MANIFEST.is_file():
        # Upstream one_stop.py: no valid final workspace and no valid dev submission -> no delivery.
        write_result({"reward": 0.0, "hidden_mean": 0.0, "status": "builder_artifact_invalid"})
        return 0
    freeze = json.loads(FREEZE_MANIFEST.read_text(encoding="utf-8"))
    candidate = Path(freeze["path"])
    if adapter.tree_digest(candidate) != freeze["digest"]:
        print("frozen submission digest changed between freeze and verification", file=sys.stderr)
        return 1
    # Upstream one_stop.py: the Candidate environment must be the one recorded at freeze time.
    one_stop = importlib.import_module("one_stop")
    expected_environment = freeze.get("environment_digest")
    if expected_environment and one_stop.environment_tree_digest(settings.CANDIDATE_ENV_PREFIX) != expected_environment:
        print("Builder environment differs from the frozen environment digest", file=sys.stderr)
        return 1
    try:
        summary, phase_dir = pipeline.run_phase("hidden", candidate, hidden_cases())
    except pipeline.InfrastructureError as exc:
        # A failed evaluation is not a zero score: exit without a reward.
        print(f"AgentSWE infrastructure error, score withheld: {exc}", file=sys.stderr)
        return 1
    # The inner per-case jobs reuse /logs/verifier; keep only this trial's outputs there.
    pipeline.clear_dir(VERIFIER_DIR)
    pipeline.shutil.copytree(phase_dir, VERIFIER_DIR / "hidden_phase", symlinks=True,
                             ignore=pipeline.shutil.ignore_patterns("evaluator-broker.env"))
    write_result({
        "reward": round(summary["mean_score"] / 100.0, 6),
        "hidden_mean": summary["mean_score"],
        "hidden_scores": {item["case_id"]: item["score"] for item in summary["cases"]},
        "valid_case_count": summary["valid_case_count"],
        "contract_valid_count": summary["contract_valid_count"],
        "api_totals": summary["api_totals"],
        "freeze_reason": freeze.get("reason"),
        "candidate_digest": freeze["digest"],
        "status": "scored",
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
