"""Jinja identity wrap used by APB's PersonaOpenHandsSDK.

APB survey personas store ``attributes``, not ``dimensions``. The MatrAIx
loader then takes the v0 branch: display name ``persona-<id>`` and that same
sentence as ``system_prompt``. This module reproduces that wrap. The profile
the model actually uses is still ``persona_prompt.persona_system_prompt``,
appended to the task text before this template runs.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATES = Path(__file__).resolve().parent / "agents" / "templates"


def render_persona_system(persona_yaml: Path) -> str:
    """Identity block APB's Docker computer-1 puts on the identity channel."""
    context = _persona_context(persona_yaml)
    return _render("persona_system.md.j2", context)


def render_persona_instruction(persona_yaml: Path, instruction: str) -> str:
    context = _persona_context(persona_yaml)
    context["instruction"] = instruction
    return _render("persona_instruction.md.j2", context)


def _persona_context(persona_yaml: Path) -> dict:
    raw = yaml.safe_load(persona_yaml.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Persona YAML must be a mapping: {persona_yaml}")

    persona_id = _optional_str(raw.get("persona_id"))
    display_name = _optional_str(raw.get("display_name"))
    summary = _optional_str(raw.get("summary"))
    system_prompt = _optional_str(raw.get("system_prompt"))
    dimensions = raw.get("dimensions") if isinstance(raw.get("dimensions"), dict) else {}
    has_domains = any(
        raw.get(key)
        for key in (
            "dimensions",
            "demographics",
            "psychology",
            "communication",
            "preferences",
            "behavior",
        )
    )

    if not display_name and persona_id:
        display_name = f"persona-{persona_id}"
    if not system_prompt and not has_domains:
        parts = []
        if display_name:
            parts.append(f"You are {display_name}.")
        if summary:
            parts.append(summary.strip())
        system_prompt = " ".join(parts).strip() or None

    context = dict(raw)
    context.update(
        {
            "persona_id": persona_id,
            "display_name": display_name,
            "summary": summary,
            "system_prompt": system_prompt,
            "dimensions": dimensions,
            "demographics": raw.get("demographics") or {},
            "psychology": raw.get("psychology") or {},
            "communication": raw.get("communication") or {},
            "preferences": raw.get("preferences") or {},
            "behavior": raw.get("behavior") or {},
            "dimension_profile_narrative": [],
        }
    )
    return context


def _render(template_name: str, context: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template(template_name).render(**context).strip()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
