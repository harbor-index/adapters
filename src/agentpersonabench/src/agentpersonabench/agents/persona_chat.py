"""Harbor agent for AgentPersonaBench chat tasks.

The persona is the user. It talks to the task's prompt bot for 12 turns, the
same split ``chat_harness.run_chat_adherence`` uses. Persona temperature is
0.7 and the bot temperature is 0.6. ``instruction.md`` is the task-instruction
section of the user-sim prompt. ``persona.yaml`` is read on the host and is
not in the image.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from agentpersonabench.judge_client import chat
from agentpersonabench.persona_prompt import persona_system_prompt

_GUIDELINES = Path(__file__).resolve().parent / "templates" / "sim_guidelines.md"
_MAX_TURNS = 12
_PERSONA_TEMPERATURE = 0.7
_BOT_TEMPERATURE = 0.6
_BOT_MAX_TOKENS = 1024
_PERSONA_MAX_TOKENS = 4096
_START = (
    "The conversation is starting. The application chatbot has not replied yet. "
    "Send your opening message with send_message."
)
_CONTINUE = (
    "The conversation isn't over yet — the assistant follows up again. Stay in "
    "character and keep engaging naturally: react to what it just said, and hold "
    "to your own preferences and values as a real person would. Send one more "
    "message with the send_message tool (do not end the conversation)."
)
_END_REASONS = ("give_up", "out_of_scope", "satisfied", "transferred")
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send one natural-language message to the application chatbot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The user message shown to the chatbot.",
                    }
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_conversation",
            "description": "End the chat when the goal is met or you would stop in real life.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "enum": list(_END_REASONS)},
                    "note": {
                        "type": "string",
                        "description": "Private one-line rationale (not sent to the chatbot).",
                    },
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    },
]
_ANTHROPIC_TOOLS = [
    {
        "name": tool["function"]["name"],
        "description": tool["function"].get("description", ""),
        "input_schema": tool["function"]["parameters"],
    }
    for tool in _TOOLS
]
_LABELS = {
    "finance_openbb": "FinAI / OpenBB",
    "meal_planning_nutrition": "Meal Planning Nutrition",
    "acme_support_api": "ACME Support API",
    "acme_support_mcp": "ACME Support MCP",
}
_TOOL_SEND = re.compile(r"^(?:\s*Tool\s+send_message\s*:\s*)+", re.IGNORECASE)
_TOOL_END = re.compile(r"^\s*Tool\s+end_conversation\s*:\s*", re.IGNORECASE)
_XML_PARAMETER = re.compile(
    r"<parameter\s+name=[\"'][^\"']+[\"'][^>]*>.*?</parameter>",
    re.IGNORECASE | re.DOTALL,
)
_INVOKE_BLOCK = re.compile(r"<invoke\b[^>]*>.*?</invoke>", re.IGNORECASE | re.DOTALL)


class PersonaChat(BaseAgent):
    def __init__(self, *args, max_turns: int | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._max_turns = int(max_turns) if max_turns is not None else _MAX_TURNS

    @staticmethod
    def name() -> str:
        return "persona-chat"

    def version(self) -> str | None:
        return "1.0.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        return None

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if self.model_name is None:
            raise RuntimeError("persona-chat requires a model name")
        os.environ.setdefault(
            "LLM_REASONING_EFFORT",
            os.environ.get("ADHERENCE_EFFORT") or "medium",
        )
        task_dir = Path(environment.environment_dir).parent
        persona = _persona_file(task_dir)
        bot_path = task_dir / "environment" / "input" / "bot.md"
        if not bot_path.is_file():
            raise FileNotFoundError(f"no bot.md under {task_dir}")
        context_path = task_dir / "environment" / "input" / "context.md"
        context_md = context_path.read_text(encoding="utf-8") if context_path.is_file() else ""
        system = _system_prompt(persona, instruction, context_md)
        bot_prompt = bot_path.read_text(encoding="utf-8")
        label = _chatbot_label(_application_id(task_dir / "task.toml"))
        persona_model = self.model_name
        bot_model = _bot_model(persona_model)
        user_turns, transcript, usage = _converse(
            system,
            bot_prompt,
            label,
            persona_model,
            bot_model,
            self._max_turns,
        )
        payload = {
            "persona": _persona_id(persona),
            "turns": user_turns,
        }
        turns_path = self.logs_dir / "user_turns.json"
        transcript_path = self.logs_dir / "transcript.json"
        turns_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        transcript_path.write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        await environment.exec(command="mkdir -p /app/output")
        await environment.upload_file(turns_path, "/app/output/user_turns.json")
        await environment.upload_file(transcript_path, "/app/output/transcript.json")
        context.n_input_tokens = usage["prompt_tokens"]
        context.n_output_tokens = usage["completion_tokens"]
        context.metadata = {
            "agent": self.name(),
            "turns": len(user_turns),
            "persona_model": persona_model,
            "bot_model": bot_model,
        }


def _system_prompt(persona: Path, instruction: str, context_md: str) -> str:
    blocks = [
        persona_system_prompt(persona, role="a user"),
        _date_block(),
        _GUIDELINES.read_text(encoding="utf-8").strip(),
        _section("Task instruction", instruction),
        _section("Task context", context_md),
    ]
    return "\n\n".join(block for block in blocks if block.strip())


def _date_block() -> str:
    moment = datetime.now().astimezone()
    return "Today is {weekday}, {month} {day}, {year}, {time}.".format(
        weekday=moment.strftime("%A"),
        month=moment.strftime("%B"),
        day=moment.day,
        year=moment.year,
        time=moment.strftime("%H:%M %Z").strip(),
    )


def _section(title: str, body: str) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    return f"## {title}\n{text}"


def _converse(
    system: str,
    bot_prompt: str,
    label: str,
    persona_model: str,
    bot_model: str,
    max_turns: int,
) -> tuple[list[str], list[dict], dict]:
    messages: list[dict] = [{"role": "system", "content": system}]
    bot_history: list[dict] = []
    user_turns: list[str] = []
    transcript: list[dict] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    action = _persona_action(messages, _START, persona_model, usage)
    for index in range(1, max_turns + 1):
        message = (action.get("message") or "").strip()
        if not message:
            action = _persona_action(messages, _CONTINUE, persona_model, usage)
            message = (action.get("message") or "").strip()
        if not message:
            break
        user_turns.append(message)
        transcript.append({"role": "user", "content": message})
        reply = _bot_reply(bot_prompt, bot_history, message, bot_model, usage)
        transcript.append({"role": "assistant", "content": reply})
        action = _persona_action(
            messages,
            _observation(label, reply),
            persona_model,
            usage,
        )
    if not user_turns:
        raise RuntimeError("persona-chat produced no user turns")
    return user_turns, transcript, usage


def _persona_action(messages: list[dict], observation: str, model: str, usage: dict) -> dict:
    messages.append({"role": "user", "content": observation})
    calls = _tool_calls(messages, model, usage)
    action = _parse_calls(calls)
    messages.append({"role": "assistant", "content": _format_action(action)})
    return action


def _bot_reply(system: str, history: list[dict], message: str, model: str, usage: dict) -> str:
    history.append({"role": "user", "content": message})
    # DeepSeek spends the completion budget on reasoning_content. APB's 1024
    # cap can come back with an empty visible reply.
    max_tokens = 8192 if "deepseek" in model.lower() else _BOT_MAX_TOKENS
    reply = chat(
        [{"role": "system", "content": system}, *history],
        model=model,
        max_tokens=max_tokens,
        temperature=_BOT_TEMPERATURE,
    )
    _add_usage(usage)
    history.append({"role": "assistant", "content": reply})
    return reply


def _tool_calls(messages: list[dict], model: str, usage: dict) -> list[tuple[str, dict]]:
    name = model.strip()
    lowered = name.lower()
    if lowered.startswith("anthropic/") or "claude" in lowered:
        api_model = name.split("/", 1)[1] if lowered.startswith("anthropic/") else name
        calls = _anthropic_tools(messages, api_model)
    else:
        calls = _openai_tools(messages, name)
    _add_usage(usage)
    return calls


def _add_usage(usage: dict) -> None:
    from agentpersonabench.judge_client import get_call_log

    log = get_call_log()
    if not log:
        return
    last = log[-1]
    usage["prompt_tokens"] += int(last.get("prompt_tokens") or 0)
    usage["completion_tokens"] += int(last.get("completion_tokens") or 0)


def _anthropic_tools(messages: list[dict], model: str) -> list[tuple[str, dict]]:
    import time
    import urllib.error
    import urllib.request

    from agentpersonabench.judge_client import _claude_thinking, _record, _split_system

    system, convo = _split_system(messages)
    key = (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is unset")
    thinking, max_tokens = _claude_thinking(model, _PERSONA_MAX_TOKENS)
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": item["role"], "content": item.get("content", "")} for item in convo],
        "tools": _ANTHROPIC_TOOLS,
        **thinking,
    }
    if _temperature_allowed(model):
        body["temperature"] = _PERSONA_TEMPERATURE
    if system:
        body["system"] = system
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"persona tool HTTP {exc.code}: {detail}") from exc
    usage = payload.get("usage") or {}
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    _record(model, prompt, completion, prompt + completion, started)
    calls: list[tuple[str, dict]] = []
    texts: list[str] = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            raw = block.get("input") or {}
            calls.append((str(block.get("name") or ""), raw if isinstance(raw, dict) else {}))
        elif block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
    if not calls and texts:
        calls.append(("send_message", {"message": "\n".join(texts)}))
    return calls


def _openai_tools(messages: list[dict], model: str) -> list[tuple[str, dict]]:
    from agentpersonabench.judge_client import _openai_route, _post

    api_model, url, key = _openai_route(model)
    body: dict = {
        "model": api_model,
        "messages": messages,
        "tools": _TOOLS,
        "tool_choice": "auto",
    }
    if "deepseek" in api_model.lower():
        body["max_tokens"] = 8192
    if _temperature_allowed(api_model):
        body["temperature"] = _PERSONA_TEMPERATURE
    effort = (os.environ.get("LLM_REASONING_EFFORT") or "").strip().lower()
    if effort and api_model.startswith(("google/", "gemini")):
        body["reasoning_effort"] = effort
    started = time.time()
    payload = _post(
        url,
        body,
        {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        180,
    )
    message = ((payload.get("choices") or [{}])[0].get("message") or {})
    calls: list[tuple[str, dict]] = []
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        raw = function.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append((str(function.get("name") or ""), args))
    if not calls and message.get("content"):
        calls.append(("send_message", {"message": str(message.get("content") or "")}))
    from agentpersonabench.judge_client import _record

    usage = payload.get("usage") or {}
    _record(
        api_model,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        int(usage.get("total_tokens") or 0),
        started,
    )
    return calls


def _parse_calls(calls: list[tuple[str, dict]]) -> dict:
    action = {"message": "", "end_reason": "", "note": ""}
    for name, args in calls:
        if name == "send_message":
            message = _normalize(str(args.get("message") or ""))
            if message:
                action["message"] = message
        elif name == "end_conversation":
            reason = str(args.get("reason") or "give_up").strip()
            if reason not in _END_REASONS:
                reason = "give_up"
            action["end_reason"] = reason
            action["note"] = str(args.get("note") or "").strip()
    if "###STOP###" in action["message"] and not action["end_reason"]:
        action["end_reason"] = "satisfied"
    return action


def _format_action(action: dict) -> str:
    parts: list[str] = []
    if action.get("message"):
        parts.append(action["message"])
    if action.get("end_reason"):
        note = (action.get("note") or "").strip()
        if note:
            parts.append(f"[Ended: {action['end_reason']} — {note}]")
        else:
            parts.append(f"[Ended: {action['end_reason']}]")
    return "\n".join(parts) if parts else "(no action)"


def _normalize(message: str) -> str:
    text = message.strip()
    if not text:
        return ""
    while True:
        stripped = _TOOL_SEND.sub("", text, count=1).strip()
        if stripped == text:
            break
        text = stripped
    text = _TOOL_END.sub("", text).strip()
    text = _XML_PARAMETER.sub("", text).strip()
    text = _INVOKE_BLOCK.sub("", text).strip()
    return text


def _observation(label: str, assistant: str) -> str:
    return (
        f'{label} Answer:\n"""{assistant}"""\n'
        "\nDecide your next move using the available tools."
    )


def _temperature_allowed(model: str) -> bool:
    lowered = (model or "").strip().lower()
    bare = lowered.rsplit("/", 1)[-1]
    if bare.startswith("gpt-5"):
        return False
    # Claude Haiku 5 rejects any temperature field. APB's llm_client already
    # omits it for every claude model.
    if "claude" in lowered:
        return False
    match = re.search(r"opus-4-(\d+)", lowered)
    if match is not None and int(match.group(1)) >= 7:
        return False
    return True


def _bot_model(arm: str) -> str:
    bot = (os.environ.get("APB_CHAT_BOT_MODEL") or "arm").strip()
    if bot.lower() == "arm":
        return arm
    return bot


def _chatbot_label(application_id: str) -> str:
    value = (application_id or "").strip()
    if not value:
        return "Chatbot"
    return _LABELS.get(value, value.replace("_", " ").strip().title() or "Chatbot")


def _application_id(task_toml: Path) -> str:
    if not task_toml.is_file():
        return "assistant"
    import tomllib

    data = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    return str(meta.get("application_id") or "assistant")


def _persona_file(task_dir: Path) -> Path:
    for candidate in (task_dir / "persona.yaml", task_dir / "tests" / "persona.yaml"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no persona.yaml under {task_dir}")


def _persona_id(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines()[:20]:
        if line.startswith("persona_id:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'") or "persona"
    return "persona"
