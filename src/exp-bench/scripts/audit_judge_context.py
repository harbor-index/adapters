"""List tasks whose setup-judge prompt exceeds the judge context without any agent patch.

The setup judge (upstream judge.py, ported in tests/judge.py) formats
``eval_judge_setup_prompt.txt`` with the task's method/requirements, the agent's patch
and the concatenated reference source files. The reference files are embedded in every
prompt and every chunk, so if they alone exceed the judge's context, no agent output can
be graded. This script reports those tasks; ``JUDGE_CONTEXT_OVERFLOW`` in adapter.py is
its output for o3-mini (200k tokens).

Usage (from src/exp-bench):
    uv run --with tiktoken==0.9.0 python scripts/audit_judge_context.py [--model o3-mini] [--context 200000]
"""

from __future__ import annotations

import argparse
import gzip
import json

import tiktoken

from exp_bench.adapter import (
    DEFAULT_CACHE_DIR,
    DEFAULT_DATA_FILE,
    JUDGE_CONTEXT_OVERFLOW,
    TEMPLATE_DIR,
    fetch_source,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="o3-mini")
    parser.add_argument("--context", type=int, default=200000)
    parser.add_argument("--warn-below", type=int, default=50000, help="Also list tasks leaving less room than this for the patch")
    args = parser.parse_args()

    encoding = tiktoken.encoding_for_model(args.model)

    def count(text: str) -> int:
        return len(encoding.encode(text, disallowed_special=()))

    template = (TEMPLATE_DIR / "tests" / "prompts" / "eval_judge_setup_prompt.txt").read_text()
    with gzip.open(DEFAULT_DATA_FILE, "rt", encoding="utf-8") as f:
        tasks = json.load(f)["tasks"]

    overflow, tight = [], []
    for task in tasks:
        if any(sha is None for sha in task["setup_script_sha256"].values()) or "README.md" in task["masked_files"]:
            continue  # excluded for masking reasons
        scripts = ""
        for path in task["setup_script_paths"]:
            content = fetch_source(task["repo"], task["commit"], path, task["setup_script_sha256"][path], DEFAULT_CACHE_DIR)
            scripts += f"{path}:\n\n{content}\n\n"
        data = task["task_data"]
        setup_gt = data["requirements"] if "requirements" in data else data["method"]
        tokens = count(template.format(setup_gt=setup_gt, setup_output="", setup_scripts=scripts))
        if tokens >= args.context:
            overflow.append((task["task_id"], tokens))
        elif args.context - tokens < args.warn_below:
            tight.append((task["task_id"], tokens))

    print(f"Setup-judge prompt over {args.context} tokens with an empty patch ({len(overflow)} tasks):")
    for task_id, tokens in sorted(overflow, key=lambda x: -x[1]):
        print(f'    "{task_id}": {tokens},')
    print(f"Less than {args.warn_below} tokens left for the agent patch ({len(tight)} tasks):")
    for task_id, tokens in sorted(tight, key=lambda x: -x[1]):
        print(f"    {task_id}: {tokens}")
    if (args.model, args.context) == ("o3-mini", 200000) and set(JUDGE_CONTEXT_OVERFLOW) != {t for t, _ in overflow}:
        print("NOTE: adapter.JUDGE_CONTEXT_OVERFLOW differs from this result")


if __name__ == "__main__":
    main()
