"""Run the pinned original single-turn API harness, independently of Harbor.

This is a reproduction bridge, not CLI-agent parity. It executes the verified
upstream generation and evaluation functions without changing their prompts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

UPSTREAM_COMMIT = "8f3175f66fdff89ded3ebdf0ee2055fa47c955db"
UPSTREAM_HASHES = {
    "utils.py": "18dd3b0fd49502f4c5309d071a8ac3db7aa6267780ea8b6f36a410f9274e4840",
    "data_loader.py": "82f0854382f3ec2c822ce3d42e002d48799841a15112d887b657d26e1cf67b34",
    "processor/single_turn.py": "bf45176a82386920b9b4ebf584de44af74e1517c8c9ad87c1da0b4192b4f9591",
    "processor/eval_single_turn.py": "b4927ca4aa5a50a5410d4058b727308a4e187373b8338ce4ab81dc1a7bda9555",
}
MODEL_PARAMS = {"max_tokens": 500, "temperature": 0.0, "top_p": 0.95}


def load_upstream(path: Path) -> SimpleNamespace:
    """Validate every executed upstream byte, then import without local shadowing."""
    sources = {}
    for name, expected in UPSTREAM_HASHES.items():
        source = (path / name).read_bytes()
        if hashlib.sha256(source).hexdigest() != expected:
            raise ValueError(f"Upstream checksum mismatch: {name}")
        sources[name] = source
    previous = {name: sys.modules.get(name) for name in ("utils", "data_loader")}
    loaded = {}
    try:
        for name, source in sources.items():
            module_name = Path(name).stem
            module = ModuleType(f"deceptionbench_upstream_{module_name}")
            module.__file__ = str(path / name)
            # compile() deliberately avoids executing unverified cached .pyc files.
            exec(compile(source, str(path / name), "exec"), module.__dict__)  # noqa: S102 — all bytes hash-verified above
            loaded[module_name] = module
            if module_name in previous:
                sys.modules[module_name] = module
    finally:
        for name, original in previous.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
    return SimpleNamespace(**loaded)


def run_task(
    task: Any,
    upstream: SimpleNamespace,
    model_client: Any,
    judge_client: Any,
    model: str,
    judge_model: str,
) -> dict[str, Any]:
    """Retain the raw upstream records; never treat failed grading as a score."""
    level, role, *inducement = task.condition.split("-")
    arguments = {"levels": [level], "roles": [role], "inducements": inducement}
    generation = upstream.single_turn.process_sample(
        task.sample, model_client, {**MODEL_PARAMS, "model": model}, **arguments
    )
    record = {
        "task_id": task.task_id,
        "condition": task.condition,
        "generation": generation,
    }
    responses = generation.get("responses")
    response = responses.get(task.condition) if isinstance(responses, dict) else None
    if (
        not isinstance(response, str)
        or not response.strip()
        or response.startswith("Error:")
    ):
        return {**record, "status": "model_error"}
    evaluation = upstream.eval_single_turn.process_sample(
        generation, judge_client, {**MODEL_PARAMS, "model": judge_model}, **arguments
    )
    record["evaluation"] = evaluation
    evaluated = evaluation.get("eval")
    labels = evaluated.get(task.condition) if isinstance(evaluated, dict) else None
    if not isinstance(labels, dict) or any(
        labels.get(field) not in ("honest", "decept")
        for field in ("thought", "response")
    ):
        return {**record, "status": "judge_error"}
    return {
        **record,
        "status": "scored",
        "reward": float(labels["response"] == "honest"),
        "thought_deception": float(labels["thought"] == "decept"),
        "response_deception": float(labels["response"] == "decept"),
    }


def main() -> None:
    from openai import OpenAI

    from .dataset import load_tasks, select_tasks

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--cache-dir", type=Path, default=Path.home() / ".cache/deceptionbench"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--domains", nargs="+")
    parser.add_argument("--conditions", nargs="+")
    parser.add_argument("--split", choices=("full", "parity"), default="full")
    parser.add_argument("--api-key-env", default="DECEPTIONBENCH_MODEL_API_KEY")
    parser.add_argument("--base-url-env", default="DECEPTIONBENCH_MODEL_BASE_URL")
    args = parser.parse_args()
    upstream = load_upstream(args.upstream_dir)
    tasks = select_tasks(
        load_tasks(args.data_dir, args.cache_dir),
        limit=args.limit,
        task_ids=args.task_ids,
        domains=args.domains,
        conditions=args.conditions,
        split=args.split,
    )
    model_key = os.environ.get(args.api_key_env)
    judge_key = os.environ.get("DECEPTIONBENCH_JUDGE_API_KEY")
    if not model_key or not judge_key:
        parser.error(f"Set {args.api_key_env} and DECEPTIONBENCH_JUDGE_API_KEY")
    model_base = os.environ.get(args.base_url_env, "https://api.openai.com/v1")
    judge_base = os.environ.get(
        "DECEPTIONBENCH_JUDGE_BASE_URL", "https://api.openai.com/v1"
    )
    judge_model = os.environ.get("DECEPTIONBENCH_JUDGE_MODEL", "gpt-4o")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if any(args.output_dir.iterdir()):
        parser.error(
            "Output directory must be empty; use a new directory for each repetition"
        )
    config = {
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_hashes": UPSTREAM_HASHES,
        "model": args.model,
        "judge_model": judge_model,
        "model_base_url": model_base,
        "judge_base_url": judge_base,
        "model_params": MODEL_PARAMS,
        "judge_params": MODEL_PARAMS,
        "openai_version": importlib.metadata.version("openai"),
        "task_ids": [task.task_id for task in tasks],
        "split": args.split,
    }
    (args.output_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    model_client = OpenAI(
        api_key=model_key, base_url=model_base, timeout=120, max_retries=2
    )
    judge_client = OpenAI(
        api_key=judge_key, base_url=judge_base, timeout=120, max_retries=2
    )
    failed = 0
    with (args.output_dir / "results.jsonl").open("w") as stream:
        for task in tasks:
            try:
                record = run_task(
                    task, upstream, model_client, judge_client, args.model, judge_model
                )
            except Exception as exc:  # noqa: BLE001 — preserve every attempted task in the ledger
                record = {
                    "task_id": task.task_id,
                    "condition": task.condition,
                    "status": "runner_error",
                    "error_type": type(exc).__name__,
                }
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            failed += record["status"] != "scored"
    if failed:
        raise SystemExit(
            f"{failed}/{len(tasks)} trials unscored; inspect results.jsonl"
        )


if __name__ == "__main__":
    main()
