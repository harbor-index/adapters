"""Offline protocol checks; no provider credentials or network calls are used."""

import asyncio
import base64
import json
import re
import shlex
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor")
from harbor.models.agent.context import AgentContext

from deceptionbench import native_agent


class FakeEnvironment:
    def __init__(self, prompt="exact upstream prompt", *, read_code=0, write_code=0):
        self.prompt = prompt
        self.read_code = read_code
        self.write_code = write_code
        self.commands = []
        self.response = "stale response"

    async def exec(self, command):
        self.commands.append(command)
        if command.startswith("rm "):
            self.response = None
            return SimpleNamespace(return_code=0, stdout="")
        if command.startswith("cat "):
            return SimpleNamespace(return_code=self.read_code, stdout=self.prompt)
        assert command.startswith("python -I -c ")
        code = shlex.split(command)[3]
        encoded = re.search(r"b64decode\('([A-Za-z0-9+/=]*)'\)", code).group(1)
        if self.write_code == 0:
            self.response = base64.b64decode(encoded).decode("utf-8")
        return SimpleNamespace(return_code=self.write_code, stdout="")


def stub_client(monkeypatch, responses):
    requests, configs = [], []

    class Client:
        def __init__(self, **config):
            configs.append(config)
            self.chat = SimpleNamespace(completions=self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def create(self, **kwargs):
            requests.append(kwargs)
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=value))],
                usage=SimpleNamespace(prompt_tokens=20, completion_tokens=7),
            )

    monkeypatch.setattr(native_agent, "AsyncOpenAI", Client)
    return requests, configs


def make_agent(tmp_path, **kwargs):
    return native_agent.DeceptionBenchAgent(
        logs_dir=tmp_path,
        model_name="provider/raw-model",
        extra_env={"DECEPTIONBENCH_MODEL_API_KEY": "fixture-key"},
        **kwargs,
    )


def test_native_request_and_artifact_are_faithful(monkeypatch, tmp_path):
    output = '```json\n{"response": "quote \' $() `safe` 雪"}\n```'
    requests, configs = stub_client(monkeypatch, [output])
    env = FakeEnvironment(prompt="raw prompt\nexact suffix")
    context = AgentContext()
    agent = make_agent(tmp_path)
    asyncio.run(agent.run("Do not send this CLI delivery text", env, context))

    assert requests == [
        {
            "model": "provider/raw-model",
            "messages": [{"role": "user", "content": "raw prompt\nexact suffix"}],
            "max_tokens": 500,
            "temperature": 0.0,
            "top_p": 0.95,
        }
    ]
    assert configs[0]["max_retries"] == 2
    assert configs[0]["api_key"] == "fixture-key"
    assert env.response == '{"response": "quote \' $() `safe` 雪"}'
    assert context.n_input_tokens == 20
    assert context.n_output_tokens == 7
    record = json.loads((tmp_path / "native_response.json").read_text())
    assert record["status"] == "completed"
    assert record["attempts"] == 1
    assert "fixture-key" not in json.dumps(record)
    assert "response" not in context.metadata


def test_upstream_retry_schedule_and_secret_safe_failure(monkeypatch, tmp_path):
    requests, _ = stub_client(monkeypatch, [RuntimeError("fixture-secret")] * 3)
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(native_agent.asyncio, "sleep", sleep)
    env = FakeEnvironment()
    context = AgentContext()
    with pytest.raises(RuntimeError, match="after 3 attempts") as exc:
        asyncio.run(make_agent(tmp_path).run("ignored", env, context))
    assert len(requests) == 3
    assert delays == [1, 2]
    assert env.response is None
    assert context.metadata["status"] == "api_error"
    assert "fixture-secret" not in str(exc.value)
    assert "fixture-secret" not in (tmp_path / "native_response.json").read_text()


def test_recovered_request_uses_same_prompt_and_config(monkeypatch, tmp_path):
    requests, configs = stub_client(monkeypatch, [RuntimeError("temporary"), "answer"])
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(native_agent.asyncio, "sleep", sleep)
    monkeypatch.setenv("DECEPTIONBENCH_MODEL_BASE_URL", "https://example.invalid/v1")
    env = FakeEnvironment()
    context = AgentContext()
    asyncio.run(make_agent(tmp_path).run("ignored", env, context))
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert configs[0]["base_url"] == "https://example.invalid/v1"
    assert delays == [1]
    assert env.response == "answer"
    assert context.metadata["status"] == "completed"
    assert context.metadata["attempts"] == 2
    assert context.metadata["error_types"] == ["RuntimeError"]


def test_personal_openai_key_is_not_a_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("DECEPTIONBENCH_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "personal-fixture-key")
    requests, configs = stub_client(monkeypatch, [])
    agent = native_agent.DeceptionBenchAgent(logs_dir=tmp_path, model_name="model")
    with pytest.raises(RuntimeError, match="DECEPTIONBENCH_MODEL_API_KEY"):
        asyncio.run(agent.run("ignored", FakeEnvironment(), AgentContext()))
    assert requests == configs == []


@pytest.mark.parametrize("prompt,read_code", [("", 0), ("   ", 0), ("missing", 1)])
def test_invalid_prompt_does_not_call_provider(
    monkeypatch, tmp_path, prompt, read_code
):
    requests, configs = stub_client(monkeypatch, [])
    with pytest.raises(RuntimeError, match="prompt.txt"):
        asyncio.run(
            make_agent(tmp_path).run(
                "ignored", FakeEnvironment(prompt, read_code=read_code), AgentContext()
            )
        )
    assert requests == configs == []


def test_failed_artifact_write_is_not_success(monkeypatch, tmp_path):
    stub_client(monkeypatch, ["native response"])
    context = AgentContext()
    env = FakeEnvironment(write_code=1)
    with pytest.raises(RuntimeError, match="write the native response"):
        asyncio.run(make_agent(tmp_path).run("ignored", env, context))
    assert env.response is None
    assert context.metadata["status"] == "artifact_error"
