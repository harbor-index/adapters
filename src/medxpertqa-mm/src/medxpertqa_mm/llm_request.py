"""Request construction for the MedXpertQA LLM agent.

Everything here mirrors the original harness (TsinghuaC3I/MedXpertQA, ``eval/``) under its
default setting, ``--method zero_shot --prompting-type cot --temperature 0``:

* ``config/prompt_templates.py``  system role, first-turn prompt, second-turn trigger
* ``main.py::zero_shot_cot``      two-turn conversation, images attached to the first turn
* ``model/api_agent.py``          request parameters, image encoding, failure strings

It has no Harbor imports so that ``check_fidelity.py`` can compare it with the upstream
code directly.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any

# config/prompt_templates.py (dataset == "medxpertqa")
SYSTEM_ROLE = "You are a helpful medical assistant."
COT_TRIGGER = "Therefore, among {start} through {end}, the answer is"
DEEPSEEK_REASONER_PREFIX = "Put your final answer within \\boxed{{}}. "

# model/api_agent.py
UPSTREAM_TEMPERATURE = 0.0  # scripts/run.sh default
UPSTREAM_SEED = 0
UPSTREAM_MAX_TOKENS = 2048
UPSTREAM_MAX_TOKENS_REASONING = 8192
UPSTREAM_ATTEMPTS = 10
NO_ANSWER = "No answer provided."
NO_ANSWER_BAD_RESPONSE = "No answer provided: bad_response."
NO_ANSWER_CONTENT_FILTER = "No answer provided: content_filter."

IMAGE_DIR_IN_ENV = "/app/images"
ANSWER_PATH_IN_ENV = "/app/answer.txt"

_INSTRUCTION_RE = re.compile(
    r"\A" + re.escape(SYSTEM_ROLE) + r"\n\n"
    r"(?P<prompt>Q: .*?\nA: Let's think step by step\.)\n\n"
    r"## Images\n\n.*?\n\n(?P<images>(?:- `[^`\n]+`\n?)+)\n"
    r"## Answer format\n\n"
    r"Among (?P<start>[A-Z]) through (?P<end>[A-Z]), ",
    re.DOTALL,
)
_IMAGE_LINE_RE = re.compile(
    r"^- `" + re.escape(IMAGE_DIR_IN_ENV) + r"/([^`/\n]+)`$", re.MULTILINE
)


@dataclass(frozen=True)
class ParsedTask:
    prompt: str  # "Q: {question}\nA: Let's think step by step."
    image_names: list[str]  # in upstream order
    start: str  # first option letter
    end: str  # last option letter


def parse_instruction(instruction: str) -> ParsedTask:
    """Recover the upstream prompt and image list from a generated ``instruction.md``."""
    match = _INSTRUCTION_RE.match(instruction)
    if match is None:
        raise ValueError("Instruction is not a MedXpertQA-MM task instruction")
    image_names = _IMAGE_LINE_RE.findall(match.group("images"))
    if not image_names:
        raise ValueError("No image paths found in the instruction")
    return ParsedTask(
        prompt=match.group("prompt"),
        image_names=image_names,
        start=match.group("start"),
        end=match.group("end"),
    )


def uses_reasoning_branch(model: str) -> bool:
    """``api_agent.py::get_response``: o1/o3/deepseek-reasoner take the other code path."""
    return ("o3" in model) or ("o1" in model) or ("deepseek-reasoner" in model)


def max_tokens_for(model: str) -> int:
    """``api_agent.py::__init__``."""
    lowered = model.lower()
    if (
        "o3" in model
        or "o1" in model
        or "qwq" in lowered
        or "qvq" in lowered
        or "reasoner" in lowered
    ):
        return UPSTREAM_MAX_TOKENS_REASONING
    return UPSTREAM_MAX_TOKENS


def cot_trigger(model: str, start: str, end: str) -> str:
    """Second-turn user message (``zero_shot_cot`` stage 2)."""
    trigger = COT_TRIGGER
    if model == "deepseek-reasoner":
        trigger = DEEPSEEK_REASONER_PREFIX + trigger
    return trigger.format(start=start, end=end)


def image_content(image_bytes: bytes) -> dict[str, Any]:
    """``api_agent.py::image_content`` for a local file (always labelled image/jpeg)."""
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
    }


def first_turn_messages(prompt: str, images: list[bytes]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend(image_content(data) for data in images)
    return [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": content},
    ]


def second_turn_messages(
    messages: list[dict[str, Any]], first_response: str, trigger: str
) -> list[dict[str, Any]]:
    return [
        *messages,
        {"role": "assistant", "content": first_response},
        {"role": "user", "content": trigger},
    ]


def build_payload(
    model: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = UPSTREAM_TEMPERATURE,
    drop_params: tuple[str, ...] = (),
    token_param: str | None = None,
) -> dict[str, Any]:
    """Chat-completion request body, as ``api_agent.py::get_response`` sends it.

    ``drop_params`` and ``token_param`` are escape hatches for endpoints that reject one of
    upstream's parameters (for example ``logprobs``) or want ``max_completion_tokens``.
    They are off by default; if used, the same change must be made on the original side.
    """
    if uses_reasoning_branch(model):
        payload: dict[str, Any] = {
            "messages": [m for m in messages if m["role"] != "system"],
            "model": model,
            "max_completion_tokens": max_tokens_for(model),
            "seed": UPSTREAM_SEED,
        }
    else:
        payload = {
            "messages": messages,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens_for(model),
            "logprobs": True,
            "seed": UPSTREAM_SEED,
        }
    if token_param:
        for name in ("max_tokens", "max_completion_tokens"):
            if name in payload and name != token_param:
                payload[token_param] = payload.pop(name)
    for name in drop_params:
        payload.pop(name, None)
    return payload
