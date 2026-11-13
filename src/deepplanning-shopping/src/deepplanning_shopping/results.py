"""Verify saved Harbor oracle results and print a reviewable receipt."""

import argparse
import json
from pathlib import Path


def check_oracle(job_dir: Path, expected: int) -> dict:
    job = json.loads((job_dir / "result.json").read_text())
    stats = job["stats"]
    trials = [json.loads(p.read_text()) for p in sorted(job_dir.glob("*/result.json"))]
    if len(trials) != expected or stats["n_completed_trials"] != expected:
        raise ValueError(f"Expected {expected} completed trials, found {len(trials)}")
    if stats["n_errored_trials"] or stats["n_retries"]:
        raise ValueError("Oracle job contains errors or retries")
    rewards = {}
    matched = total = 0
    for trial in trials:
        if trial["exception_info"] or trial["verifier_environment_mode"] != "separate":
            raise ValueError(f"Invalid isolated verification: {trial['task_name']}")
        reward = trial["verifier_result"]["rewards"]["reward"]
        if reward != 1.0:
            raise ValueError(f"Oracle reward {reward}: {trial['task_name']}")
        rewards[trial["task_name"]] = reward
        logs = job_dir / trial["trial_name"] / "verifier"
        if float((logs / "reward.txt").read_text()) != reward:
            raise ValueError("Trial result and reward file disagree")
        report = json.loads((logs / "report.json").read_text())
        if not report["is_completed"] or report["case_score"] != 1.0:
            raise ValueError("Reference replay did not complete")
        matched += report["matched_count"]
        total += report["expected_count"]
    if len(rewards) != expected:
        raise ValueError("Duplicate task names in oracle job")
    return {
        "trials": expected,
        "exceptions": 0,
        "retries": 0,
        "mean_reward": 1.0,
        "match_rate": matched / total,
        "separate_verifier_trials": expected,
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "rewards": rewards,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--expected", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    receipt = check_oracle(args.job_dir, args.expected)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print("DeepPlanning Shopping levels 1–2 — saved Harbor oracle results")
    print("Runtime: Harbor 0.25.0 | Smol 1.32.4 | local microVMs")
    print(f"Oracle: {receipt['trials']}/{args.expected} passed | mean reward 1.000")
    print(
        f"Exceptions: 0 | retries: 0 | isolated verifiers: {receipt['separate_verifier_trials']}"
    )
    print(f"Upstream product match rate: {receipt['match_rate']:.3f}")
    print("Verified every trial result, reward file and upstream grader report.")


if __name__ == "__main__":
    main()
