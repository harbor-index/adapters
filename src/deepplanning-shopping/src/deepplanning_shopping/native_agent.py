"""Harbor lifecycle wrapper for Qwen's pinned native ShoppingFnAgent."""

import json
import os
import re
import shlex
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.agents.options import AgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import Field

from .adapter import CODE_REVISION

OPENAI_VERSION = "3.50.0"
NATIVE_CONFIG_KEYS = {
    "model_name",
    "model_type",
    "base_url",
    "api_key_env",
    "temperature",
    "max_retries",
    "backoff",
    "extra_body",
}


class NativeAgentOptions(AgentOptions):
    model_config_path: str = Field(description="Host-side upstream models_config.json")
    max_llm_calls: int = Field(
        default=400, ge=1, description="Limit per upstream stage"
    )
    execution_timeout_sec: int = Field(default=1800, ge=1)


def load_model_config(path: Path, model_name: str) -> dict:
    """Select an explicit upstream configuration without embedding credentials."""
    models = json.loads(path.read_text())["models"]
    config = models[model_name]
    unknown = config.keys() - NATIVE_CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"Unsupported native model configuration keys: {sorted(unknown)}"
        )
    if config.get("model_type", "openai") != "openai":
        raise ValueError("The pinned ShoppingFnAgent supports OpenAI-compatible APIs")
    if config.get("model_name", model_name) != model_name:
        raise ValueError("Use the exact API model ID as both model name and config key")
    if not config.get("base_url") or not isinstance(config["base_url"], str):
        raise ValueError("An explicit native base_url is required")
    key_name = config.get("api_key_env", "")
    if not isinstance(key_name, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", key_name
    ):
        raise ValueError("api_key_env must name a credential environment variable")
    return dict(config)


class ShoppingFnAgent(BaseAgent):
    """Execute the original agent in its VM, retaining its native messages file."""

    options_model = NativeAgentOptions

    def __init__(self, *args, **kwargs):
        # Upstream consumes exact API model IDs and its own native config.
        # Avoid provider-alias rewriting and LiteLLM's remote cost-map lookup.
        kwargs["skip_model_parsing"] = True
        super().__init__(*args, **kwargs)
        if not self.model_name:
            raise ValueError("An exact model_name is required")
        self.model_config = load_model_config(
            Path(self.options.model_config_path), self.model_name
        )

    @staticmethod
    def name() -> str:
        return "deepplanning-shopping-native"

    def version(self) -> str:
        return CODE_REVISION

    async def setup(self, environment: BaseEnvironment) -> None:
        result = await environment.exec(
            "/usr/local/bin/python /workspace/run_native.py --check", timeout_sec=30
        )
        if result.return_code:
            raise RuntimeError(
                "Native task files missing or source checksum mismatch; regenerate tasks"
            )
        result = await environment.exec(
            "/usr/local/bin/python -m pip install --disable-pip-version-check "
            f"openai=={OPENAI_VERSION} numpy==2.2.6 rank-bm25==0.2.2",
            timeout_sec=180,
        )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "native-setup.txt").write_text(
            (result.stdout or "") + (result.stderr or "")
        )
        if result.return_code:
            raise RuntimeError(
                "Native dependencies failed to install; see native-setup.txt"
            )

    def execution_env(self) -> dict[str, str]:
        key_name = self.model_config["api_key_env"]
        value = self._extra_env.get(key_name, os.environ.get(key_name))
        if not value:
            raise ValueError(
                f"Missing native credential environment variable: {key_name}"
            )
        return {key_name: value, "PYTHONUNBUFFERED": "1"}

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        # The native agent reads the original system/query from prompt.json. The
        # terminal transport instructions belong only to terminal agents.
        env = self.execution_env()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        config = {"models": {self.model_name: self.model_config}}
        config_path = self.logs_dir / "native-config.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        await environment.upload_file(
            config_path, "/workspace/native_agent/models_config.json"
        )
        command = (
            "/usr/local/bin/python /workspace/run_native.py --model "
            + shlex.quote(self.model_name)
            + f" --max-llm-calls {self.options.max_llm_calls}"
            + " > /logs/agent/native-run.txt 2>&1"
        )
        try:
            result = await environment.exec(
                command,
                cwd="/workspace",
                env=env,
                timeout_sec=self.options.execution_timeout_sec,
            )
        finally:
            for source, filename in (
                ("/logs/agent/native-run.txt", "native-run.txt"),
                ("/workspace/state/messages.json", "native-messages.json"),
                ("/workspace/state/native-summary.json", "native-summary.json"),
            ):
                try:
                    await environment.download_file(source, self.logs_dir / filename)
                except Exception as error:  # noqa: BLE001 - retain partial logs without masking execution errors
                    self.logger.warning(
                        "Could not collect %s: %s", filename, type(error).__name__
                    )
        context.metadata = {
            "native_source_revision": CODE_REVISION,
            "model_config_name": self.model_name,
            "max_llm_calls_per_stage": self.options.max_llm_calls,
            "openai_version": OPENAI_VERSION,
        }
        if result.return_code:
            raise RuntimeError(
                f"Native agent exited {result.return_code}; see native-run.txt"
            )
