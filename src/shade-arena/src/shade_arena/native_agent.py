"""Harbor integration for SHADE-Arena's original LLM and tool loop."""

import os
import shlex

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.utils.env import resolve_env_vars


PROVIDER_KEYS = {
    "anthropic": "ANTHROPIC_HIGH_PRIO_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "together": "TOGETHER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}


class ShadeArenaNativeAgent(BaseAgent):
    @staticmethod
    def name() -> str:
        return "shade-arena-native"

    def version(self) -> str:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        return None

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        model = self.model_name or ""
        provider, separator, name = model.partition("/")
        if not separator or not name or provider not in PROVIDER_KEYS:
            raise ValueError(f"Specify a supported SHADE-Arena provider/model: {model!r}")
        key = PROVIDER_KEYS[provider]
        extra_env = resolve_env_vars(self.extra_env)
        credential = extra_env.get(key) or os.getenv(key)
        if not credential:
            raise ValueError(f"Set {key} for {model}")
        model_env = {key: credential}
        if provider == "openai":
            base_url = extra_env.get("OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL")
            if base_url:
                model_env["OPENAI_BASE_URL"] = base_url
        result = await environment.exec(f"shade native {shlex.quote(model)}", env=model_env)
        if result.return_code:
            (self.logs_dir / "shade-native-error.txt").write_text(
                result.stderr or result.stdout or "", encoding="utf-8"
            )
            raise RuntimeError(f"SHADE-Arena agent exited with code {result.return_code}")
        context.metadata = {"shade_model": model, "tool_loop": "upstream"}
