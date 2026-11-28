from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import brokers, settings
from .trial import VERIFIER_DIR, clear

FREEZE_MANIFEST = settings.RUN_DIR / "freeze_manifest.json"
# The Builder's private environment, archived in the Builder container by a collect hook.
ENVIRONMENT_ARCHIVE = Path("/var/agentswe-env/environment.tar")
# Unpacked below its own root: the upstream prefix path is a per-step link (trial.step).
BUILDER_ROOT = settings.STATE_DIR / "builder-root"
PHASE_ID = "harbor-hidden"


def write_result(result: dict) -> None:
    (VERIFIER_DIR / "agentswe_result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (VERIFIER_DIR / "reward.txt").write_text(f"{result['reward']}\n", encoding="utf-8")


def main() -> int:
    """Held-out evaluation of the frozen delivery, as upstream one_stop.main runs it after the freeze. The reward is
    the held-out mean / 100; an evaluator-side failure exits without a reward instead of recording a zero."""
    settings.prepare()
    providers = brokers.take_providers("runtime", "judge")
    adapter, one_stop = settings.import_upstream()
    if not FREEZE_MANIFEST.is_file():
        # No valid dev submission and no valid final workspace: upstream records a hidden mean of 0.
        write_result({"reward": 0.0, "hidden_mean": 0.0, "status": "builder_artifact_invalid"})
        return 0
    frozen = json.loads(FREEZE_MANIFEST.read_text(encoding="utf-8"))
    candidate = Path(frozen["path"])
    BUILDER_ROOT.mkdir(mode=0o755)
    subprocess.run(["tar", "-C", str(BUILDER_ROOT), "-xf", str(ENVIRONMENT_ARCHIVE)], check=True)
    ENVIRONMENT_ARCHIVE.unlink()
    if one_stop.environment_tree_digest(settings.CANDIDATE_ENV_PREFIX) != frozen["environment_digest"]:
        raise SystemExit("the Builder environment differs from the one recorded at freeze time")
    if adapter.tree_digest(candidate) != frozen["digest"]:
        raise SystemExit("the frozen submission changed between freeze and verification")
    with contextlib.ExitStack() as stack:
        judge_credential = brokers.evaluation_brokers(stack, providers, settings.RUN_DIR / "brokers-hidden")
        try:
            summary, phase_dir = one_stop.run_adapter_phase(
                run_dir=settings.RUN_DIR, phase_id=PHASE_ID, candidate=candidate, cases=None,
                benchmark=settings.TASK_DIR / "benchmark", env_prefix=settings.CANDIDATE_ENV_PREFIX,
                trusted_env_prefix=settings.TRUSTED_ENV_PREFIX,
                trusted_env_digest=one_stop.environment_tree_digest(settings.TRUSTED_ENV_PREFIX),
                credential_file=judge_credential, jobs_dir=settings.JOBS_DIR, n_concurrent=settings.N_CONCURRENT,
            )
        except RuntimeError as exc:
            print(f"AgentSWE infrastructure error, score withheld: {exc}", file=sys.stderr)
            return 1
    if adapter.tree_digest(candidate) != frozen["digest"]:
        raise SystemExit("the frozen submission changed during the held-out evaluation")
    # The per-case jobs reused /logs/verifier; keep this trial's evidence there.
    clear(VERIFIER_DIR)
    shutil.copytree(phase_dir, VERIFIER_DIR / "hidden_phase", symlinks=True)
    for job in settings.JOBS_DIR.glob(f"*{PHASE_ID}*"):
        shutil.copytree(job, VERIFIER_DIR / "hidden_jobs" / job.name, symlinks=True)
    write_result({
        "reward": round(summary["mean_score"] / 100.0, 6),
        "hidden_mean": summary["mean_score"],
        "hidden_scores": {case["case_id"]: case["score"] for case in summary["cases"]},
        "freeze_reason": frozen.get("reason"),
        "candidate_digest": frozen["digest"],
        "status": "scored",
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
