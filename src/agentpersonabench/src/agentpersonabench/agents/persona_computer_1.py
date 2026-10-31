"""Harbor agent for AgentPersonaBench os-app tasks.

Stock ``computer-1`` has no identity channel. APB's Docker path does two
things this class puts back: the Jinja identity block replaces the
"You are computer-1" opening, and the full persona profile is appended to the
instruction the way ``harbor_solve.sh --extra-instruction-path`` does.
``instruction.md`` on disk is left unchanged. ``persona.yaml`` is uploaded at
trial time, matching APB's persona mixin.
"""

from __future__ import annotations

import os
from pathlib import Path

from harbor.agents.computer_1.computer_1 import Computer1
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from agentpersonabench.persona_jinja import render_persona_system
from agentpersonabench.persona_prompt import persona_system_prompt

_STOCK_OPENING = (
    "You are computer-1, an autonomous agent that controls a desktop computer to\n"
    "complete tasks. Each turn you observe the current screen via a screenshot and\n"
    "respond with one action.\n\n"
)
_CAPABILITY = (
    "You are using a desktop computer. Each turn you observe the current screen "
    "via a screenshot and respond with one action.\n\n"
)
_CONSTRAINT = (
    "Environment constraint: interact only with the visible application UI "
    "using screenshots, clicks, scrolling, and normal text entry. Do not use "
    "view-source, developer tools, DOM inspection, shell commands, or "
    "filesystem inspection."
)


class PersonaComputer1(Computer1):
    @staticmethod
    def name() -> str:
        return "persona-computer-1"

    def version(self) -> str | None:
        return "1.0.0"

    def __init__(self, *args, max_steps: int | None = None, **kwargs) -> None:
        if max_steps is not None and "max_turns" not in kwargs:
            kwargs["max_turns"] = int(max_steps)
        kwargs.setdefault("max_turns", 40)
        if "temperature" not in kwargs:
            raw = os.environ.get("ADHERENCE_TEMPERATURE")
            kwargs["temperature"] = float(raw) if raw else 0.7
        if not kwargs.get("api_base"):
            base = os.environ.get("LLM_BASE_URL") or os.environ.get("DASHSCOPE_API_BASE")
            if base:
                kwargs["api_base"] = base
        os.environ.setdefault(
            "LLM_REASONING_EFFORT",
            os.environ.get("ADHERENCE_EFFORT") or "medium",
        )
        super().__init__(*args, **kwargs)
        self._identity_prompt = ""

    def _build_provider(self):
        provider = super()._build_provider()
        _install_claude_thinking(provider)
        identity = (self._identity_prompt or "").strip()
        original = getattr(provider, "_prompt_text", None)
        if not identity or original is None:
            return provider

        def _prompt_text(instruction: str) -> str:
            text = original(instruction)
            if text.startswith(_STOCK_OPENING):
                return f"{identity}\n\n{_CAPABILITY}{text[len(_STOCK_OPENING):]}"
            return text

        provider._prompt_text = _prompt_text
        return provider

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        persona = _persona_file(Path(environment.environment_dir).parent)
        self._identity_prompt = render_persona_system(persona)
        await environment.upload_file(persona, "/app/input/persona.yaml")
        if _CONSTRAINT not in instruction:
            profile = persona_system_prompt(persona, role="a user")
            instruction = f"{instruction.rstrip()}\n\n{profile}\n\n{_CONSTRAINT}"
        await super().run(instruction, environment, context)


def _persona_file(task_dir: Path) -> Path:
    for candidate in (task_dir / "persona.yaml", task_dir / "tests" / "persona.yaml"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no persona.yaml under {task_dir}")


def _install_claude_thinking(provider) -> None:
    """APB's computer-1 sends Claude thinking. Harbor 0.18's native provider does not."""
    client = getattr(provider, "_client", None)
    messages = getattr(getattr(client, "beta", None), "messages", None)
    create = getattr(messages, "create", None)
    if create is None or getattr(provider, "_apb_thinking_wrapped", False):
        return
    from agentpersonabench.judge_client import _claude_thinking

    def wrapped(**kwargs):
        model = kwargs.get("model") or getattr(provider, "model_name", "")
        thinking, max_tokens = _claude_thinking(str(model), int(kwargs.get("max_tokens") or 4096))
        kwargs.pop("temperature", None)
        if thinking:
            kwargs.update(thinking)
            kwargs["max_tokens"] = max_tokens
        return create(**kwargs)

    messages.create = wrapped
    provider._apb_thinking_wrapped = True
