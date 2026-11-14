"""Check all 900 pinned tasks against upstream using in-memory clients only.

Requires existing local source/data directories; this script downloads nothing,
instantiates no API clients, and measures prompt fidelity rather than model parity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deceptionbench.adapter import DeceptionBenchAdapter
from deceptionbench.dataset import (
    DATA_HASHES,
    DATASET_ID,
    DATASET_REVISION,
    UPSTREAM_COMMIT,
    load_tasks,
)
from deceptionbench.original import MODEL_PARAMS, UPSTREAM_HASHES, load_upstream
from deceptionbench.protocol import build_judge_prompt
from deceptionbench.verifier import call_judge

TIMESTAMP = "2026-11-14 12:34:56"
RESPONSE = (
    '```json\n{"thought":"Read the display.",'
    '"response":"The display says café 42."}\n```'
)


class FakeClient:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.answer))]
        )


def check_fidelity(upstream_dir: Path, data_dir: Path) -> dict[str, Any]:
    upstream = load_upstream(upstream_dir)
    tasks = load_tasks(data_dir)
    if len(tasks) != 900:
        raise ValueError(f"Expected all 900 pinned tasks, found {len(tasks)}")
    hashes = []
    with tempfile.TemporaryDirectory(prefix="deceptionbench-fidelity-") as temporary:
        adapter = DeceptionBenchAdapter(Path(temporary))
        for task in tasks:
            level, role, *inducement = task.condition.split("-")
            arguments = {"levels": [level], "roles": [role], "inducements": inducement}
            generation_client = FakeClient(RESPONSE)
            generation = upstream.single_turn.process_sample(
                task.sample,
                generation_client,
                {**MODEL_PARAMS, "model": "synthetic-generation"},
                **arguments,
            )
            generation["timestamp"] = TIMESTAMP
            destination = Path(temporary) / task.task_id
            adapter._render(destination, task)
            native_prompt = (destination / "environment/prompt.txt").read_text(
                encoding="utf-8"
            )
            expected_generation = dict(
                model="synthetic-generation",
                messages=[{"role": "user", "content": native_prompt}],
                **MODEL_PARAMS,
            )
            if generation_client.calls != [expected_generation]:
                raise ValueError(f"Generation prompt/request mismatch: {task.task_id}")

            answer = "###FINAL: " + json.dumps(
                {task.condition: {"thought": "honest", "response": "honest"}}
            )
            original_judge = FakeClient(answer)
            evaluation = upstream.eval_single_turn.process_sample(
                generation,
                original_judge,
                {**MODEL_PARAMS, "model": "synthetic-judge"},
                **arguments,
            )
            judge_prompt = build_judge_prompt(
                task.sample, task.condition, RESPONSE, TIMESTAMP
            )
            adapter_judge = FakeClient(answer)
            result = call_judge(
                judge_prompt, task.condition, adapter_judge, "synthetic-judge"
            )
            if (
                len(original_judge.calls) != 1
                or original_judge.calls != adapter_judge.calls
            ):
                raise ValueError(f"Judge prompt/request mismatch: {task.task_id}")
            if result["labels"] != evaluation["eval"][task.condition]:
                raise ValueError(f"Judge parsing mismatch: {task.task_id}")
            hashes.append(
                {
                    "task_id": task.task_id,
                    "generation_sha256": hashlib.sha256(
                        native_prompt.encode()
                    ).hexdigest(),
                    "judge_sha256": hashlib.sha256(judge_prompt.encode()).hexdigest(),
                }
            )

    serialized_hashes = (
        "\n".join(
            json.dumps(record, sort_keys=True, separators=(",", ":"))
            for record in hashes
        )
        + "\n"
    )
    return {
        "status": "passed",
        "api_calls": 0,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_source_sha256": UPSTREAM_HASHES,
        "dataset": DATASET_ID,
        "dataset_revision": DATASET_REVISION,
        "dataset_file_sha256": DATA_HASHES,
        "tasks_checked": len(tasks),
        "generation_prompt_request_matches": len(hashes),
        "judge_prompt_request_matches": len(hashes),
        "judge_parser_matches": len(hashes),
        "condition_counts": dict(Counter(task.condition for task in tasks)),
        "domain_counts": dict(Counter(task.domain for task in tasks)),
        "fixed_generation_timestamp": TIMESTAMP,
        "synthetic_response_sha256": hashlib.sha256(RESPONSE.encode()).hexdigest(),
        "ordered_per_task_prompt_hashes_sha256": hashlib.sha256(
            serialized_hashes.encode()
        ).hexdigest(),
        "first_task_prompt_hashes": hashes[0],
        "last_task_prompt_hashes": hashes[-1],
        "method": (
            "Rendered all tasks and compared environment/prompt.txt with captured "
            "upstream generation requests; compared adapter judge requests and parsed "
            "labels with captured upstream evaluation requests/results. Source/data "
            "checksums are verified before use. All clients are in-memory fakes. "
            "This proves prompt fidelity, not empirical model-score parity."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-file", type=Path)
    args = parser.parse_args()
    evidence = check_fidelity(args.upstream_dir, args.data_dir)
    output = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if args.output_file:
        args.output_file.parent.mkdir(parents=True, exist_ok=True)
        args.output_file.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
