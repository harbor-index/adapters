"""Compare native and Harbor request construction and artifact transport without a model."""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from harbor.models.task.task import Task, strip_canary

from artifactsbench.adapter import TEMPLATE, ArtifactsBenchAdapter
from artifactsbench.oneshot import original_question
from artifactsbench.original_oneshot import generate_original


async def check(dataset, work):
    adapter = ArtifactsBenchAdapter(work / "tasks", dataset=dataset)
    rows = adapter._records()
    template = (TEMPLATE / "instruction.md").read_text(encoding="utf-8")
    for row in rows:
        instruction = strip_canary(template.replace("__QUESTION__", row["question"]))
        if original_question(instruction) != row["question"]:
            raise ValueError(f"Question changed for index {row['index']}")

    selected = [rows[0]["index"], rows[-1]["index"]]
    adapter.task_ids = selected
    adapter.run()
    output = work / "native.jsonl"
    answer = "  exact text\r\n含中文\n\n"
    model = "fixture/model"
    response = {
        "model": model,
        "choices": [{"message": {"content": answer}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 5},
    }
    env = {
        "ARTIFACTSBENCH_GENERATOR_BASE_URL": "https://unused.invalid/v1",
        "ARTIFACTSBENCH_GENERATOR_TEMPERATURE": "0.25",
        "ARTIFACTSBENCH_GENERATOR_MAX_COMPLETION_TOKENS": "256",
    }
    with patch.dict(os.environ, env), patch("artifactsbench.original_oneshot._complete", return_value=response) as post:
        generate_original(dataset, selected, model, output)
        native_rows = [json.loads(line) for line in output.read_bytes().splitlines()]
        if [r["index"] for r in native_rows] != selected:
            raise ValueError("Native output cohort changed")
        native_calls = list(post.call_args_list)
        post.reset_mock()
        for row in native_rows:
            task = Task(adapter.output_dir / f"artifactsbench-{row['index']:04d}")
            agent = AgentFactory.create_agent_from_import_path(
                "artifactsbench.oneshot:OneShotAgent", work / str(row["index"]), model_name=model,
            )
            environment = AsyncMock()
            context = AgentContext()
            await agent.setup(environment)
            await agent.run(task.instruction, environment, context)
            args = environment.upload_file.await_args.kwargs
            if (args["target_path"] != "/workspace/answer.md" or
                    Path(args["source_path"]).read_bytes() != row["answer"].encode("utf-8") or
                    row["answer"] != answer or context.n_output_tokens != 5):
                raise ValueError("Answer bytes, destination, or token usage changed")
        if native_calls != post.call_args_list:
            raise ValueError("Native and Harbor requests differ")
        for call, row in zip(native_calls, native_rows, strict=True):
            expected = {"messages": [{"role": "user", "content": row["question"]}], "model": model,
                        "temperature": 0.25, "max_completion_tokens": 256}
            if call.args != (expected,):
                raise ValueError("Unexpected generator request")
    print(json.dumps({"source_questions_matched": len(rows), "paired_fixture_ids": selected,
                      "real_model_requests": 0}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        asyncio.run(check(args.dataset, Path(temp)))
