"""Claude Code with the same persona wrap as the survey OpenHands agent.

Stock ``claude-code`` only receives ``instruction.md``. That file states the
errand and does not contain the profile. This subclass appends
``persona_system_prompt`` and runs APB's identity template before the CLI
sees the prompt. Web tasks use the role in ``web.json``; survey tasks stay
on the default role.
"""

from __future__ import annotations

import json
from pathlib import Path

from harbor.agents.installed.claude_code import ClaudeCode

from agentpersonabench.persona_jinja import render_persona_instruction
from agentpersonabench.persona_prompt import persona_system_prompt


class PersonaClaudeCode(ClaudeCode):
    @staticmethod
    def name() -> str:
        return "persona-claude-code"

    def version(self) -> str | None:
        return "1.0.0"

    def __init__(self, logs_dir: Path, *args, **kwargs) -> None:
        model_name = kwargs.get("model_name")
        if isinstance(model_name, str) and model_name.startswith("anthropic/"):
            # PP API's Anthropic surface expects the bare model id.
            kwargs["model_name"] = model_name.split("/", 1)[1]
        super().__init__(logs_dir, *args, **kwargs)

    def render_instruction(self, instruction: str) -> str:
        instruction = super().render_instruction(instruction)
        task_dir = self._task_dir()
        task_text = (
            instruction.rstrip()
            + "\n\n"
            + persona_system_prompt(task_dir, role=_role(task_dir))
        )
        return render_persona_instruction(task_dir / "persona.yaml", task_text)

    def _task_dir(self) -> Path:
        config_path = self.logs_dir.parent / "config.json"
        if not config_path.is_file():
            raise FileNotFoundError(f"trial config.json is missing at {config_path}")
        data = json.loads(config_path.read_text(encoding="utf-8"))
        task = data.get("task") if isinstance(data, dict) else None
        raw = None
        if isinstance(task, dict):
            raw = task.get("path") or task.get("source")
        if not raw:
            raise FileNotFoundError(f"{config_path} has no task.path")
        task_dir = Path(str(raw))
        if not (task_dir / "persona.yaml").is_file():
            raise FileNotFoundError(f"no persona.yaml in task dir {task_dir}")
        return task_dir


def _role(task_dir: Path) -> str:
    for candidate in (
        task_dir / "environment" / "web.json",
        task_dir / "solution" / "web.json",
        task_dir / "tests" / "web.json",
    ):
        if not candidate.is_file():
            continue
        data = json.loads(candidate.read_text(encoding="utf-8"))
        role = data.get("role") if isinstance(data, dict) else None
        if isinstance(role, str) and role.strip():
            return role.strip()
        return "a user"
    return "a person"
