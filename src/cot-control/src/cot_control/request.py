"""The chat-completion request CoT-Control-QA sends, without any Harbor dependency.

These helpers mirror ``CoT-Control-QA/llm.py::OpenRouterLLM`` (``_build_payload`` and
``_parse_response``) with the settings ``run_cceval.py`` uses. They live apart from the
Harbor agent so that ``python -m cot_control.check_fidelity`` can compare them with the
upstream code itself.
"""

from __future__ import annotations

from typing import Any

DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"

# Upstream defaults (run_cceval.py): DEFAULT_MAX_TOKENS, DEFAULT_REASONING_TOKENS,
# temperature=0.0 in run_evaluation, and the 300 s request timeout in llm.py.
UPSTREAM_MAX_TOKENS = 25000
UPSTREAM_REASONING_TOKENS = 25000
UPSTREAM_TEMPERATURE = 0.0
UPSTREAM_REQUEST_TIMEOUT_SEC = 300.0

# Upstream decides whether to request reasoning from the model name (llm.py).
UPSTREAM_REASONING_MODEL_KEYWORDS = ["r1", "thinking", "qwen3", "magistral", "oss"]

REASONING_MODES = ("auto", "on", "off")


def is_upstream_reasoning_model(model: str) -> bool:
    """Upstream's rule for models that get a ``reasoning`` request parameter."""
    return any(kw in model.lower() for kw in UPSTREAM_REASONING_MODEL_KEYWORDS)


def build_payload(
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = UPSTREAM_MAX_TOKENS,
    temperature: float = UPSTREAM_TEMPERATURE,
    reasoning_tokens: int | None = UPSTREAM_REASONING_TOKENS,
    reasoning: str = "auto",
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Request body. With the defaults it equals upstream ``_build_payload``.

    ``reasoning="auto"`` is upstream's behaviour: the ``reasoning`` parameter is sent
    only for model names upstream recognises as reasoning models. ``"on"`` and ``"off"``
    force it, and ``extra_body`` is merged in last (e.g. an OpenRouter provider pin);
    both are extensions and must be applied to the original side too in a comparison.
    """
    if reasoning not in REASONING_MODES:
        raise ValueError(
            f"reasoning must be one of {REASONING_MODES}, got {reasoning!r}"
        )
    request_reasoning = reasoning == "on" or (
        reasoning == "auto" and is_upstream_reasoning_model(model)
    )

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if request_reasoning and reasoning_tokens:
        payload["reasoning"] = {
            "max_tokens": reasoning_tokens,
            "temperature": temperature,
        }
    if extra_body:
        payload.update(extra_body)
    return {k: v for k, v in payload.items() if v is not None}


def parse_response(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(content, reasoning)`` as upstream ``_parse_response`` extracts them.

    Upstream reads the reasoning trace from ``message.reasoning_content`` and, failing
    that, from ``message.reasoning``. An API that returns the trace anywhere else, or
    returns only a summary of it, cannot be evaluated faithfully.
    """
    choice = data["choices"][0]
    content = choice["message"]["content"]

    reasoning = None
    if "reasoning_content" in choice["message"]:
        reasoning = choice["message"]["reasoning_content"]
    elif "reasoning" in choice["message"]:
        reasoning = choice["message"]["reasoning"]
    return content, reasoning
