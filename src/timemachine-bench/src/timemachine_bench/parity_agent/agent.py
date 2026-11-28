"""Harbor wrapper around the upstream TimeMachine-bench baseline agent.

The upstream agent (agents/src/main/solve_openai.py at tohoku-nlp/timemachine-bench
@9928dbf) is a LangGraph tool-calling loop with its own file tools and a
test-execution tool. This wrapper installs that code, unchanged except for the
test-execution tool, with its exact dependency pins in a virtualenv inside the
task container (/opt/tmb-agent), and runs it against /work. The virtualenv is
separate from the interpreter the tests use, and grading happens in a fresh
container anyway.

    PYTHONPATH=src/timemachine-bench/src harbor run -p <tasks> \
        --agent-import-path timemachine_bench.parity_agent.agent:TimeMachineParityAgent \
        -m openai/<model>

The model is called through the OpenAI-compatible API exactly as upstream:
langchain ChatOpenAI(model=<model>, temperature=0), configured by
OPENAI_API_KEY and, optionally, OPENAI_BASE_URL.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .inputs import parse_instruction

RUNNER_DIR = Path(__file__).parent / "runner"
REMOTE_ROOT = "/opt/tmb-agent"
REMOTE_RUNNER = f"{REMOTE_ROOT}/runner"
REMOTE_VENV = f"{REMOTE_ROOT}/venv"
REMOTE_TASK = f"{REMOTE_ROOT}/task"


class TimeMachineParityAgent(BaseAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return "timemachine-agent"

    def version(self) -> str | None:
        return "upstream-9928dbf"

    async def setup(self, environment: BaseEnvironment) -> None:
        await environment.exec(f"mkdir -p {REMOTE_ROOT} {REMOTE_TASK}", user="root")
        await environment.upload_dir(RUNNER_DIR, REMOTE_RUNNER)
        result = await environment.exec(
            f"python3 -m venv {REMOTE_VENV} && "
            f"{REMOTE_VENV}/bin/pip install -q --no-cache-dir --disable-pip-version-check "
            f"--index-url https://pypi.org/simple -r {REMOTE_RUNNER}/requirements.txt",
            timeout_sec=900,
            user="root",
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"installing the upstream agent failed ({result.return_code}):\n"
                f"{result.stdout}\n{result.stderr}"
            )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        task = parse_instruction(instruction)
        with tempfile.TemporaryDirectory() as tmp:
            test_files = Path(tmp) / "test_files.txt"
            # upstream 02_extract_assets.py: "\n".join(test files) + "\n"
            test_files.write_text("\n".join(task["test_files"]) + "\n")
            dep_versions = Path(tmp) / "dep_versions.txt"
            dep_versions.write_text(task["dependency_versions"])
            await environment.upload_file(test_files, f"{REMOTE_TASK}/test_files.txt")
            await environment.upload_file(dep_versions, f"{REMOTE_TASK}/dep_versions.txt")

        model = self.model_name or ""
        if "/" in model:
            model = model.split("/", 1)[1]
        env = {"OPENAI_MODEL_NAME": model, "TMB_TEST_SCRIPT": str(task["test_script"])}
        for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
            if os.environ.get(key):
                env[key] = os.environ[key]

        log_path = f"{self.environment_logs_dir.as_posix()}/solve_openai.log"
        # Same invocation as upstream runners/main/solve_openai.sh, which pipes
        # stdout and stderr through `tee` into the run log.
        command = (
            f"cd {REMOTE_RUNNER} && {REMOTE_VENV}/bin/python -u solve_openai.py "
            f"--repo_name {shlex.quote(str(task['repo_name']))} "
            f"--host_repo_dir /work "
            f"--test_files_txt_path {REMOTE_TASK}/test_files.txt "
            f"--dep_versions_txt_path {REMOTE_TASK}/dep_versions.txt "
            f"2>&1 | tee {shlex.quote(log_path)}"
        )
        result = await environment.exec(command, env=env, user="root")
        output = (result.stdout or "") + (result.stderr or "")

        def last_int(pattern: str) -> int | None:
            values = re.findall(pattern, output, re.M)
            return int(values[-1]) if values else None

        context.n_input_tokens = last_int(r"^Total input tokens: (\d+)$")
        context.n_output_tokens = last_int(r"^Total output tokens: (\d+)$")
        context.metadata = {
            "upstream_test_success": "##### Test Success #####" in output,
            "num_llm_calls": last_int(r"^Current num_llm_calls: (\d+)$"),
            "num_executed_tests": last_int(r"^Current num_executed_tests: (\d+)$"),
            "exit_code": result.return_code,
        }
