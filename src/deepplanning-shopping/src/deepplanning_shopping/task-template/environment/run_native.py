"""Run the unchanged ShoppingFnAgent using Harbor's task paths."""

import argparse
import hashlib
import json
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NATIVE = ROOT / "native_agent"


def verify_sources():
    source = json.loads((NATIVE / "source.json").read_text())
    for name, expected in source["sha256"].items():
        actual = hashlib.sha256((NATIVE / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Native source checksum mismatch: {name}")
    return source


def run(model, max_llm_calls):
    source = verify_sources()
    prompt = json.loads((ROOT / "prompt.json").read_text())
    # Preserve the upstream loader's tools/ and database/case_<id>/ layout.
    tools = NATIVE / "tools"
    if not tools.exists():
        tools.symlink_to(ROOT / "tools", target_is_directory=True)
    database = NATIVE / "database"
    database.mkdir(exist_ok=True)
    case = database / "case_1"
    if not case.exists():
        case.symlink_to(ROOT / "state", target_is_directory=True)

    from native_agent.agent.shopping_agent import ShoppingFnAgent

    agent = ShoppingFnAgent(
        model=model,
        tool_schema_path=str(ROOT / "tools/shopping_tool_schema.json"),
        sample_id="1",
        database_base_path=str(database),
    )
    messages = agent.run(
        user_query=prompt["query"],
        system_prompt=prompt["system"],
        max_llm_calls=max_llm_calls,
        save_messages=True,
        sample_id="1",
    )
    summary = {
        "source_revision": source["revision"],
        "openai_version": version("openai"),
        "model_config_name": model,
        "max_llm_calls_per_stage": max_llm_calls,
        "message_count": len(messages),
        "is_completed": bool(messages)
        and messages[-1]["role"] == "assistant"
        and not messages[-1].get("tool_calls"),
    }
    (ROOT / "state/native-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--max-llm-calls", type=int, default=400)
    args = parser.parse_args()
    if args.check:
        print(json.dumps(verify_sources()))
        return
    if not args.model or args.max_llm_calls < 1:
        parser.error("--model and a positive --max-llm-calls are required")
    print(json.dumps(run(args.model, args.max_llm_calls)))


if __name__ == "__main__":
    main()
