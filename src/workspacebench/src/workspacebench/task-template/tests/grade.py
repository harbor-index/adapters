"""Collect the agent's outputs and grade them with upstream's agent-as-a-judge.

Steps, each one upstream's own code (Workspace-Bench @ 3fbd0f1 in /opt/wsb):
1. Outputs: collected in the agent container by the collect hook
   (environment/collect_outputs.py, agent_runner's collector: files written
   outside model_output are mirrored into it, then the expected names, the
   agent's final path list and model_output decide which files are graded).
   They arrive here as the /logs/wsb_collected artifact; this verifier runs in
   a separate container, so the agent never touches the judge.
2. Trace: Claude Code's stream-json log (collected as claude-code.txt) is
   converted to upstream's executionTrace shape. Other agents get no trace,
   which upstream's judge accepts.
3. Judge: agent_as_a_judge.py, a Claude Code session that reads the task
   inputs, the outputs and the trace, and scores each rubric.
   environment/judge-copy-view.patch makes its judge view copy inputs and
   outputs instead of symlinking them (Glob does not follow symlinked dirs). Reward is
   passed / total. If every judge attempt fails, upstream would count all
   rubrics as failed (reward 0). Here that is a verifier error instead: no
   reward is written and the script exits non-zero, so Harbor records the
   trial as errored rather than as a real 0.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

EVAL = Path("/opt/wsb/evaluation")

WORK_DIR = "/app"
OUTPUT_DIR = "model_output"
TASK = Path("/tests/task")
COLLECTED = Path("/logs/wsb_collected")
VERIFIER = Path("/logs/verifier")
CASE = Path("/tmp/wsb-case")


def stream_events(text: str):
    for line in text.splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


def final_text(text: str) -> str:
    result = ""
    for e in stream_events(text):
        if e.get("type") == "result":
            result = e.get("result") or ""
    return result


def to_trace(text: str, prompt: str) -> list[dict]:
    """Claude Code stream-json -> upstream executionTrace (the fields the judge's snapshot reads)."""
    events = [{"type": "text", "role": "user", "content": prompt, "timestamp": None}]
    pending = {}
    for e in stream_events(text):
        if e.get("type") == "assistant":
            for b in (e.get("message") or {}).get("content") or []:
                if b.get("type") == "text":
                    events.append({"type": "text", "role": "assistant", "content": b.get("text", ""), "timestamp": None})
                elif b.get("type") == "tool_use":
                    ev = {
                        "type": "tool", "role": "tool", "tool": b.get("name"), "callID": b.get("id"),
                        "input": b.get("input") if isinstance(b.get("input"), dict) else {},
                        "output": {}, "timestamp": None,
                    }
                    pending[b.get("id")] = ev
                    events.append(ev)
        elif e.get("type") == "user":
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    pending[b["tool_use_id"]]["output"] = {"content": str(c)[:4000], "is_error": bool(b.get("is_error"))}
    return events


def write_reward(value: float, details: dict) -> None:
    VERIFIER.mkdir(parents=True, exist_ok=True)
    (VERIFIER / "reward.txt").write_text(f"{value}\n")
    (VERIFIER / "grading.json").write_text(json.dumps(details, ensure_ascii=False, indent=2))


def main() -> int:
    meta_path = TASK / "metadata.json"
    meta = json.loads(meta_path.read_text())
    meta["__metadata_path"] = str(meta_path)

    # Outputs were collected in the agent container by the collect hook
    # (environment/collect_outputs.py, upstream's collector) and arrive here as
    # the /logs/wsb_collected artifact. The judge runs in this separate container.
    collect_file = COLLECTED / "collect.json"
    if not collect_file.exists():
        print("no collected outputs (/logs/wsb_collected/collect.json missing)", file=sys.stderr)
        return 1
    collected = json.loads(collect_file.read_text())
    manifest, method = collected.get("outputs") or [], collected.get("retrievalMethod")
    log = COLLECTED / "claude-code.txt"
    stream = log.read_text(errors="replace") if log.exists() else ""
    prompt = Path("/tests/instruction.md").read_text() if Path("/tests/instruction.md").exists() else ""

    if CASE.exists():
        shutil.rmtree(CASE)
    CASE.mkdir(parents=True)
    if (COLLECTED / "output").exists():
        shutil.copytree(COLLECTED / "output", CASE / "output")
    else:
        (CASE / "output").mkdir()

    (CASE / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    agent = {"caseId": str(meta.get("id")), "workDir": WORK_DIR, "outputs": manifest, "retrievalMethod": method}
    if stream:
        agent["trace"] = {"executionTrace": to_trace(stream, prompt)}
    (CASE / "agent.json").write_text(json.dumps(agent, ensure_ascii=False, indent=2))

    model = os.environ.get("JUDGE_MODEL") or "claude-sonnet-5-5"
    # Auth: an API key if one is set (the default). Otherwise a Claude
    # subscription token (CLAUDE_CODE_OAUTH_TOKEN) via judge-subscription-auth.patch.
    if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        key_line = 'apiKey: "__SUBSCRIPTION__"\n'
    else:
        key_line = 'apiKey: "${ANTHROPIC_API_KEY}"\n'
    (CASE / "judge.yaml").write_text(
        f'model_name: "{model}"\n'
        f'model: "{model}"\n'
        'baseUrl: "${ANTHROPIC_BASE_URL:-https://api.anthropic.com}"\n'
        + key_line
    )
    env = dict(os.environ, WORKSPACE_BENCH_CLAUDE_CONFIG_DIR="/opt/wsb/judge-claude")
    retries = os.environ.get("JUDGE_MAX_RETRIES", "3")
    proc = subprocess.run(
        [sys.executable, "-u", str(EVAL / "src" / "agent_as_a_judge.py"), "--task-dir", str(CASE),
         "--eval-yaml", str(CASE / "judge.yaml"), "--max-retries", retries, "--overwrite"],
        env=env, capture_output=True, text=True,
    )
    VERIFIER.mkdir(parents=True, exist_ok=True)
    (VERIFIER / "judge_stdout.txt").write_text(proc.stdout[-200000:])
    (VERIFIER / "judge_stderr.txt").write_text(proc.stderr[-200000:])

    found = glob.glob(str(CASE / "rubrics_judge--*.json"))
    if not found:
        print("judge wrote no result", file=sys.stderr)
        return 1
    result = json.loads(Path(found[0]).read_text())
    shutil.copy(found[0], VERIFIER / "rubrics_judge.json")
    s = result.get("summary") or {}
    total = int(s.get("total") or 0)
    if total == 0:
        print("judge result has no rubrics", file=sys.stderr)
        return 1
    # Upstream fills every rubric with this evidence when no attempt produced a verdict.
    rows = result.get("rubrics") or []
    if all(str(r.get("evidence") or "").startswith("ClaudeCode judge failed") for r in rows):
        print(f"judge failed every attempt: {(result.get('judge') or {}).get('error')}", file=sys.stderr)
        return 1
    reward = int(s.get("passed") or 0) / total
    write_reward(reward, {
        "passed": s.get("passed"), "total": total, "judge_error": (result.get("judge") or {}).get("error"),
        "outputs": [m.get("outputPath") for m in manifest], "retrieval": method,
    })
    print(f"reward {reward:.4f} ({s.get('passed')}/{total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
