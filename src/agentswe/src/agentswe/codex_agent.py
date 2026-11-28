from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from harbor.agents.installed.base import with_prompt_template
from harbor.agents.installed.codex import CodexOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import Field

from .codex_builder.codex_provider_agent import ProviderCodex

GENERATOR = Path(__file__).resolve().parent / "codex_builder" / "gen_codex_config.py"
# The evaluator sidecar runs upstream's builder role broker and writes its per-trial client token here.
BROKER_URL = "http://evaluator:18083/v1"
BROKER_TOKEN_FILE = "/run/agentswe-controller/builder-token"
TOKEN_ENV = "AGENTSWE_BUILDER_API_KEY"


class AgentSWECodexOptions(CodexOptions):
    context_window: int | None = Field(
        default=None,
        description="Prompt window for a builder model outside the AgentSWE-Lite catalog.",
    )


class AgentSWECodex(ProviderCodex):
    """AgentSWE's Builder harness (upstream builders/codex: ProviderCodex) on Harbor 0.27.

    Codex 0.144.1 runs with the AgentSWE-Lite model catalog and provider config from upstream's
    gen_codex_config.py and reaches the model only through the task's builder broker, so it never holds a
    provider key. ``reasoning_effort`` is the Codex catalog level (default ``max``); the builder broker pins the
    provider's own spelling from AGENTSWE_BUILDER_EFFORT. When the session limit cancels the run, upstream's
    ProviderCodex._stop_session_processes stops every Builder process before the freeze.
    """

    options_model = AgentSWECodexOptions

    @staticmethod
    def name() -> str:
        return "agentswe-codex"

    def _generate_config(self, model: str, catalog_path: str, out_dir: Path) -> None:
        command = [
            sys.executable, str(GENERATOR), "--model", model, "--effort", self.options.reasoning_effort or "max",
            "--provider-type", "broker", "--base-url", BROKER_URL, "--catalog-path", catalog_path,
            "--env-key", TOKEN_ENV, "--out-dir", str(out_dir),
        ]
        if self.options.context_window:
            command += ["--context-window", str(self.options.context_window)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise ValueError("Codex config generation failed:\n" + (result.stdout + result.stderr)[-1500:])

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        if not self.model_name:
            raise ValueError("Model name is required")
        model = self.model_name.split("/")[-1]
        codex_home = self._REMOTE_CODEX_HOME
        logs_dir = self.environment_logs_dir.as_posix()
        env = {"CODEX_HOME": codex_home.as_posix()}
        await self._ensure_agent_owned_dir(environment, codex_home)
        await self.exec_as_agent(environment, command=f"mkdir -p {shlex.quote(logs_dir)}", env=env)
        with tempfile.TemporaryDirectory(prefix="agentswe-codex-") as tmp:
            catalog = (codex_home / "models.json").as_posix()
            self._generate_config(model, catalog, Path(tmp))
            await self._upload_agent_owned_file(environment, Path(tmp) / "config.toml", (codex_home / "config.toml").as_posix())
            await self._upload_agent_owned_file(environment, Path(tmp) / "model_catalog.json", catalog)

        cli_flags = self.build_cli_flags()
        output = shlex.quote(f"{logs_dir}/{self._OUTPUT_FILENAME}")
        try:
            await self.exec_as_agent(
                environment,
                command=(
                    "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                    # The evaluator writes the token once its builder broker is up.
                    f"for _ in $(seq 300); do [ -s {BROKER_TOKEN_FILE} ] && break; sleep 1; done; "
                    f'{TOKEN_ENV}="$(cat {BROKER_TOKEN_FILE})"; export {TOKEN_ENV}; '
                    "codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check "
                    f"--model {shlex.quote(model)} --json {cli_flags + ' ' if cli_flags else ''}"
                    f"-- {shlex.quote(instruction)} 2>&1 </dev/null | tee {output}"
                ),
                env=env,
            )
        except asyncio.CancelledError:
            # Harbor's session limit cancels the wait; stop every Builder process before the freeze.
            await self._stop_session_processes(environment)
            raise
        finally:
            try:
                await self.exec_as_agent(
                    environment,
                    command=(
                        f"mkdir -p {shlex.quote(logs_dir)}\n"
                        'if [ -d "$CODEX_HOME/sessions" ]; then\n'
                        f"  rm -rf {shlex.quote(logs_dir + '/sessions')}\n"
                        f'  cp -R "$CODEX_HOME/sessions" {shlex.quote(logs_dir + "/sessions")}\n'
                        "fi"
                    ),
                    env=env,
                    timeout_sec=60,
                )
            except Exception:
                # As upstream: the trajectory copy is evidence only and never fails the session.
                self.logger.warning("copying the Codex sessions failed", exc_info=True)
