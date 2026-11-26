"""Score one Online-Mind2Web trajectory with WebJudge (Online-Mind2Web mode).

Mirrors the v2 branch of upstream `src/run.py::auto_eval` for a single task:
same inputs (task, action history, per-step screenshots), same
`WebJudge_Online_Mind2Web_eval` prompts, same `extract_predication` parsing and the
same default score threshold (3). Before judging, the trajectory is validated against
the v2 rules (README section 6) so malformed or mismatched output scores 0 instead of
being judged.

Usage: run_webjudge.py --task-json /tests/task.json --trajectory-dir /app/output/<task_id>
                       --output /logs/verifier/webjudge_result.json
Writes the reward to --reward-file. Exits 1 (no reward) only if the judge API itself fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils import OpenaiEngine, extract_predication  # noqa: E402
from webjudge_online_mind2web import WebJudge_Online_Mind2Web_eval  # noqa: E402

MODE = "WebJudge_Online_Mind2Web_eval"
SCREENSHOT_RE = re.compile(r"^(\d{4}|\d+_full_screenshot_\d+)\.(png|jpg|jpeg|webp)$")


def validate(result: dict, traj_dir: Path, task: dict) -> list[str]:
    """Upstream v2 validation rules plus a check that the trajectory is for this task."""
    errors: list[str] = []
    if result.get("schema_version") != "online-mind2web-v2":
        errors.append("schema_version must be 'online-mind2web-v2'")
    if result.get("task_id") != task["task_id"]:
        errors.append(f"task_id {result.get('task_id')!r} != {task['task_id']!r}")
    steps = result.get("action_history")
    if not isinstance(steps, list) or not steps:
        return errors + ["action_history must be a non-empty list"]
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step {i}: not an object")
            continue
        if step.get("step") != i:
            errors.append(f"step {i}: 'step' field is {step.get('step')!r}")
        if "thought" not in step:
            errors.append(f"step {i}: missing 'thought' key")
        if not isinstance(step.get("action"), str) or not step["action"]:
            errors.append(f"step {i}: missing action")
        shot = step.get("screenshot")
        if not isinstance(shot, str) or not SCREENSHOT_RE.match(shot):
            errors.append(f"step {i}: bad screenshot name {shot!r}")
        elif not (traj_dir / "trajectory" / shot).is_file():
            errors.append(f"step {i}: screenshot {shot} does not exist")
    names = [s.get("screenshot") for s in steps if isinstance(s, dict)]
    if names != sorted(names):
        errors.append("screenshot names do not sort in step order")
    last = steps[-1].get("action", "") if isinstance(steps[-1], dict) else ""
    if not last.startswith("TASK_COMPLETE"):
        errors.append("final step must be TASK_COMPLETE")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-json", required=True)
    ap.add_argument("--trajectory-dir", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--reward-file", default="/logs/verifier/reward.txt")
    ap.add_argument("--model", default=os.getenv("JUDGE_MODEL", "gpt-6-luna"))
    ap.add_argument("--score-threshold", type=int, default=int(os.getenv("JUDGE_SCORE_THRESHOLD", "3")))
    args = ap.parse_args()

    task = json.loads(Path(args.task_json).read_text())
    traj_dir = Path(args.trajectory_dir)
    out: dict = {"task_id": task["task_id"], "mode": MODE, "judge_model": args.model,
                 "score_threshold": args.score_threshold}
    reward = 0
    judge_failed = False

    def finish() -> int:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False))
        if judge_failed:
            # Infrastructure failure: leave no reward so Harbor records a verifier error
            # instead of a silent 0.
            print(f"WebJudge failed: {out.get('error')}", file=sys.stderr)
            return 1
        out["reward"] = reward
        Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False))
        Path(args.reward_file).write_text(f"{reward}\n")
        print(f"WebJudge predicted_label={out.get('predicted_label')} reward={reward}")
        return 0

    # 1) Trajectory checks: an agent that produced no / invalid trajectory scores 0.
    result_path = traj_dir / "result.json"
    try:
        result = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        out["error"] = f"no valid result.json: {type(exc).__name__}: {exc}"
        return finish()
    errors = validate(result, traj_dir, task)
    if errors:
        out["validation_errors"] = errors
        out["error"] = "invalid trajectory"
        return finish()

    # 2) WebJudge, exactly as upstream run.py does for v2 submissions.
    steps = result["action_history"]
    action_history = [s["action"] for s in steps]
    screenshot_paths = [str(traj_dir / "trajectory" / s["screenshot"]) for s in steps]
    try:
        model_name = args.model.split("/", 1)[1] if args.model.startswith("openai/") else args.model
        model = OpenaiEngine(model=model_name, api_key=os.environ.get("OPENAI_API_KEY"))
        # Task text comes from the trusted task definition, not the agent-written result.json.
        messages, text, system_msg, record, key_points = asyncio.run(
            WebJudge_Online_Mind2Web_eval(task["confirmed_task"], action_history, screenshot_paths,
                                          model, args.score_threshold)
        )
        response = model.generate(messages)[0]
    except Exception as exc:
        judge_failed = True
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc()
        return finish()

    predicted_label = extract_predication(response, MODE)
    reward = int(predicted_label)
    out.update({
        "input_text": text,
        "key_points": key_points,
        "image_judge_record": record,
        "evaluation_details": {"response": response, "predicted_label": predicted_label},
        "predicted_label": predicted_label,
        "n_steps": len(steps),
        "agent_final_answer": result.get("agent_final_answer"),
    })
    return finish()


if __name__ == "__main__":
    sys.exit(main())
