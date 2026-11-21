"""Check the real Harbor command builder without Docker or API calls (Harbor 0.23.0)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from harbor.agents.installed.codex import Codex


async def main():
    environment = object()
    prompt = "Read README.\nTry godot --headless --path /workspace/game res://main.tscn\nKeep '$x' literal."
    agent = SimpleNamespace(
        model_name="openai/gpt-5-mini", build_cli_flags=lambda: "-c model_reasoning_effort=high",
        _resolve_auth_json_path=lambda: None,
        model_connection=SimpleNamespace(api_key="test-not-a-real-key", configured_base_url=None),
        _REMOTE_CODEX_HOME=Codex._REMOTE_CODEX_HOME,
        _REMOTE_CODEX_SECRETS_DIR=Codex._REMOTE_CODEX_SECRETS_DIR,
        _OUTPUT_FILENAME=Codex._OUTPUT_FILENAME,
        exec_as_agent=AsyncMock(), _build_effective_config=lambda url: {},
        _upload_effective_config=AsyncMock(), _upload_config_text=AsyncMock(),
        _build_register_skills_command=lambda: "", _load=False, _resume=False, logger=Mock(),
    )
    await Codex.run.__wrapped__(agent, prompt, environment, None)
    upload = agent._upload_config_text.await_args
    assert upload.kwargs["content"] == prompt
    assert upload.kwargs["remote_path"] == "/tmp/codex-secrets/instruction.txt"
    commands = [call.kwargs["command"] for call in agent.exec_as_agent.await_args_list]
    launch, = [command for command in commands if "codex exec" in command]
    assert "- < /tmp/codex-secrets/instruction.txt" in launch
    assert "godot --headless" not in launch and "Keep '$x'" not in launch
    assert "</dev/null" not in launch
    print("Codex prompt bytes preserved; prompt absent from launcher argv; no API calls")


if __name__ == "__main__":
    asyncio.run(main())
