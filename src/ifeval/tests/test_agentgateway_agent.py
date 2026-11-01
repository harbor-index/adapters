from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Protocol
from unittest.mock import AsyncMock

import httpx
import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from harbor.models.trial.config import AgentConfig

from ifeval import agentgateway_agent as agentgateway_module
from ifeval.agentgateway_agent import AgentGatewayAgent


def completed_response(text: str = "answer") -> dict[str, Any]:
    return {
        "id": "resp_123",
        "status": "completed",
        "model": "served-model",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }


TransportHandler = Callable[[httpx.Request], httpx.Response]


class TransportInstaller(Protocol):
    def __call__(
        self,
        handler: TransportHandler | None = None,
    ) -> list[httpx.Request]: ...


@pytest.fixture
def patch_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> TransportInstaller:
    async_client = httpx.AsyncClient

    def install(handler: TransportHandler | None = None) -> list[httpx.Request]:
        requests: list[httpx.Request] = []
        supplied_handler = handler or (
            lambda request: httpx.Response(
                200,
                json=completed_response("model output"),
            )
        )

        def recording_handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return supplied_handler(request)

        transport = httpx.MockTransport(recording_handler)

        def make_client(**kwargs: Any) -> httpx.AsyncClient:
            return async_client(transport=transport, **kwargs)

        monkeypatch.setattr(agentgateway_module.httpx, "AsyncClient", make_client)
        return requests

    return install


async def run_agent(tmp_path: Path, **kwargs: Any):
    environment = AsyncMock()
    context = AgentContext()
    model_name = kwargs.pop("model_name", "gateway-model")
    agent = AgentGatewayAgent(
        logs_dir=tmp_path,
        model_name=model_name,
        **kwargs,
    )
    await agent.run("Follow the instruction.", environment, context)
    return environment, context


def test_name_version_and_factory(tmp_path: Path):
    agent = AgentFactory.create_agent_from_config(
        AgentConfig(
            import_path="ifeval.agentgateway_agent:AgentGatewayAgent",
            model_name="gateway-model",
            kwargs={"api_base": "http://gateway.invalid/v1"},
        ),
        logs_dir=tmp_path,
    )

    assert isinstance(agent, AgentGatewayAgent)
    assert agent.name() == "agentgateway"
    assert agent.version() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api_base",
    ["http://gateway.invalid/v1", "http://gateway.invalid/v1/"],
)
async def test_run_posts_one_responses_request_and_uploads_output(
    tmp_path: Path,
    patch_transport: TransportInstaller,
    api_base: str,
):
    requests = patch_transport()
    environment, _ = await run_agent(
        tmp_path,
        api_base=api_base,
        extra_env={"AGENTGATEWAY_API_KEY": "sentinel"},
        reasoning_effort="high",
        max_tokens=128,
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url == httpx.URL("http://gateway.invalid/v1/responses")
    assert request.headers["Authorization"] == "Bearer sentinel"
    assert json.loads(request.content) == {
        "model": "gateway-model",
        "input": "Follow the instruction.",
        "store": False,
        "reasoning": {"effort": "high"},
        "max_output_tokens": 128,
    }
    assert request.extensions["timeout"] == {
        "connect": 10.0,
        "read": None,
        "write": 10.0,
        "pool": 10.0,
    }

    response_path = tmp_path / "response.txt"
    assert response_path.read_text(encoding="utf-8") == "model output"
    environment.upload_file.assert_awaited_once_with(
        source_path=response_path,
        target_path="/workspace/response.txt",
    )


@pytest.mark.asyncio
async def test_agent_env_overrides_process_env(
    tmp_path: Path,
    patch_transport: TransportInstaller,
    monkeypatch: pytest.MonkeyPatch,
):
    requests = patch_transport()
    monkeypatch.setenv("OPENAI_BASE_URL", "http://process.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "process-key")

    await run_agent(
        tmp_path,
        extra_env={
            "AGENTGATEWAY_BASE_URL": "http://agent.invalid/v1",
            "AGENTGATEWAY_API_KEY": "agent-key",
        },
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.url == httpx.URL("http://agent.invalid/v1/responses")
    assert request.headers["Authorization"] == "Bearer agent-key"


@pytest.mark.asyncio
async def test_completed_response_populates_usage_and_writes_redacted_metadata(
    tmp_path: Path,
    patch_transport: TransportInstaller,
):
    response_body = completed_response()
    response_body["output"][0]["content"].append(
        {"type": "output_text", "text": " two"}
    )
    response_body["usage"] = {
        "input_tokens": 11,
        "output_tokens": 7,
        "input_tokens_details": {"cached_tokens": 3},
    }
    patch_transport(lambda request: httpx.Response(200, json=response_body))

    _, context = await run_agent(
        tmp_path,
        api_base="http://gateway.invalid/v1",
        extra_env={"AGENTGATEWAY_API_KEY": "sentinel-api-key"},
        reasoning_effort="medium",
        max_tokens=64,
    )

    assert context.n_input_tokens == 11
    assert context.n_cache_tokens == 3
    assert context.n_output_tokens == 7
    assert (tmp_path / "response.txt").read_text(encoding="utf-8") == "answer two"

    metadata_text = (tmp_path / "agentgateway.json").read_text(encoding="utf-8")
    assert "sentinel-api-key" not in metadata_text
    assert "Follow the instruction." not in metadata_text
    metadata = json.loads(metadata_text)
    assert metadata == {
        "model_name": "gateway-model",
        "response_model_name": "served-model",
        "response_id": "resp_123",
        "api_base": "http://gateway.invalid/v1",
        "endpoint": "http://gateway.invalid/v1/responses",
        "request": {
            "model": "gateway-model",
            "input": "<redacted>",
            "store": False,
            "reasoning": {"effort": "medium"},
            "max_output_tokens": 64,
        },
        "response_path": "/workspace/response.txt",
        "status": "completed",
        "incomplete_reason": None,
        "wire_api": "responses",
        "stop_reason": None,
        "usage": response_body["usage"],
        "input_token_accounting": "provider-reported (cache inclusion depends on gateway route)",
        "latency_sec": metadata["latency_sec"],
    }
    assert metadata["latency_sec"] >= 0


def test_redact_url_removes_credentials_query_and_fragment():
    assert (
        AgentGatewayAgent._redact_url(
            "https://user:pass@gateway.invalid/v1?token=secret#fragment"
        )
        == "https://gateway.invalid/v1"
    )


@pytest.mark.asyncio
async def test_null_usage_is_treated_as_absent(
    tmp_path: Path,
    patch_transport: TransportInstaller,
):
    response_body = completed_response()
    response_body["usage"] = None
    patch_transport(lambda request: httpx.Response(200, json=response_body))

    environment, context = await run_agent(
        tmp_path,
        api_base="http://gateway.invalid/v1",
    )

    response_path = tmp_path / "response.txt"
    assert response_path.read_text(encoding="utf-8") == "answer"
    environment.upload_file.assert_awaited_once_with(
        source_path=response_path,
        target_path="/workspace/response.txt",
    )
    assert context.n_input_tokens is None
    assert context.n_cache_tokens is None
    assert context.n_output_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["max_output_tokens", "max_tokens"])
async def test_incomplete_output_limit_response_uploads_partial_output(
    tmp_path: Path,
    patch_transport: TransportInstaller,
    reason: str,
):
    response_body = completed_response("partial")
    response_body["status"] = "incomplete"
    response_body["incomplete_details"] = {"reason": reason}
    patch_transport(lambda request: httpx.Response(200, json=response_body))

    environment, _ = await run_agent(
        tmp_path,
        api_base="http://gateway.invalid/v1",
    )

    response_path = tmp_path / "response.txt"
    assert response_path.read_text(encoding="utf-8") == "partial"
    environment.upload_file.assert_awaited_once_with(
        source_path=response_path,
        target_path="/workspace/response.txt",
    )
    metadata = json.loads((tmp_path / "agentgateway.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "incomplete"
    assert metadata["incomplete_reason"] == "max_output_tokens"
    assert metadata["stop_reason"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "details"),
    [
        ("failed", None),
        ("cancelled", None),
        ("queued", None),
        ("in_progress", None),
        ("incomplete", {"reason": "content_filter"}),
    ],
)
async def test_rejects_non_terminal_and_unsuitable_response_states(
    tmp_path: Path,
    patch_transport: TransportInstaller,
    status: str,
    details: dict[str, str] | None,
):
    response_body = completed_response()
    response_body["status"] = status
    if details is not None:
        response_body["incomplete_details"] = details
    patch_transport(lambda request: httpx.Response(200, json=response_body))

    with pytest.raises(RuntimeError, match=f"status {status}"):
        await run_agent(tmp_path, api_base="http://gateway.invalid/v1")


@pytest.mark.asyncio
async def test_http_failure_does_not_expose_response_body(
    tmp_path: Path,
    patch_transport: TransportInstaller,
):
    secret = "upstream secret should not leak"
    patch_transport(lambda request: httpx.Response(502, text=secret))

    with pytest.raises(RuntimeError, match="HTTP 502") as exc_info:
        await run_agent(tmp_path, api_base="http://gateway.invalid/v1")

    assert secret not in str(exc_info.value)


@pytest.mark.asyncio
async def test_request_failure_exposes_only_exception_type(
    tmp_path: Path,
    patch_transport: TransportInstaller,
):
    secret = "https://user:pass@gateway.invalid/v1?token=secret"

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"could not connect to {secret}", request=request)

    patch_transport(fail)

    with pytest.raises(RuntimeError, match="request failed: ConnectError") as exc_info:
        await run_agent(tmp_path, api_base="http://gateway.invalid/v1")

    assert secret not in str(exc_info.value)


@pytest.mark.asyncio
async def test_rejects_non_json_response(
    tmp_path: Path,
    patch_transport: TransportInstaller,
):
    patch_transport(lambda request: httpx.Response(200, text="not json"))

    with pytest.raises(RuntimeError, match="non-JSON"):
        await run_agent(tmp_path, api_base="http://gateway.invalid/v1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage", "invalid_key"),
    [
        ([], "usage"),
        ({"input_tokens_details": []}, "input_tokens_details"),
        ({"input_tokens": True}, "input_tokens"),
        ({"input_tokens": -1}, "input_tokens"),
        ({"output_tokens": "7"}, "output_tokens"),
        (
            {"input_tokens_details": {"cached_tokens": False}},
            "cached_tokens",
        ),
    ],
)
async def test_rejects_invalid_usage_values(
    tmp_path: Path,
    patch_transport: TransportInstaller,
    usage: Any,
    invalid_key: str,
):
    response_body = completed_response()
    response_body["usage"] = usage
    patch_transport(lambda request: httpx.Response(200, json=response_body))

    with pytest.raises(RuntimeError, match=invalid_key):
        await run_agent(tmp_path, api_base="http://gateway.invalid/v1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_body", "error"),
    [
        ([], "JSON object"),
        ({"status": "completed", "output": {}}, "output must be a list"),
        ({"status": "completed", "output": ["message"]}, "output item"),
        (
            {
                "status": "completed",
                "output": [{"type": "message", "content": {}}],
            },
            "message content must be a list",
        ),
        (
            {
                "status": "completed",
                "output": [{"type": "message", "content": ["text"]}],
            },
            "message content item",
        ),
        (
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": 1}],
                    }
                ],
            },
            "output_text text",
        ),
    ],
)
async def test_rejects_malformed_response_shapes(
    tmp_path: Path,
    patch_transport: TransportInstaller,
    response_body: Any,
    error: str,
):
    patch_transport(lambda request: httpx.Response(200, json=response_body))

    with pytest.raises(RuntimeError, match=error):
        await run_agent(tmp_path, api_base="http://gateway.invalid/v1")


def messages_response(text: str = "answer") -> dict[str, Any]:
    return {
        "id": "msg_123",
        "type": "message",
        "role": "assistant",
        "model": "served-model",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
    }


@pytest.mark.parametrize("effort", [None, "high"])
async def test_messages_request_preserves_instruction_and_redacts_metadata(
    tmp_path: Path,
    patch_transport: TransportInstaller,
    effort: str | None,
):
    body = messages_response(" answer\n")
    body["content"].insert(0, {"type": "thinking", "thinking": "private thought"})
    body["content"].insert(1, {"type": "redacted_thinking", "data": "private data"})
    body["content"].append({"type": "text", "text": " two "})
    body["usage"] = {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 5,
        "cache_creation": {"ephemeral_5m_input_tokens": 5},
        "upstream_debug": "private data",
    }
    requests = patch_transport(lambda request: httpx.Response(200, json=body))
    environment = AsyncMock()
    context = AgentContext()
    agent = AgentGatewayAgent(
        logs_dir=tmp_path,
        model_name="gateway-model",
        api_base="http://gateway.invalid/v1/",
        wire_api="messages",
        max_tokens=128,
        reasoning_effort=effort,
        extra_env={"AGENTGATEWAY_API_KEY": "sentinel-key"},
    )
    instruction = "  Follow this\ninstruction exactly.\n"
    await agent.run(instruction, environment, context)

    assert len(requests) == 1
    request = requests[0]
    assert request.url == httpx.URL("http://gateway.invalid/v1/messages")
    assert request.headers["Authorization"] == "Bearer sentinel-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    expected: dict[str, Any] = {
        "model": "gateway-model",
        "messages": [{"role": "user", "content": instruction}],
        "max_tokens": 128,
    }
    if effort is not None:
        expected["thinking"] = {"type": "adaptive"}
        expected["output_config"] = {"effort": effort}
    assert json.loads(request.content) == expected
    assert (tmp_path / "response.txt").read_text() == " answer\n two "
    environment.upload_file.assert_awaited_once_with(
        source_path=tmp_path / "response.txt", target_path="/workspace/response.txt"
    )
    assert context.n_input_tokens == 19
    assert context.n_cache_tokens == 3
    assert context.n_output_tokens == 7
    assert context.cost_usd is None
    metadata = json.loads((tmp_path / "agentgateway.json").read_text())
    assert metadata["wire_api"] == "messages"
    assert metadata["stop_reason"] == "end_turn"
    assert metadata["request"] == {**expected, "messages": "<redacted>"}
    assert metadata["usage"] == {
        key: value for key, value in body["usage"].items() if key != "upstream_debug"
    }
    assert metadata["input_token_accounting"] == (
        "input_tokens + cache_read_input_tokens + cache_creation_input_tokens"
    )
    for secret in (instruction, "sentinel-key", "private thought", "private data"):
        assert secret not in json.dumps(metadata)


@pytest.mark.parametrize("wire_api", ["responses", "messages", "completions"])
@pytest.mark.parametrize("max_tokens", [True, False, 0, -1, 1.5, "128"])
def test_rejects_invalid_output_limits(tmp_path: Path, wire_api: str, max_tokens: Any):
    with pytest.raises(ValueError, match="max_tokens.*positive integer"):
        AgentGatewayAgent(
            logs_dir=tmp_path,
            model_name="model",
            wire_api=wire_api,
            max_tokens=max_tokens,
        )


@pytest.mark.parametrize("wire_api", ["unknown", "Messages", "", None, []])
def test_rejects_unknown_wire_api(tmp_path: Path, wire_api: Any):
    with pytest.raises(ValueError, match="wire_api"):
        AgentGatewayAgent(logs_dir=tmp_path, model_name="model", wire_api=wire_api)


async def test_messages_requires_explicit_output_limit_before_request(
    tmp_path: Path, patch_transport: TransportInstaller
):
    requests = patch_transport()
    with pytest.raises(ValueError, match="messages.*max_tokens"):
        await run_agent(
            tmp_path, api_base="http://gateway.invalid/v1", wire_api="messages"
        )
    assert requests == []


@pytest.mark.parametrize(
    "stop_reason", ["end_turn", "stop_sequence", "max_tokens", "refusal"]
)
@pytest.mark.parametrize("text", ["", " partial answer\n", "I cannot help with that."])
async def test_messages_terminal_text_is_exported(
    tmp_path: Path, patch_transport: TransportInstaller, stop_reason: str, text: str
):
    body = messages_response(text)
    body["stop_reason"] = stop_reason
    patch_transport(lambda request: httpx.Response(200, json=body))
    environment, context = await run_agent(
        tmp_path,
        api_base="http://gateway.invalid/v1",
        wire_api="messages",
        max_tokens=128,
    )
    assert (tmp_path / "response.txt").read_text() == text
    environment.upload_file.assert_awaited_once()
    assert context.n_input_tokens is None
    assert context.n_cache_tokens is None
    assert context.n_output_tokens is None
    metadata = json.loads((tmp_path / "agentgateway.json").read_text())
    assert metadata["stop_reason"] == stop_reason
    assert metadata["status"] == (
        "incomplete" if stop_reason == "max_tokens" else "completed"
    )
    assert metadata["incomplete_reason"] == (
        "max_output_tokens" if stop_reason == "max_tokens" else None
    )
    assert metadata["usage"] is None


@pytest.mark.parametrize(
    "updates",
    [
        {"type": "error"},
        {"role": "user"},
        {"stop_reason": None},
        {"stop_reason": "tool_use"},
        {"stop_reason": "pause_turn"},
        {"stop_reason": "content_filter"},
        {"stop_reason": "model_context_window_exceeded"},
        {"stop_reason": {"secret": "private data"}},
        {"content": None},
        {"content": {}},
        {"content": []},
        {"content": ["private data"]},
        {"content": [{"type": "text", "text": None}]},
        {"content": [{"type": "thinking", "thinking": "private data"}]},
        {"content": [{"type": "text", "text": "answer"}, {"type": "tool_use"}]},
        {"content": [{"type": "unknown", "text": "private data"}]},
    ],
)
async def test_messages_invalid_answers_never_upload(
    tmp_path: Path, patch_transport: TransportInstaller, updates: dict[str, Any]
):
    body = {**messages_response(), **updates}
    requests = patch_transport(lambda request: httpx.Response(200, json=body))
    environment = AsyncMock()
    agent = AgentGatewayAgent(
        logs_dir=tmp_path,
        model_name="model",
        api_base="http://gateway.invalid/v1",
        wire_api="messages",
        max_tokens=128,
    )
    with pytest.raises(RuntimeError, match="AgentGateway") as exc:
        await agent.run("instruction", environment, AgentContext())
    assert "private data" not in str(exc.value)
    assert len(requests) == 1
    environment.upload_file.assert_not_awaited()
    assert not (tmp_path / "response.txt").exists()


@pytest.mark.parametrize(
    ("usage", "counts"),
    [
        (None, (None, None, None)),
        ({}, (None, None, 0)),
        ({"input_tokens": 0, "output_tokens": 0}, (0, 0, 0)),
        ({"input_tokens": 11}, (11, None, 0)),
        (
            {"cache_read_input_tokens": 3, "cache_creation_input_tokens": 5},
            (None, None, 3),
        ),
    ],
)
async def test_messages_missing_usage_stays_unknown(
    tmp_path: Path,
    patch_transport: TransportInstaller,
    usage: Any,
    counts: tuple[int | None, int | None, int | None],
):
    body = {**messages_response(), "usage": usage}
    patch_transport(lambda request: httpx.Response(200, json=body))
    _, context = await run_agent(
        tmp_path,
        api_base="http://gateway.invalid/v1",
        wire_api="messages",
        max_tokens=128,
    )
    assert (
        context.n_input_tokens,
        context.n_output_tokens,
        context.n_cache_tokens,
    ) == counts


@pytest.mark.parametrize(
    "key",
    [
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ],
)
@pytest.mark.parametrize("value", [True, -1, 1.5, "7", None, [], {}])
async def test_messages_invalid_counters_never_upload(
    tmp_path: Path, patch_transport: TransportInstaller, key: str, value: Any
):
    body = {**messages_response(), "usage": {key: value}}
    patch_transport(lambda request: httpx.Response(200, json=body))
    environment = AsyncMock()
    agent = AgentGatewayAgent(
        logs_dir=tmp_path,
        model_name="model",
        api_base="http://gateway.invalid/v1",
        wire_api="messages",
        max_tokens=128,
    )
    with pytest.raises(RuntimeError, match=key):
        await agent.run("instruction", environment, AgentContext())
    environment.upload_file.assert_not_awaited()


def completions_response(text: str | None = "answer") -> dict[str, Any]:
    return {
        "id": "chatcmpl_123",
        "model": "served-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }


@pytest.mark.parametrize(
    "settings", [{}, {"max_tokens": 128, "reasoning_effort": "high"}]
)
async def test_completions_request_and_usage(
    tmp_path: Path, patch_transport: TransportInstaller, settings: dict[str, Any]
):
    body = completions_response(" answer\n")
    body["usage"] = {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "prompt_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 5},
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 5,
        "completion_tokens_details": {"reasoning_tokens": 2},
        "upstream_debug": "private data",
    }
    requests = patch_transport(lambda request: httpx.Response(200, json=body))
    environment, context = await run_agent(
        tmp_path,
        api_base="http://gateway.invalid/v1/",
        wire_api="completions",
        extra_env={"AGENTGATEWAY_API_KEY": "sentinel-key"},
        **settings,
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.url == httpx.URL("http://gateway.invalid/v1/chat/completions")
    assert request.headers["Authorization"] == "Bearer sentinel-key"
    assert "anthropic-version" not in request.headers
    assert json.loads(request.content) == {
        "model": "gateway-model",
        "messages": [{"role": "user", "content": "Follow the instruction."}],
        **settings,
    }
    assert (tmp_path / "response.txt").read_text() == " answer\n"
    environment.upload_file.assert_awaited_once_with(
        source_path=tmp_path / "response.txt", target_path="/workspace/response.txt"
    )
    assert (
        context.n_input_tokens,
        context.n_output_tokens,
        context.n_cache_tokens,
    ) == (11, 7, 3)
    assert context.cost_usd is None
    metadata = json.loads((tmp_path / "agentgateway.json").read_text())
    assert metadata["wire_api"] == "completions"
    assert metadata["stop_reason"] == "stop"
    assert metadata["usage"] == {
        key: value for key, value in body["usage"].items() if key != "upstream_debug"
    }
    assert metadata["request"] == {
        "model": "gateway-model",
        "messages": "<redacted>",
        **settings,
    }
    assert (
        metadata["input_token_accounting"]
        == "provider-reported (cache inclusion depends on gateway route)"
    )
    for secret in ("Follow the instruction.", "sentinel-key", "private data"):
        assert secret not in json.dumps(metadata)


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
@pytest.mark.parametrize(
    ("content", "refusal", "answer"),
    [
        ("", None, ""),
        (" partial\n", None, " partial\n"),
        (None, "I cannot help with that.", "I cannot help with that."),
    ],
)
async def test_completions_terminal_text_and_refusal(
    tmp_path: Path,
    patch_transport: TransportInstaller,
    finish_reason: str,
    content: str | None,
    refusal: str | None,
    answer: str,
):
    body = completions_response(content)
    body["choices"][0]["finish_reason"] = finish_reason
    body["choices"][0]["message"]["refusal"] = refusal
    patch_transport(lambda request: httpx.Response(200, json=body))
    environment, context = await run_agent(
        tmp_path, api_base="http://gateway.invalid/v1", wire_api="completions"
    )
    assert (tmp_path / "response.txt").read_text() == answer
    environment.upload_file.assert_awaited_once()
    assert context.n_input_tokens is None
    assert context.n_cache_tokens is None
    assert context.n_output_tokens is None
    metadata = json.loads((tmp_path / "agentgateway.json").read_text())
    assert metadata["stop_reason"] == finish_reason
    assert metadata["status"] == (
        "incomplete" if finish_reason == "length" else "completed"
    )
    assert metadata["incomplete_reason"] == (
        "max_output_tokens" if finish_reason == "length" else None
    )


@pytest.mark.parametrize(
    "choice",
    [
        None,
        {},
        {"finish_reason": "stop", "message": "private data"},
        *[
            {
                "finish_reason": reason,
                "message": {"role": "assistant", "content": "answer"},
            }
            for reason in (
                None,
                "tool_calls",
                "function_call",
                "content_filter",
                {"secret": "private data"},
            )
        ],
        *[
            {"finish_reason": "stop", "message": message}
            for message in (
                {"role": "user", "content": "answer"},
                {"role": "assistant", "content": None},
                {"role": "assistant", "content": []},
                {"role": "assistant", "content": 7},
                {"role": "assistant", "refusal": 7},
                {
                    "role": "assistant",
                    "content": "answer",
                    "tool_calls": [{"type": "function"}],
                },
                {"role": "assistant", "content": "answer", "tool_calls": {}},
                {"role": "assistant", "content": "answer", "function_call": {}},
            )
        ],
    ],
)
async def test_completions_invalid_choice_never_uploads(
    tmp_path: Path, patch_transport: TransportInstaller, choice: Any
):
    requests = patch_transport(
        lambda request: httpx.Response(200, json={"choices": [choice]})
    )
    environment = AsyncMock()
    agent = AgentGatewayAgent(
        logs_dir=tmp_path,
        model_name="model",
        api_base="http://gateway.invalid/v1",
        wire_api="completions",
    )
    with pytest.raises(RuntimeError, match="AgentGateway") as exc:
        await agent.run("instruction", environment, AgentContext())
    assert "private data" not in str(exc.value)
    assert len(requests) == 1
    environment.upload_file.assert_not_awaited()
    assert not (tmp_path / "response.txt").exists()


@pytest.mark.parametrize("choices", [None, {}, [], [None, None]])
async def test_completions_requires_exactly_one_choice(
    tmp_path: Path, patch_transport: TransportInstaller, choices: Any
):
    patch_transport(lambda request: httpx.Response(200, json={"choices": choices}))
    environment = AsyncMock()
    agent = AgentGatewayAgent(
        logs_dir=tmp_path,
        model_name="model",
        api_base="http://gateway.invalid/v1",
        wire_api="completions",
    )
    with pytest.raises(RuntimeError, match="exactly one choice"):
        await agent.run("instruction", environment, AgentContext())
    environment.upload_file.assert_not_awaited()


@pytest.mark.parametrize(
    "usage",
    [
        [],
        {"prompt_tokens": True},
        {"completion_tokens": -1},
        {"total_tokens": "7"},
        {"cache_read_input_tokens": True},
        {"cache_creation_input_tokens": -1},
        {"prompt_tokens_details": []},
        {"prompt_tokens_details": {"cached_tokens": 1.5}},
        {"prompt_tokens_details": {"cache_write_tokens": "5"}},
        {"completion_tokens_details": {"reasoning_tokens": "2"}},
    ],
)
async def test_completions_invalid_usage_never_uploads(
    tmp_path: Path, patch_transport: TransportInstaller, usage: Any
):
    patch_transport(
        lambda request: httpx.Response(
            200, json={**completions_response(), "usage": usage}
        )
    )
    environment = AsyncMock()
    agent = AgentGatewayAgent(
        logs_dir=tmp_path,
        model_name="model",
        api_base="http://gateway.invalid/v1",
        wire_api="completions",
    )
    with pytest.raises(RuntimeError, match="usage"):
        await agent.run("instruction", environment, AgentContext())
    environment.upload_file.assert_not_awaited()


async def test_responses_metadata_does_not_copy_unvalidated_incomplete_details(
    tmp_path: Path, patch_transport: TransportInstaller
):
    body = completed_response()
    body["incomplete_details"] = {"reason": "private data"}
    patch_transport(lambda request: httpx.Response(200, json=body))
    await run_agent(tmp_path, api_base="http://gateway.invalid/v1")
    assert "private data" not in (tmp_path / "agentgateway.json").read_text()


async def test_completions_accepts_gateway_nullable_usage_details(
    tmp_path: Path, patch_transport: TransportInstaller
):
    body = completions_response()
    # The gateway serializes an absent optional reasoning count as null.
    body["usage"] = {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "completion_tokens_details": {"audio_tokens": 5, "reasoning_tokens": None},
    }
    patch_transport(lambda request: httpx.Response(200, json=body))
    environment, context = await run_agent(
        tmp_path, api_base="http://gateway.invalid/v1", wire_api="completions"
    )
    environment.upload_file.assert_awaited_once()
    assert (tmp_path / "response.txt").read_text() == "answer"
    assert (context.n_input_tokens, context.n_output_tokens) == (11, 7)
    metadata = json.loads((tmp_path / "agentgateway.json").read_text())
    assert metadata["usage"] == body["usage"]
