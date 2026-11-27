"""Host-side Harbor agent for comparing single-turn ArtifactsBench generation."""

import asyncio
import json
from pathlib import Path

from harbor.agents.base import BaseAgent

from .adapter import TEMPLATE
from .original_oneshot import generate


def original_question(instruction):
    template = (TEMPLATE / "instruction.md").read_text(encoding="utf-8")
    prefix, suffix = template.split("__QUESTION__")
    if not suffix or not instruction.startswith(prefix) or not instruction.endswith(suffix):
        raise ValueError("Instruction does not match the ArtifactsBench submission protocol")
    return instruction[len(prefix):-len(suffix)]


class OneShotAgent(BaseAgent):
    @staticmethod
    def name():
        return "artifactsbench-oneshot"

    def version(self):
        return "0.1.0"

    async def setup(self, environment):
        # The generator runs on the host. The task image already contains /workspace.
        pass

    async def run(self, instruction, environment, context):
        question = original_question(instruction)
        answer, metadata = await asyncio.to_thread(generate, question, self.model_name)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        output = Path(self.logs_dir) / "answer.md"
        output.write_bytes(answer.encode("utf-8"))
        (self.logs_dir / "generation.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        context.metadata = metadata
        usage = metadata.get("usage") or {}
        context.n_input_tokens = usage.get("prompt_tokens")
        context.n_output_tokens = usage.get("completion_tokens")
        await environment.upload_file(source_path=output, target_path="/workspace/answer.md")
