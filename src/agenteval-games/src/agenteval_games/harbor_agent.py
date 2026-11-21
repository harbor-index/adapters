"""Harbor adapter for the native AgentEval text-conversation policy."""

import asyncio
import json
import math
import os
import shlex

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .native_policy import (
    ChatCompletionsClient,
    DEFAULT_EPISODE_TIMEOUT_SEC,
    ModelSettings,
    POLICY_VERSION,
    ProtocolError,
    new_report,
    run_episode,
    timestamp,
    validate_base_url,
    write_report,
)


# The command contains no model credentials or model URL, only the game action.
_GAME_REQUEST = """import json,sys,urllib.request
url,method,payload,timeout=sys.argv[1:]
data=payload.encode('utf-8') if method=='POST' else None
request=urllib.request.Request(url,data=data,method=method,headers={'Content-Type':'application/json'})
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(request,timeout=float(timeout)) as response:
    print(response.read().decode('utf-8'))
"""


class AgentEvalNativeAgent(BaseAgent):
    def __init__(
        self,
        *args,
        temperature: float,
        max_tokens: int,
        request_timeout_sec: float,
        top_p: float = 1.0,
        api_base: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        environment_timeout_sec: float = 30.0,
        episode_timeout_sec: float = DEFAULT_EPISODE_TIMEOUT_SEC,
        game_base_url: str = "http://game:8080",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._settings = ModelSettings(
            self.model_name, temperature, max_tokens, request_timeout_sec, top_p
        )
        self._api_base = validate_base_url(
            api_base or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        )
        self._api_key_env = api_key_env
        self._game_base = validate_base_url(game_base_url)
        if not math.isfinite(environment_timeout_sec) or environment_timeout_sec <= 0:
            raise ValueError("environment_timeout_sec must be positive and finite")
        if not math.isfinite(episode_timeout_sec) or episode_timeout_sec <= 0:
            raise ValueError("episode_timeout_sec must be positive and finite")
        self._environment_timeout = environment_timeout_sec
        self._episode_timeout = episode_timeout_sec

    @staticmethod
    def name() -> str:
        return "agenteval-native"

    def version(self) -> str:
        return POLICY_VERSION

    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    async def _game_request(self, environment, path, action=None):
        arguments = [
            "python3",
            "-c",
            _GAME_REQUEST,
            self._game_base + path,
            "GET" if action is None else "POST",
            ""
            if action is None
            else json.dumps({"action": action}, ensure_ascii=False),
            str(self._environment_timeout),
        ]
        result = await environment.exec(
            command=shlex.join(arguments),
            timeout_sec=self._environment_timeout + 5,
        )
        if result.return_code != 0:
            raise ProtocolError(
                "Game HTTP bridge failed (exit " + str(result.return_code) + ")"
            )
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError):
            raise ProtocolError("Game HTTP bridge returned invalid JSON") from None

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        # `instruction` contains terminal-agent guidance. The native policy gets
        # the exact upstream initial observation directly from the game service.
        report = new_report(self._settings, "harbor")
        report.update(
            api_base=self._api_base, environment_timeout_sec=self._environment_timeout
        )
        path = self.logs_dir / "agenteval-native.json"

        def checkpoint():
            context.n_input_tokens = report["usage"]["input_tokens"]
            context.n_output_tokens = report["usage"]["output_tokens"]
            context.n_cache_tokens = report["usage"]["cache_tokens"]
            context.metadata = {
                "status": report["status"],
                "policy_version": POLICY_VERSION,
                "settings": report["settings"],
                "attempts": len(report["attempts"]),
            }
            write_report(path, report)

        checkpoint()
        try:
            client = ChatCompletionsClient(
                self._settings, self._api_base, os.environ.get(self._api_key_env, "")
            )
            initial = await self._game_request(environment, "/observation")
            await run_episode(
                game=initial["game"],
                initial=initial,
                complete=client.complete,
                step=lambda action: self._game_request(environment, "/step", action),
                report=report,
                checkpoint=checkpoint,
                episode_timeout_sec=self._episode_timeout,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            report.update(status="interrupted", ended_at=timestamp())
            checkpoint()
            raise
        except Exception as exc:
            report.update(
                status="error", error_type=type(exc).__name__, ended_at=timestamp()
            )
            checkpoint()
            raise
