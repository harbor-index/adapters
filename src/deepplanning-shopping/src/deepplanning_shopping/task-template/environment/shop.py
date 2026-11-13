"""Terminal transport for the provided local shopping tools."""

import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"


def tools():
    # A prebuilt Python image can initialize the same pinned dependencies once.
    try:
        import numpy  # noqa: F401
        import rank_bm25  # noqa: F401
    except ImportError:
        import subprocess

        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "numpy==2.2.6",
                "rank-bm25==0.2.2",
            ],
            check=True,
            stdout=sys.stderr,
        )
    sys.path[:0] = [str(ROOT / "tools"), str(ROOT)]
    importlib.import_module("tools")
    from base_shopping_tool import TOOL_REGISTRY

    return TOOL_REGISTRY


def history():
    path = STATE / "messages.json"
    if path.exists():
        return json.loads(path.read_text())["messages"]
    prompt = json.loads((ROOT / "prompt.json").read_text())
    return [
        {"role": "system", "content": prompt["system"]},
        {"role": "user", "content": prompt["query"]},
    ]


def save(messages):
    (STATE / "messages.json").write_text(
        json.dumps({"messages": messages}, ensure_ascii=False, indent=2) + "\n"
    )


def call(name, arguments):
    registry = tools()
    if name not in registry:
        raise ValueError(f"Unknown tool: {name}")
    response = registry[name]({"database_path": str(STATE)}).call(arguments)
    messages = history()
    call_id = f"call_{len(messages):06d}"
    messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            ],
        }
    )
    messages.append({"role": "tool", "tool_call_id": call_id, "content": response})
    save(messages)
    return json.loads(response)


def finish(summary):
    messages = history()
    messages.append({"role": "assistant", "content": summary})
    save(messages)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: shop.py list | finish SUMMARY | TOOL JSON_ARGUMENTS")
    name = sys.argv[1]
    if name == "list":
        print((ROOT / "tools/shopping_tool_schema.json").read_text())
    elif name == "finish":
        finish(sys.argv[2])
        print(json.dumps({"status": "completed"}))
    else:
        print(json.dumps(call(name, json.loads(sys.argv[2])), ensure_ascii=False))


if __name__ == "__main__":
    main()
