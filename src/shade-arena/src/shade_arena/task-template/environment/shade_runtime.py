"""Run and replay SHADE-Arena tool turns using the upstream runtime."""

import argparse
import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

from openai import NOT_GIVEN

ROOT = Path(__file__).resolve().parent
UPSTREAM = ROOT / "upstream"
SETTINGS = json.loads((ROOT / "task.json").read_text(encoding="utf-8"))
TRAJECTORY = Path(os.getenv("SHADE_TRAJECTORY", "/workspace/shade-trajectory.json"))
sys.path.insert(0, str(UPSTREAM))

from llms import LLMFactory
from llms.anthropic_llm import AnthropicLLM
from utils.functions_runtime import FunctionCall
from utils.llm_utils import get_appropriate_llm
from utils.pipeline_elements import ToolsExecutor
from utils.pipeline_utils import PipelineUtils


class AnthropicRuntimeLLM(AnthropicLLM):
    def calculate_cost(self, input_tokens: int, output_tokens: int) -> float | None:
        if self.model not in self._MODEL_PRICING:
            return None
        return super().calculate_cost(input_tokens, output_tokens)


LLMFactory.register_provider("anthropic", AnthropicRuntimeLLM)


def make_model(name):
    provider, separator, model = name.partition("/")
    if not separator or not model:
        raise ValueError(f"Use provider/model for SHADE-Arena models: {name}")
    if model.rsplit("/", 1)[-1] in {"claude-sonnet-5-5", "claude-haiku-5-5"}:
        # The 5.5 models reject a custom temperature and count reasoning toward max_tokens.
        if provider == "anthropic":
            return LLMFactory.create(provider, model, temperature=None, max_tokens=16384)
        if provider == "openai":
            return get_appropriate_llm(model, provider, temperature=NOT_GIVEN, max_tokens=16384)
    return get_appropriate_llm(model, provider)


def create_environment():
    module = importlib.import_module(SETTINGS["module"])
    return getattr(module, SETTINGS["environment"]).create()


def load_turns():
    if not TRAJECTORY.is_file():
        return []
    turns = json.loads(TRAJECTORY.read_text(encoding="utf-8"))
    if not isinstance(turns, list):
        raise ValueError("SHADE-Arena trajectory must be a JSON list of turns")
    return turns


def save_turns(turns):
    temp = TRAJECTORY.with_suffix(".tmp")
    temp.write_text(json.dumps(turns, indent=2) + "\n", encoding="utf-8")
    temp.replace(TRAJECTORY)


def tool_turn(function, args):
    return {"content": None, "calls": [{"function": function, "args": args}], "execute": True}


async def replay(turns):
    environment = create_environment()
    runtime = PipelineUtils.create_runtime(environment)
    executor = ToolsExecutor()
    messages = [
        {"role": "system", "content": SETTINGS["system"]},
        {"role": "user", "content": SETTINGS["user"]},
    ]
    for index, turn in enumerate(turns):
        if "followup" in turn:
            messages.append({"role": "user", "content": turn["followup"]})
            continue
        calls = [
            FunctionCall.model_validate({"id": f"{index}-{i}", **call})
            for i, call in enumerate(turn.get("calls", []))
        ]
        messages.append({"role": "assistant", "content": turn["content"], "tool_calls": calls or None})
        if calls and turn["execute"]:
            result = await executor.query(SETTINGS["user"], runtime, environment, messages, {})
            messages = list(result.messages)
    return messages, environment


async def run_native(model_name):
    pipeline = PipelineUtils(None)
    environment = create_environment()
    result, _ = await pipeline.run_agent_pipeline(
        pipeline.prepare_tools_pipeline(SETTINGS["system"], make_model(model_name), max_iters=150),
        pipeline.create_runtime(environment),
        environment,
        SETTINGS["user"],
        {"temperature": 1, "use_cache": False, "environment_states": [environment.copy(deep=True)]},
    )
    turns = []
    for i, message in enumerate(result.messages):
        if message["role"] == "assistant":
            turns.append({
                "content": message["content"],
                "calls": [call.model_dump(mode="json") for call in message["tool_calls"] or []],
                "execute": i + 1 < len(result.messages) and result.messages[i + 1]["role"] == "tool",
            })
        elif i > 1 and message["role"] == "user":
            turns.append({"followup": message["content"]})
    save_turns(turns)


async def cli(args):
    if args.command == "tools":
        runtime = PipelineUtils.create_runtime(create_environment())
        for function in runtime.functions.values():
            print(json.dumps({
                "name": function.name,
                "description": function.description,
                "parameters": function.parameters.model_json_schema(),
            }))
        return
    if args.command == "native":
        await run_native(args.model)
        return

    # CLI calls can be issued in separate shell processes against one trial.
    import fcntl

    with TRAJECTORY.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        turns = load_turns()
        if any(call["function"] == "end_task" for turn in turns for call in turn.get("calls", [])):
            raise ValueError("The task has already ended")
        if args.command == "say":
            turns.append({"content": args.message, "calls": [], "execute": False})
            save_turns(turns)
            return
        arguments = json.loads(args.arguments)
        turn = tool_turn(args.function, arguments)
        turn["execute"] = args.function != "end_task"
        turns.append(turn)
        messages, _ = await replay(turns)
        save_turns(turns)
        if turn["execute"]:
            print(messages[-1].get("error") or messages[-1]["content"])


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("tools", help="List tools and their argument schemas")
    call = commands.add_parser("call", help="Call a tool")
    call.add_argument("function")
    call.add_argument("arguments", nargs="?", default="{}")
    commands.add_parser("say", help="Record an assistant message").add_argument("message")
    commands.add_parser("native").add_argument("model")
    asyncio.run(cli(parser.parse_args()))


if __name__ == "__main__":
    main()
