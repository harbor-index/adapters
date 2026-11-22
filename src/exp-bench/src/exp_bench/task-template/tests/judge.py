"""EXP-Bench verifier for Harbor.

A port of the upstream EXP-Bench judging pipeline
(github.com/Just-Curieous/Curie, benchmark/exp_bench/evaluation/{eval.py,judge.py},
helper/utils.py and model.py). The prompts in ./prompts are copied verbatim from
upstream and are filled with the same Python objects, so the judge sees the same text.

Pipeline (same order as upstream ``query_normal``):
  1. Collect the agent's output JSON and remove it from the repo (eval.py:_query_agent).
  2. Build the git diff patch of the agent's changes (eval.py:generate_patch).
  3. Setup monitor on the agent logs; if it flags forbidden operations, every score is 0.
  4. Design + conclusion judge.
  5. Setup (implementation) judge on the patch, chunked when it exceeds the context.
  6. Execution check: run reproduce_exp_bench.sh with a 1 hour timeout.

Deviations from upstream are listed in the adapter README.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from langchain_text_splitters import RecursiveCharacterTextSplitter
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError
import tiktoken

TESTS_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = TESTS_DIR / "prompts"
WORKSPACE = Path("/workspace")
AGENT_LOG_DIR = Path("/logs/agent")
VERIFIER_DIR = Path("/logs/verifier")

JUDGE_MODEL = os.environ.get("EXP_BENCH_JUDGE_MODEL") or "o3-mini"
EXEC_CHECK = os.environ.get("EXP_BENCH_EXEC_CHECK", "1") != "0"
EXEC_TIMEOUT_SEC = int(os.environ.get("EXP_BENCH_EXEC_TIMEOUT_SEC", "3600"))
# Oracle validation only: skip the log monitor and execution check, which judge an
# agent's trajectory and cannot be satisfied by copying reference answers.
ORACLE_VALIDATION = os.environ.get("EXP_BENCH_ORACLE_VALIDATION", "0") == "1"
# Capture the judge inputs in the container and run the LLM judge later
# (scripts/judge_deferred.py), e.g. when no judge API key is available during the run.
DEFER_JUDGE = os.environ.get("EXP_BENCH_DEFER_JUDGE", "0") == "1"
DEFERRED_INPUTS_FILE = "judge_inputs.json"

ERR = "Error parsing response"

# helper/utils.py:get_model_context_length
CONTEXT_LENGTHS = {
    "gpt-4o": 128000,
    "o3-mini": 200000,
    "gpt-4o-mini": 128000,
    "anthropic.claude-3-7-sonnet-20250219-v1:0": 200000,
    "anthropic.claude-3-5-haiku-20241022-v1:0": 200000,
    "deepseek.r1-v1:0": 64000,
    "amazon.nova-pro-v1:0": 300000,
    "gemini-2.5-pro-preview-03-25": 200000,
}


def log(msg: str) -> None:
    print(f"[exp-bench judge] {msg}", flush=True)


# --------------------------------------------------------------------------- tokens


def context_length() -> int:
    return CONTEXT_LENGTHS.get(JUDGE_MODEL, 30000)


def count_tokens(text: str) -> int:
    try:
        encoding = tiktoken.encoding_for_model(JUDGE_MODEL)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    try:
        return len(encoding.encode(text, disallowed_special=()))
    except Exception:
        return 0


def get_messages_needed(msg: str) -> int:
    max_tokens = context_length() - 1000
    return (count_tokens(msg) // max_tokens) + 1


def breakdown_message(msg: str) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=context_length() - 10000,
        chunk_overlap=100,
        length_function=count_tokens,
        is_separator_regex=False,
    )
    return splitter.split_text(msg)


# --------------------------------------------------------------------------- llm


def make_client() -> OpenAI:
    kwargs = {"api_key": os.environ["OPENAI_API_KEY"]}
    if os.environ.get("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
    return OpenAI(**kwargs)


CLIENT = None


def llm_query_plain(system_prompt: str, default_json_output: dict) -> dict:
    """model.py:llm_query_plain with use_json=True (system prompt, empty user turn)."""
    global CLIENT
    if CLIENT is None:
        CLIENT = make_client()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": ""},
    ]
    kwargs = {
        "model": JUDGE_MODEL,
        "messages": messages,
        "stop": None,
        "response_format": {"type": "json_object"},
    }
    if not ("o3" in JUDGE_MODEL or "o1" in JUDGE_MODEL):
        kwargs["temperature"] = 0.7
    # Transport-level retries only; they do not change what is judged.
    for attempt in range(6):
        try:
            response = CLIENT.chat.completions.create(**kwargs)
            break
        except (APIConnectionError, APITimeoutError, RateLimitError) as e:
            wait = min(60, 5 * 2**attempt)
            log(f"judge API transient error ({type(e).__name__}); retrying in {wait}s")
            time.sleep(wait)
        except APIStatusError as e:
            if e.status_code >= 500 and attempt < 5:
                wait = min(60, 5 * 2**attempt)
                log(f"judge API {e.status_code}; retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
    else:
        raise RuntimeError("Judge API unavailable after retries")
    content = response.choices[0].message.content or ""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return dict(default_json_output)


def query_plain(task_prompt: str, default_json_output: dict) -> dict:
    """judge.py:query_plain: up to 3 attempts until all keys parse."""
    response = dict(default_json_output)
    for iteration in range(3):
        response = llm_query_plain(task_prompt, default_json_output)
        if set(response.keys()) == set(default_json_output.keys()):
            if not any(value == ERR for value in response.values()):
                return response
        log(f"unexpected judge output (attempt {iteration + 1}): {str(response)[:300]}")
    return response


# --------------------------------------------------------------------------- inputs


def run(cmd: str, cwd: Path | None = None) -> None:
    subprocess.run(cmd, shell=True, cwd=cwd, check=False)


def escape_invalid_json_control_chars(raw_text: str) -> str:
    """helper/utils.py:escape_invalid_json_control_chars."""

    def escape_inside_quotes(match: re.Match) -> str:
        content = match.group(0)
        return content.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

    return re.sub(r'"(.*?)(?<!\\)"', escape_inside_quotes, raw_text, flags=re.DOTALL)


def safe_json_load(path: Path):
    raw_text = path.read_text(errors="replace")
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        try:
            return json.loads(escape_invalid_json_control_chars(raw_text))
        except json.JSONDecodeError as e2:
            return {"no_answer": f"JSONDecodeError after repair: {e2}"}


def collect_agent_output(output_filename: str) -> dict:
    """eval.py:_query_agent: copy, delete from the repo, validate keys."""
    workspace_path = WORKSPACE / output_filename
    saved_path = VERIFIER_DIR / "eval_gen.json"
    if workspace_path.exists():
        shutil.copy(workspace_path, saved_path)
        workspace_path.unlink()
        response = safe_json_load(saved_path)
        if isinstance(response, dict) and set(response.keys()) == {"design", "conclusion"}:
            return response
        log(f"agent output has unexpected keys: {str(response)[:300]}")
    else:
        log(f"agent output {workspace_path} not found")
    response = {"no_answer": "No answer found in eval gen after 1 iterations."}
    saved_path.write_text(json.dumps(response, indent=2))
    return response


def generate_patch(patch_file: Path) -> str:
    """eval.py:generate_patch (same git commands and size limit)."""
    repo = str(WORKSPACE)
    run(f"git config --global --add safe.directory {repo}")
    run("git config --global --add safe.directory '*'")
    run("git config core.fileMode false", cwd=WORKSPACE)
    run("git ls-files --others --exclude-standard | xargs git add -N", cwd=WORKSPACE)
    run(
        "git submodule foreach --quiet --recursive "
        "'git ls-files --others --exclude-standard | xargs git add -N || true'",
        cwd=WORKSPACE,
    )
    run(
        f"git diff -G\".\" | grep -v '^diff --git' | grep -v '^index' > {patch_file}",
        cwd=WORKSPACE,
    )
    run(
        "git submodule foreach --recursive 'git diff -G\".\"' "
        f"| grep -v '^diff --git' | grep -v '^index' >> {patch_file}",
        cwd=WORKSPACE,
    )
    patch_contents = patch_file.read_text(errors="replace") if patch_file.exists() else ""

    size_mb = patch_file.stat().st_size / (1024 * 1024) if patch_file.exists() else 0
    head_lines = tail_lines = 5000
    if size_mb > 70:
        lines = patch_file.read_text(errors="replace").splitlines(keepends=True)
        while size_mb > 70 and head_lines >= 10 and tail_lines >= 10:
            if len(lines) <= head_lines + tail_lines:
                clipped = lines
            else:
                clipped = lines[:head_lines] + ["...\n"] + lines[-tail_lines:]
            patch_file.write_text("".join(clipped))
            size_mb = patch_file.stat().st_size / (1024 * 1024)
            if size_mb > 70:
                head_lines = max(head_lines // 2, 10)
                tail_lines = max(tail_lines // 2, 10)
    if not patch_contents.strip():
        log("patch is empty: no changes detected")
    return patch_file.read_text(errors="replace") if patch_file.exists() else ""


def read_agent_logs() -> str:
    """Return the agent log the monitor judges.

    Upstream passes the OpenHands console log (stdout and stderr of the OpenHands CLI,
    ``tee``'d to ``*_eval_gen_openhands_logs.txt``). Harbor's OpenHands agent writes the
    same stream to ``/logs/agent/openhands.txt``, so only that file is used when it
    exists; Harbor's derived files (``trajectory.json``, ``sessions/``) repeat its
    content in another format. Other agents have no upstream equivalent, so all their
    text logs are concatenated.
    """
    console_log = AGENT_LOG_DIR / "openhands.txt"
    parts = []
    if console_log.is_file():
        parts.append(console_log.read_text(errors="replace"))
    elif AGENT_LOG_DIR.exists():
        for path in sorted(p for p in AGENT_LOG_DIR.rglob("*") if p.is_file()):
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            if "\x00" in text[:4096]:
                continue
            parts.append(f"===== {path.relative_to(AGENT_LOG_DIR)} =====\n{text}")
    logs = "\n".join(parts)
    # Keep the monitor prompt within the judge context (upstream would error instead).
    budget = context_length() - 20000
    if count_tokens(logs) > budget:
        encoding = tiktoken.get_encoding("cl100k_base")
        tokens = encoding.encode(logs, disallowed_special=())
        half = max(budget // 2, 1000)
        logs = (
            encoding.decode(tokens[:half])
            + "\n\n... [log truncated to fit the judge context] ...\n\n"
            + encoding.decode(tokens[-half:])
        )
    (VERIFIER_DIR / "agent_logs_for_monitor.txt").write_text(logs)
    return logs


def concatenate_setup_scripts(setup_scripts: dict, paths: list[str]) -> str:
    """helper/utils.py:concatenate_setup_scripts with sources read at the pinned commit."""
    text = ""
    for path in paths:
        text += f"{path}:\n\n"
        text += (setup_scripts.get(path) or "") + "\n\n"
    return text


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


# --------------------------------------------------------------------------- exec


def execute_setup() -> dict:
    """judge.py:execute_setup: run reproduce_exp_bench.sh with a 1 hour timeout."""
    log("running execution check: bash reproduce_exp_bench.sh")
    proc = subprocess.Popen(
        ["bash", "-c", f"cd {WORKSPACE} && bash reproduce_exp_bench.sh"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=EXEC_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return {"execution_success": False, "execution_message": "Timeout running setup script"}
    (VERIFIER_DIR / "exec_stdout.txt").write_text(stdout or "")
    (VERIFIER_DIR / "exec_stderr.txt").write_text(stderr or "")
    if proc.returncode != 0:
        return {
            "execution_success": False,
            "execution_message": f"Error running setup script: {(stderr or '')[-20000:]}",
        }
    return {
        "execution_success": True,
        "execution_message": f"Setup script executed successfully. Output: {(stdout or '')[-20000:]}",
    }


# --------------------------------------------------------------------------- scoring


def to_fraction(score) -> float:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, value)) / 100.0


def main() -> None:
    VERIFIER_DIR.mkdir(parents=True, exist_ok=True)
    spec = json.loads((TESTS_DIR / "task_data.json").read_text())

    output_json = collect_agent_output(spec["output_filename"])
    patch = generate_patch(VERIFIER_DIR / "eval_gen.patch")

    if DEFER_JUDGE:
        # Everything that needs the container (agent output, patch, logs, execution
        # check) is captured now; the LLM calls run later via scripts/judge_deferred.py.
        # The execution check runs unconditionally here; grade() ignores it when the
        # monitor flags the run, as upstream skips it in that case.
        inputs = {
            "judge_model": JUDGE_MODEL,
            "oracle_validation": ORACLE_VALIDATION,
            "exec_check": EXEC_CHECK,
            "spec": spec,
            "output_json": output_json,
            "patch": patch,
            "agent_logs": None if ORACLE_VALIDATION else read_agent_logs(),
            "execution": execute_setup() if (EXEC_CHECK and not ORACLE_VALIDATION) else None,
        }
        (VERIFIER_DIR / DEFERRED_INPUTS_FILE).write_text(json.dumps(inputs))
        (VERIFIER_DIR / "reward.json").write_text(json.dumps({"judge_pending": 1.0}))
        log("EXP_BENCH_DEFER_JUDGE=1: judge inputs saved; grade later with scripts/judge_deferred.py")
        return

    grade(spec, output_json, patch)


def grade(
    spec: dict,
    output_json: dict,
    patch: str,
    agent_logs: str | None = None,
    execution: dict | None = None,
) -> dict:
    """Upstream judge.py:query_normal: monitor, design/conclusion, setup, execution.

    ``agent_logs`` and ``execution`` are passed when grading inputs saved by
    ``EXP_BENCH_DEFER_JUDGE=1``; otherwise they are read / run here.
    """
    task_data = spec["task_data"]
    if "no_answer" in output_json:
        design_output = output_json["no_answer"]
        conclusion_output = output_json["no_answer"]
    else:
        design_output = output_json["design"]
        conclusion_output = output_json["conclusion"]

    setup_output_str = patch
    setup_gt = task_data["requirements"] if "requirements" in task_data else task_data["method"]
    setup_scripts = concatenate_setup_scripts(spec["setup_scripts"], spec["setup_script_paths"])

    default_json_output = {
        "design_evaluation_explanation": ERR,
        "design_score": ERR,
        "design_error_analysis": ERR,
        "conclusion_evaluation_explanation": ERR,
        "conclusion_score": ERR,
        "conclusion_error_analysis": ERR,
    }
    default_setup_json_output = {
        "setup_evaluation_explanation": ERR,
        "setup_score": ERR,
        "setup_error_analysis": ERR,
    }
    default_monitor_json_output = {
        "paper_access": ERR,
        "git_operations": ERR,
        "faked_or_nonexperimental_data": ERR,
        "setup_monitor_comprehensive_reason": ERR,
    }

    judged: dict = {}
    monitor_flagged = False

    # 0. Setup monitor.
    if ORACLE_VALIDATION:
        log("EXP_BENCH_ORACLE_VALIDATION=1: skipping the log monitor and execution check")
        monitor_response = {"monitor_skipped": True}
    else:
        logs = agent_logs if agent_logs is not None else read_agent_logs()
        monitor_prompt = load_prompt("eval_judge_setup_monitor_prompt.txt").format(logs=logs)
        monitor_response = query_plain(monitor_prompt, default_monitor_json_output)
        monitor_flagged = any(v == True for v in monitor_response.values()) or any(  # noqa: E712
            v == ERR for v in monitor_response.values()
        )
    (VERIFIER_DIR / "monitor.json").write_text(json.dumps(monitor_response, indent=2))

    if monitor_flagged:
        msg = "Agent performed forbidden operations. See setup_monitor_comprehensive_reason for more details."
        reason = monitor_response.get("setup_monitor_comprehensive_reason", ERR)
        if reason == ERR:
            reason = "Specific reason: " + ", ".join(
                f"{k}: {v}" for k, v in monitor_response.items() if v == True or v == ERR  # noqa: E712
            )
        judged = {
            "design_evaluation_explanation": msg,
            "design_score": 0,
            "design_error_analysis": msg,
            "conclusion_evaluation_explanation": msg,
            "conclusion_score": 0,
            "conclusion_error_analysis": msg,
            "setup_evaluation_explanation": msg,
            "setup_score": 0,
            "setup_error_analysis": msg,
            "setup_monitor_comprehensive_reason": reason,
        }
    else:
        # 1. Design and conclusion.
        task_prompt = load_prompt("eval_judge_prompt.txt").format(
            design_gt=task_data["design_complexity"],
            conclusion_gt=task_data["expected_outcome"],
            design_output=design_output,
            conclusion_output=conclusion_output,
        )
        response = query_plain(task_prompt, default_json_output)

        # 2. Setup, chunked like upstream when the patch exceeds the context.
        setup_template = load_prompt("eval_judge_setup_prompt.txt")
        partial_template = load_prompt("eval_judge_setup_partial_prompt.txt")
        if get_messages_needed(setup_output_str) > 1:
            chunks = breakdown_message(setup_output_str)
            log(f"patch too large: judging it in {len(chunks)} chunks")
            setup_response = dict(default_setup_json_output)
            for i, chunk in enumerate(chunks):
                if i == 0:
                    prompt = setup_template.format(
                        setup_gt=setup_gt, setup_output=chunk, setup_scripts=setup_scripts
                    )
                elif i == 15:
                    log("skipping remaining setup portions")
                    break
                else:
                    prompt = partial_template.format(
                        setup_gt=setup_gt,
                        setup_output=chunk,
                        setup_scripts=setup_scripts,
                        previous_partial_evaluation=setup_response,
                    )
                setup_response = query_plain(prompt, default_setup_json_output)
        else:
            prompt = setup_template.format(
                setup_gt=setup_gt, setup_output=setup_output_str, setup_scripts=setup_scripts
            )
            setup_response = query_plain(prompt, default_setup_json_output)
        judged = {**response, **setup_response}

        # 3. Execution check.
        if EXEC_CHECK and not ORACLE_VALIDATION:
            judged = {**judged, **(execution if execution is not None else execute_setup())}

    (VERIFIER_DIR / "eval_judge.json").write_text(json.dumps(judged, indent=2))

    design = to_fraction(judged.get("design_score"))
    implementation = to_fraction(judged.get("setup_score"))
    conclusion = 1.0 if str(judged.get("conclusion_score", "")).strip().lower() == "correct" else 0.0
    execution = 1.0 if judged.get("execution_success") is True else 0.0
    all_correct = 1.0 if (design == 1.0 and implementation == 1.0 and conclusion == 1.0) else 0.0
    rewards = {
        "reward": conclusion,
        "design": design,
        "implementation": implementation,
        "conclusion": conclusion,
        "execution": execution,
        "implementation_and_execution": 1.0 if (implementation == 1.0 and execution == 1.0) else 0.0,
        "all_correct": all_correct,
        "all_correct_and_execution": 1.0 if (all_correct == 1.0 and execution == 1.0) else 0.0,
        "monitor_pass": 0.0 if monitor_flagged else 1.0,
    }
    (VERIFIER_DIR / "reward.json").write_text(json.dumps(rewards, indent=2))
    (VERIFIER_DIR / "reward.txt").write_text(f"{rewards['reward']}\n")
    log(f"rewards: {rewards}")
    return rewards


if __name__ == "__main__":
    main()
