#!/usr/bin/env python3
"""Run Browser Use on one Online-Mind2Web task and export an `online-mind2web-v2` trajectory.

This script is shared by BOTH sides of the parity experiment:

* Harbor side: `BrowserUseAgent` uploads it into the task container and runs it there.
* Original side: the parity fork runs it directly against the upstream task list,
  then scores the output with the upstream `src/run.py --mode WebJudge_Online_Mind2Web_eval`.

Keeping one script guarantees identical prompts, agent settings and trajectory format.

Output layout (the upstream v2 submission layout, one directory per task):

    <output_dir>/<task_id>/result.json
    <output_dir>/<task_id>/trajectory/0000.png, 0001.png, ...

Only factual browser actions are recorded in `action_history` (per the upstream
"include only factual actions, not agent outputs" rule); the agent's final answer is
stored separately in `agent_final_answer` and is not shown to WebJudge.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "online-mind2web-v2"

# Browser Use settings that differ from the library defaults. They are applied on both
# parity sides and documented in the adapter README.
DEFAULT_MAX_STEPS = 50
DEFAULT_MAX_ACTIONS_PER_STEP = 1  # one action per screenshot -> clean v2 step mapping
DEFAULT_REASONING_EFFORT = "low"  # Browser Use's own ChatOpenAI default


def build_task_prompt(confirmed_task: str, website: str) -> str:
    """The exact task text given to the agent (also used as Harbor's instruction.md)."""
    return f"{confirmed_task.strip()}\n\nStart from this website: {website.strip()}\n"


# --------------------------------------------------------------------------------------
# Action formatting (Grammar A of the v2 schema README, section 4.1)
# --------------------------------------------------------------------------------------


def _describe_element(el: Any) -> str:
    """Short, factual description of the DOM element an action targeted."""
    if el is None:
        return ""
    attrs = getattr(el, "attributes", None) or {}
    tag = (getattr(el, "node_name", "") or "").lower()
    parts = [tag] if tag else []
    for key in ("aria-label", "name", "placeholder", "title", "id", "type", "value", "href"):
        val = attrs.get(key)
        if val:
            val = str(val).strip().replace("\n", " ")
            if len(val) > 80:
                val = val[:77] + "..."
            parts.append(f"{key}='{val}'")
    return " ".join(parts)


def _target(el: Any) -> str:
    xpath = getattr(el, "x_path", None) if el is not None else None
    return f"xpath={xpath}" if xpath else "element"


def _status_suffix(result: Any) -> tuple[str, str]:
    failed = bool(result is not None and getattr(result, "error", None))
    status = "FAILED" if failed else "SUCCESS"
    return f" | {status}", status


def format_action(name: str, params: dict, el: Any, result: Any) -> tuple[str, str | None]:
    """Return (action string, action_status) for one Browser Use action."""
    suffix, status = _status_suffix(result)
    desc = _describe_element(el)
    p = params or {}

    if name == "navigate":
        return f"page -> NAVIGATE -> navigate to {p.get('url', '')}{suffix}", status
    if name == "search":
        engine = p.get("engine", "search engine")
        return f"page -> NAVIGATE -> search '{p.get('query', '')}' on {engine}{suffix}", status
    if name == "go_back":
        return f"page -> GO_BACK -> go back to the previous page{suffix}", status
    if name == "click":
        if p.get("coordinate_x") is not None and p.get("coordinate_y") is not None:
            target = f"coords({p['coordinate_x']}, {p['coordinate_y']})"
        else:
            target = _target(el)
        return f"CLICK {target} -> click {desc or 'element'}{suffix}", status
    if name == "input":
        return f"TYPE {_target(el)} -> type '{p.get('text', '')}' into {desc or 'input'}{suffix}", status
    if name == "select_dropdown":
        return f"SELECT {_target(el)} -> select '{p.get('text', '')}' in {desc or 'dropdown'}{suffix}", status
    if name == "scroll":
        direction = "down" if p.get("down", True) else "up"
        target = _target(el) if p.get("index") is not None else "page"
        return f"SCROLL {target} -> scroll {direction} {p.get('pages', 1.0)} page(s){suffix}", status
    if name == "find_text":
        return f"SCROLL page -> scroll to text '{p.get('text', '')}'{suffix}", status
    if name == "send_keys":
        return f"PRESS_KEY page -> press {p.get('keys', '')}{suffix}", status
    if name == "switch":
        return f"page -> NAVIGATE -> switch to tab {p.get('tab_id', '')}{suffix}", status
    if name == "close":
        return f"page -> NAVIGATE -> close tab {p.get('tab_id', '')}{suffix}", status
    if name == "wait":
        return f"WAIT page -> wait {p.get('seconds', 3)} seconds{suffix}", status
    if name == "upload_file":
        return f"CLICK {_target(el)} -> upload file {p.get('path', '')}{suffix}", status
    # Observation-only tools (extract, search_page, find_elements, dropdown_options,
    # screenshot, read/write files, evaluate, ...). Record the call, never its output.
    arg_text = ", ".join(f"{k}={v!r}" for k, v in p.items() if k != "interacted_element")
    if len(arg_text) > 200:
        arg_text = arg_text[:197] + "..."
    return f"WAIT page -> {name}({arg_text}){suffix}", status


# --------------------------------------------------------------------------------------
# History -> v2 conversion
# --------------------------------------------------------------------------------------


def _thought(model_output: Any) -> str | None:
    if model_output is None:
        return None
    parts = []
    for field in ("thinking", "evaluation_previous_goal", "memory", "next_goal"):
        val = getattr(model_output, field, None)
        if val:
            parts.append(f"{field}: {val}")
    return "\n".join(parts) or None


def _write_png(dst: Path, *, src_path: str | None = None, data: bytes | str | None = None) -> bool:
    if src_path and Path(src_path).exists():
        shutil.copyfile(src_path, dst)
        return True
    if data:
        dst.write_bytes(base64.b64decode(data) if isinstance(data, str) else data)
        return True
    return False


def export_v2(
    history: Any,
    task: dict,
    out_dir: Path,
    final_screenshot: bytes | str | None,
    final_url: str | None,
    run_error: str | None,
) -> dict:
    """Convert a Browser Use AgentHistoryList into the upstream v2 submission layout."""
    traj_dir = out_dir / "trajectory"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    traj_dir.mkdir(parents=True)

    steps: list[dict] = []
    final_answer: str | None = None
    last_screenshot: Path | None = None

    def add_step(action: str, status: str | None, thought: str | None, url: str | None,
                 src_path: str | None = None, data: bytes | str | None = None) -> None:
        nonlocal last_screenshot
        idx = len(steps)
        name = f"{idx:04d}.png"
        dst = traj_dir / name
        if not _write_png(dst, src_path=src_path, data=data):
            if last_screenshot is None:
                return  # nothing to show the judge for this step; skip it
            shutil.copyfile(last_screenshot, dst)
        last_screenshot = dst
        steps.append(
            {
                "step": idx,
                "screenshot": name,
                "url": url or None,
                "action": action,
                "action_status": status,
                "thought": thought,
            }
        )

    for item in (history.history if history is not None else []):
        mo = item.model_output
        if mo is None or not mo.action:
            continue
        state = item.state
        elements = list(state.interacted_element or [])
        for i, action_model in enumerate(mo.action):
            dumped = action_model.model_dump(exclude_none=True, mode="json")
            if not dumped:
                continue
            name, params = next(iter(dumped.items()))
            result = item.result[i] if i < len(item.result) else None
            if name == "done":
                final_answer = (params or {}).get("text")
                continue  # TASK_COMPLETE is appended once at the end
            el = elements[i] if i < len(elements) else None
            action, status = format_action(name, params or {}, el, result)
            url = state.url
            if name == "navigate" and params.get("url"):
                url = params["url"]
            add_step(action, status, _thought(mo) if i == 0 else None, url,
                     src_path=state.screenshot_path)

    # Terminal step: screenshot of the final page state, as WebJudge's last piece of evidence.
    answer_text = final_answer if final_answer is not None else "(no answer)"
    add_step(
        f"TASK_COMPLETE -> ANSWER: {answer_text}",
        None,
        None if run_error is None else f"run ended with error: {run_error}",
        final_url,
        data=final_screenshot,
    )

    result = {
        "schema_version": SCHEMA_VERSION,
        "task": task["confirmed_task"],
        "task_id": task["task_id"],
        "agent_final_answer": final_answer,
        "reference_length": int(task["reference_length"]),
        "action_history": steps,
    }
    (out_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


# --------------------------------------------------------------------------------------
# Agent run
# --------------------------------------------------------------------------------------


def _build_llm(model: str, reasoning_effort: str):
    from browser_use import ChatOpenAI

    model = model.split("/", 1)[1] if model.startswith("openai/") else model
    # Browser Use's built-in reasoning-model list predates GPT-6 Luna; without this it
    # sends temperature/frequency_penalty, which the API rejects for reasoning models.
    return ChatOpenAI(
        model=model,
        reasoning_models=[model],
        reasoning_effort=reasoning_effort,
    )


async def run(args: argparse.Namespace) -> int:
    from browser_use import Agent, BrowserProfile, BrowserSession

    task = json.loads(Path(args.task_json).read_text())
    prompt = Path(args.instruction_file).read_text() if args.instruction_file else build_task_prompt(
        task["confirmed_task"], task["website"]
    )
    out_dir = Path(args.output_dir) / task["task_id"]

    profile = BrowserProfile(
        headless=True,
        chromium_sandbox=False,
        keep_alive=True,  # keep the page open to grab the final screenshot
        executable_path=args.chrome_path or None,
    )
    session = BrowserSession(browser_profile=profile)
    agent = Agent(
        task=prompt,
        llm=_build_llm(args.model, args.reasoning_effort),
        browser_session=session,
        initial_actions=[{"navigate": {"url": task["website"], "new_tab": False}}],
        directly_open_url=False,
        max_actions_per_step=args.max_actions_per_step,
        use_judge=False,  # Browser Use's own LLM judge is irrelevant here and costs tokens
        calculate_cost=True,
    )

    history = None
    run_error: str | None = None
    try:
        history = await agent.run(max_steps=args.max_steps)
    except Exception as exc:  # still export whatever trajectory exists
        run_error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        history = getattr(agent, "history", None)

    final_png, final_url = None, None
    try:
        final_png = await session.take_screenshot()
        final_url = await session.get_current_page_url()
    except Exception:
        traceback.print_exc()

    result = export_v2(history, task, out_dir, final_png, final_url, run_error)

    usage = getattr(history, "usage", None)
    summary = {
        "task_id": task["task_id"],
        "n_steps": len(result["action_history"]),
        "agent_final_answer": result["agent_final_answer"],
        "is_done": bool(history and history.is_done()),
        "run_error": run_error,
        "usage": usage.model_dump(mode="json") if usage is not None else None,
    }
    (out_dir / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, default=str))

    try:
        await session.kill()
    except Exception:
        pass
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--task-json", required=True, help="JSON with task_id, confirmed_task, website, reference_length")
    parser.add_argument("--instruction-file", default=None, help="Task prompt; defaults to build_task_prompt()")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--max-actions-per-step", type=int, default=DEFAULT_MAX_ACTIONS_PER_STEP)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--chrome-path", default=os.environ.get("CHROME_PATH"))
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
