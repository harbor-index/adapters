"""cavebench-llm: Harbor parity agent that runs the authors' CaveBench protocol.

The agent installs the upstream inference code at the pinned commit (stdlib
only; each file is SHA-256 checked) and answers the task's question with
`run.py --paper-settings` semantics: the archived prompt plus frames F0..Fk in
one user message, the model's recorded per-question settings from
`inference/profiles.json`, streaming with the upstream answer controller, and at
most one same-model answer-completion request. See run_cavebench_llm.py.

Usage (from the adapters repo root):
    set -a; . ~/.your-openrouter.env; set +a     # exports OPENROUTER_API_KEY
    PYTHONPATH=src/cavebench harbor run -p datasets/cavebench-parity \
        --agent-import-path cavebench_llm_agent:CaveBenchLLMAgent \
        -m deepseek/deepseek-v4.1-flash-1017 -n 4
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

UPSTREAM_COMMIT = "c0f8cbee47af72a9437b5ab82f3f03b391e2a819"
UPSTREAM_RAW = f"https://raw.githubusercontent.com/PIER-Lab/CaveBench/{UPSTREAM_COMMIT}"
UPSTREAM_DIR = "/opt/cavebench-upstream"
# Files the protocol imports, pinned by SHA-256 at UPSTREAM_COMMIT.
UPSTREAM_FILES = {
    "run.py": "b7dedb7d3151553c2c8134cbadd55a763fedf4a289723b7baf0fefe4262295f0",
    "contracts.py": "c5562a968209df435a87e9b21fa73c5fdce46bf54707a0ce14e1490151438415",
    "prompt_templates.py": "11f30691277a8981154a7e186fca23afffe22e55e395ac8e8440133bb17c3aa8",
    "prompts/apparatus.json": "5c3ebbd091bbf9a17215c03f6e91651ab96ed6d615c813462a65252cb8514266",
    "inference/completion.py": "22180db76be6d25586d149e37242e580dd7c0d805c19b31e03f8ee0e8c57ca22",
    "inference/controller.py": "d2b6b21018da8ba34576c85aae2461f8b071d35e2fa1a4bcf7a642ae828b440e",
    "inference/protocol.py": "2c001eb41cfb0ce6edec97cc43ee8a3710e6bbdb13ce66828bf7fd00774046af",
    "inference/transport.py": "bab57297281c725b2bb11107a2570a9d7506ec8fe9702b20e31bd0ddc6481bc8",
    "inference/profiles.json": "534f562fffeb556e38d8c2ee3e7de88e7c2ab3bbe558229938e694ad2ca833b6",
    "LICENSE": "8e74ddaaccfec8e2d31176bdfba3389010a9c39b5a648043554bcc1b6b763f03",
}
RUNNER_FILENAME = "run_cavebench_llm.py"
RUNNER_TARGET = f"/opt/{RUNNER_FILENAME}"
QUESTION_PATH = "/opt/cavebench/question.json"
TRACE_FILENAME = "cavebench-llm-trace.json"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
HARBOR_IO_SEPARATOR = "\n\n---\n\n## Harbor task I/O\n"


class CaveBenchLLMAgent(BaseInstalledAgent):
    def __init__(self, *args: Any, endpoint: str = DEFAULT_ENDPOINT, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._endpoint = endpoint

    @staticmethod
    def name() -> str:
        return "cavebench-llm"

    def version(self) -> str | None:
        return self._version or f"1.0+upstream.{UPSTREAM_COMMIT[:7]}"

    async def install(self, environment: BaseEnvironment) -> None:
        fetch = " && ".join(
            f"mkdir -p {shlex.quote(str(Path(UPSTREAM_DIR, rel).parent))}"
            f" && curl -fsSL --retry 4 -o {shlex.quote(f'{UPSTREAM_DIR}/{rel}')}"
            f" {shlex.quote(f'{UPSTREAM_RAW}/{rel}')}"
            for rel in UPSTREAM_FILES
        )
        checks = "\n".join(f"{sha}  {rel}" for rel, sha in UPSTREAM_FILES.items())
        await self.exec_as_root(
            environment,
            command=(
                f"{fetch} && cd {UPSTREAM_DIR} && printf '%s\\n' {shlex.quote(checks)}"
                " | sha256sum -c --quiet -"
            ),
        )
        runner = self.logs_dir / RUNNER_FILENAME
        runner.write_text(
            Path(__file__).with_name(RUNNER_FILENAME).read_text(encoding="utf-8"), encoding="utf-8"
        )
        await environment.upload_file(runner, RUNNER_TARGET)

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        if not self.model_name:
            raise ValueError("cavebench-llm needs a model, e.g. -m deepseek/deepseek-v4.1-flash-1017")
        model = self.model_name.removeprefix("openrouter/")
        key = self._get_env("CAVEBENCH_API_KEY", "OPENROUTER_API_KEY")
        if not key:
            raise ValueError("set OPENROUTER_API_KEY (or CAVEBENCH_API_KEY) in the host environment")

        # The archived prompt is the part of the instruction before the Harbor
        # I/O note; the runner sends the upstream question's prompt and checks
        # here that both agree.
        prompt = instruction.split(HARBOR_IO_SEPARATOR, 1)[0]
        (self.logs_dir / "instruction-prompt.txt").write_text(prompt, encoding="utf-8")

        command = " ".join([
            "python3", RUNNER_TARGET,
            "--upstream", UPSTREAM_DIR,
            "--question", QUESTION_PATH,
            "--frames", "/workspace/frames",
            "--model", shlex.quote(model),
            "--endpoint", shlex.quote(self._endpoint),
            "--expect-prompt-sha256", _sha256(prompt),
            "--answer", "/workspace/answer.json",
            "--trace", f"/logs/agent/{TRACE_FILENAME}",
        ])
        await self.exec_as_agent(
            environment,
            command=f"{command} 2>&1 | tee /logs/agent/cavebench-llm.txt",
            env={"CAVEBENCH_API_KEY": key},
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        path = self.logs_dir / TRACE_FILENAME
        if not path.exists():
            return
        try:
            trace = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        inputs = outputs = 0
        for stage in trace.get("stages", []):
            usage = stage.get("usage") or {}
            inputs += int(usage.get("prompt_tokens") or 0)
            outputs += int(usage.get("completion_tokens") or 0)
        context.n_input_tokens = inputs or None
        context.n_output_tokens = outputs or None
        context.cost_usd = trace.get("cost_usd")
        context.metadata = {
            "status": trace.get("status"),
            "error_type": trace.get("error_type"),
            "stages": [s.get("stage") for s in trace.get("stages", [])],
            "settings": trace.get("settings"),
        }


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
