"""No model API calls: policy/error tests and loopback HTTP integration tests."""

import asyncio
import ast
from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agenteval_games.native_policy import (
    ChatCompletionsClient,
    ModelSettings,
    ProtocolError,
    initial_messages,
    new_report,
    run_episode,
)
from agenteval_games.original_runner import (
    OriginalEnvironment,
    original_parse_action,
    run_original,
    select_tasks,
)


SETTINGS = ModelSettings("test-provider/model-id", 0.25, 123, 5.0, 0.9)


def state(observation="initial", *, done=False, reward=0, remaining=15):
    return {
        "game": "maze",
        "observation": observation,
        "done": done,
        "reward": reward,
        "turns_remaining": remaining,
        "episode_finished": done or remaining == 0,
    }


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_history_and_last_reward(self):
        prompts = []
        received = []
        raw = [" Thought: examine\nAction: move down\n", "Action: move left</s>"]

        async def complete(messages):
            prompts.append(copy.deepcopy(messages))
            return {"content": raw[len(prompts) - 1], "usage": {"input_tokens": 2}}

        async def step(action):
            received.append(action)
            return state(
                "feedback " + str(len(received)),
                done=len(received) == 2,
                reward=1 if len(received) == 2 else 0,
                remaining=15 - len(received),
            )

        report = new_report(SETTINGS, "test")
        await run_episode(
            game="maze", initial=state(), complete=complete, step=step, report=report
        )
        self.assertEqual(prompts[0], initial_messages("maze", "initial"))
        self.assertEqual(
            prompts[1],
            prompts[0]
            + [
                {"role": "assistant", "content": raw[0]},
                {"role": "user", "content": "feedback 1"},
            ],
        )
        self.assertEqual(received, raw)
        self.assertEqual(report["reward"], 1)
        self.assertEqual(report["usage"]["input_tokens"], 4)
        self.assertEqual(report["outcome"], "success")
        self.assertFalse(report["truncated"])

    async def test_maze_harness_truncation_and_wordle_environment_done(self):
        for game, effective_limit in (("maze", 15), ("wordle", 6)):
            count = 0

            async def complete(messages):
                return {"content": "invalid action"}

            async def step(action):
                nonlocal count
                count += 1
                return state(
                    "invalid",
                    done=game == "wordle" and count == 6,
                    remaining=(15 if game == "maze" else 8) - count,
                )

            report = new_report(SETTINGS, "test")
            await run_episode(
                game=game,
                initial=state(remaining=15 if game == "maze" else 8),
                complete=complete,
                step=step,
                report=report,
            )
            self.assertEqual(count, effective_limit)
            self.assertEqual(report["reward"], 0)
            self.assertEqual(
                report["outcome"], "truncated" if game == "maze" else "failure"
            )

    async def test_errors_and_interruptions_are_not_zero_scores_or_retried(self):
        for kind in ("model", "step", "cancel"):
            calls = {"model": 0, "step": 0}

            async def complete(messages):
                calls["model"] += 1
                if kind == "model":
                    raise ProtocolError("API unavailable")
                if kind == "cancel":
                    raise asyncio.CancelledError()
                return {"content": "Action: move down"}

            async def step(action):
                calls["step"] += 1
                raise ProtocolError("POST outcome uncertain")

            report = new_report(SETTINGS, "test")
            with self.assertRaises(
                asyncio.CancelledError if kind == "cancel" else ProtocolError
            ):
                await run_episode(
                    game="maze",
                    initial=state(),
                    complete=complete,
                    step=step,
                    report=report,
                )
            self.assertEqual(calls["model"], 1)
            self.assertEqual(calls["step"], 1 if kind == "step" else 0)
            self.assertEqual(
                report["status"], "interrupted" if kind == "cancel" else "error"
            )
            self.assertNotIn("reward", report)
            self.assertEqual(len(report["attempts"]), 1)

    async def test_premature_truncation_is_error(self):
        async def complete(messages):
            return {"content": "anything"}

        async def step(action):
            return {**state(remaining=14), "episode_finished": True}

        report = new_report(SETTINGS, "test")
        with self.assertRaises(ProtocolError):
            await run_episode(
                game="maze",
                initial=state(),
                complete=complete,
                step=step,
                report=report,
            )
        self.assertEqual(report["status"], "error")
        self.assertNotIn("reward", report)

    async def test_shared_deadline_is_error_without_score(self):
        async def complete(messages):
            await asyncio.Event().wait()

        async def step(action):
            raise AssertionError("No action should execute")

        report = new_report(SETTINGS, "test")
        with self.assertRaises(TimeoutError):
            await run_episode(
                game="maze",
                initial=state(),
                complete=complete,
                step=step,
                report=report,
                episode_timeout_sec=0.01,
            )
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["timeout_scope"], "episode")
        self.assertNotIn("reward", report)


class SelectionAndParsingTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("AGENTEVAL_UPSTREAM_DIR"),
        "Set AGENTEVAL_UPSTREAM_DIR for pinned source checks",
    )
    def test_parser_ast_matches_both_pinned_original_clients(self):
        import inspect
        from agenteval_games.original_runner import verify_upstream

        path = Path(os.environ["AGENTEVAL_UPSTREAM_DIR"])
        verify_upstream(path)
        source = ast.parse((path / "agentenv/agentenv/envs/lmrlgym.py").read_text())
        implementation = (
            ast.parse(inspect.getsource(original_parse_action)).body[0].body[:3]
        )
        for classname in ("MazeEnvClient", "WordleEnvClient"):
            cls = next(
                node
                for node in source.body
                if isinstance(node, ast.ClassDef) and node.name == classname
            )
            step = next(
                node
                for node in cls.body
                if isinstance(node, ast.FunctionDef) and node.name == "step"
            )
            self.assertEqual(
                [ast.dump(node) for node in implementation],
                [ast.dump(node) for node in step.body[1:4]],
            )

    def test_all_official_ids_and_selection(self):
        tasks = select_tasks()
        self.assertEqual(len(tasks), 50)
        for game in ("maze", "wordle"):
            self.assertEqual(
                [task["index"] for task in tasks if task["game"] == game],
                list(range(25)),
            )
        self.assertEqual(
            [
                task["task_id"]
                for task in select_tasks(
                    ["lmrlgym_maze_0,agentgym/agenteval-wordle-24"]
                )
            ],
            ["agenteval-maze-00", "agenteval-wordle-24"],
        )
        with self.assertRaises(ValueError):
            select_tasks(["agenteval-maze-25"])

    def test_original_literal_parser_edge_cases(self):
        expected = {
            " Thought: go\nAction: move down ": "move down",
            "Action: move downAction: move up": "move down",
            "move down</s>": "move dow",  # Original five-character slice.
            "Action: a b c d e\n</s>": "a b c d e",
            "action: move down": "action: move down",
            "Action: U P P E R": "U P P E R",
            "Action: a\tb c d e": "a\tb c d e",
        }
        for raw, parsed in expected.items():
            self.assertEqual(original_parse_action(raw), parsed)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, value, code=200):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/observation":
            return self.reply(state(self.server.initial_prompt))
        if self.path == "/wordle/filtered_vocab?id=7":
            return self.reply(self.server.vocab)
        self.reply({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, body))
        if self.path == "/v1/chat/completions":
            self.server.model_requests.append(body)
            self.server.auth.append(self.headers.get("Authorization"))
            if self.server.model_error:
                return self.reply({"error": "not a completion"}, 503)
            return self.reply(
                {
                    "choices": [
                        {
                            "message": {"content": self.server.action},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 9,
                        "completion_tokens": 4,
                        "prompt_tokens_details": {"cached_tokens": 2},
                    },
                }
            )
        if self.path.endswith("/create"):
            return self.reply({"id": 7})
        if self.path.endswith("/reset"):
            return self.reply({"id": 7, "observation": "server observation"})
        if self.path.endswith("/step"):
            return self.reply(state("Success\n", done=True, reward=1, remaining=14))
        self.reply({}, 404)


@contextmanager
def http_server():
    from agenteval_games.original_runner import DATA_DIR

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.model_requests = []
    server.auth = []
    server.action = "Thought: move\nAction: move down"
    server.model_error = False
    server.vocab = (DATA_DIR / "tweet_words.txt").read_text().splitlines()
    server.initial_prompt = (
        DATA_DIR / "maze_prompt.txt"
    ).read_text() + "\nserver observation"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_ledger_scores_only_complete_runs(self):
        for model_error in (False, True):
            with (
                http_server() as (server, url),
                tempfile.TemporaryDirectory() as temporary,
            ):
                server.model_error = model_error
                output = Path(temporary) / "result.json"
                args = SimpleNamespace(
                    output_json=str(output),
                    task_ids=["agenteval-maze-00", "agenteval-wordle-24"],
                    split="all",
                    upstream_dir="unused-with-mocked-checkout-verification",
                    model=SETTINGS.model,
                    temperature=SETTINGS.temperature,
                    max_tokens=SETTINGS.max_tokens,
                    request_timeout_sec=SETTINGS.request_timeout_sec,
                    top_p=SETTINGS.top_p,
                    extra_body={},
                    api_base=url + "/v1",
                    api_key_env="TEST_AGENT_EVAL_KEY",
                    environment_base_url=url,
                    environment_timeout_sec=5,
                    episode_timeout_sec=20,
                    run_id="local-mock",
                )
                with (
                    patch.dict(os.environ, {"TEST_AGENT_EVAL_KEY": "test-only-key"}),
                    patch(
                        "agenteval_games.original_runner.verify_upstream",
                        return_value={"test_fixture": True},
                    ),
                ):
                    if model_error:
                        with self.assertRaises(ProtocolError):
                            await run_original(args)
                    else:
                        self.assertEqual(await run_original(args), 0)
                report = json.loads(output.read_text())
                self.assertEqual(report["selected_task_ids"], args.task_ids)
                self.assertEqual(
                    report["status"], "error" if model_error else "completed"
                )
                self.assertEqual(report["score"], None if model_error else 1)
                self.assertEqual(len(report["episodes"]), 1 if model_error else 2)
                self.assertEqual(
                    report["episodes"][0]["status"],
                    "error" if model_error else "completed",
                )
                self.assertNotIn("test-only-key", output.read_text())

    async def test_model_payload_and_original_server_wire_protocol(self):
        with http_server() as (server, url):
            client = ChatCompletionsClient(SETTINGS, url + "/v1", "test-only-key")
            environment = OriginalEnvironment(url, "maze", 5)
            initial = await environment.reset(24)
            report = new_report(SETTINGS, "original")
            await run_episode(
                game="maze",
                initial=initial,
                complete=client.complete,
                step=environment.step,
                report=report,
            )
            self.assertEqual(server.requests[0], ("/maze/create", {}))
            self.assertEqual(server.requests[1], ("/maze/reset", {"id": 7, "game": 24}))
            self.assertEqual(
                server.requests[-1], ("/maze/step", {"id": 7, "action": "move down"})
            )
            self.assertEqual(
                server.model_requests,
                [
                    {
                        "model": SETTINGS.model,
                        "messages": initial_messages("maze", server.initial_prompt),
                        "temperature": 0.25,
                        "max_tokens": 123,
                        "top_p": 0.9,
                    }
                ],
            )
            self.assertEqual(server.auth, ["Bearer test-only-key"])
            self.assertEqual(
                report["usage"],
                {"input_tokens": 9, "output_tokens": 4, "cache_tokens": 2},
            )

    async def test_omitted_and_provider_specific_request_fields(self):
        settings = ModelSettings(
            "test-provider/reasoning-model",
            None,
            None,
            5.0,
            None,
            {"max_completion_tokens": 77, "reasoning_effort": "low"},
        )
        messages = initial_messages("maze", "initial")
        with http_server() as (server, url):
            client = ChatCompletionsClient(settings, url + "/v1", "test-only-key")
            await client.complete(messages)
            self.assertEqual(
                server.model_requests,
                [
                    {
                        "model": settings.model,
                        "messages": messages,
                        "max_completion_tokens": 77,
                        "reasoning_effort": "low",
                    }
                ],
            )
        self.assertEqual(
            new_report(settings, "harbor")["settings"]["extra_body"],
            {"max_completion_tokens": 77, "reasoning_effort": "low"},
        )
        # Only top_p is dropped; the other fields keep their explicit values.
        self.assertEqual(
            ModelSettings("m", 0, 64, 5.0, None).request_body(messages),
            {"model": "m", "messages": messages, "temperature": 0, "max_tokens": 64},
        )
        for extra_body in (
            {"messages": []},
            {"max_tokens": 5},
            {"tools": []},
            {"bad": float("nan")},
            {1: "non-string key"},
            ["not", "an", "object"],
        ):
            with self.assertRaises(ValueError):
                ModelSettings("m", 0, 64, 5.0, 1.0, extra_body)
        with self.assertRaises(ValueError):
            ModelSettings("m", 0, 64, None)

    def test_original_runner_parses_omitted_fields(self):
        from agenteval_games.original_runner import json_object, optional

        self.assertIsNone(optional(float)("none"))
        self.assertIsNone(optional(int)("None"))
        self.assertEqual(optional(float)("0"), 0.0)
        self.assertEqual(optional(int)("2048"), 2048)
        self.assertEqual(
            json_object('{"reasoning_effort": "low"}'), {"reasoning_effort": "low"}
        )
        for text in ("[]", "not json"):
            with self.assertRaises(ValueError):
                json_object(text)

    async def test_wordle_prompt_ignores_server_greeting(self):
        from agenteval_games.original_runner import DATA_DIR

        with http_server() as (server, url):
            initial = await OriginalEnvironment(url, "wordle", 5).reset(24)
            expected = (
                (DATA_DIR / "wordle_prompt.txt")
                .read_text()
                .replace("{{vocab}}", "\n".join(server.vocab))
            )
            self.assertEqual(initial["observation"], expected)
            self.assertNotIn("server observation", initial["observation"])
            self.assertEqual(
                server.requests[-1], ("/wordle/reset", {"id": 7, "seed": 24})
            )

    async def test_model_http_failure_does_not_retry(self):
        with http_server() as (server, url):
            server.model_error = True
            client = ChatCompletionsClient(SETTINGS, url + "/v1", "test-only-key")
            with self.assertRaises(ProtocolError):
                await client.complete(initial_messages("maze", "initial"))
            self.assertEqual(len(server.model_requests), 1)

    async def test_harbor_bridge_preserves_raw_text_without_shell_execution(self):
        try:
            from agenteval_games.harbor_agent import AgentEvalNativeAgent
            from harbor.models.agent.context import AgentContext
        except ModuleNotFoundError as exc:
            if exc.name == "harbor":
                self.skipTest(
                    "Install the pinned Harbor dependency for integration test"
                )
            raise

        class Environment:
            def __init__(self):
                self.commands = []

            async def exec(self, command, timeout_sec):
                self.commands.append(command)
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout_sec
                )
                return SimpleNamespace(
                    return_code=process.returncode,
                    stdout=stdout.decode(),
                    stderr=stderr.decode(),
                )

        with http_server() as (server, url), tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "should-not-exist"
            server.action = (
                "Thought: 'quoted'\nAction: $(touch "
                + str(marker)
                + ") `echo injected`"
            )
            agent = AgentEvalNativeAgent(
                logs_dir=Path(temporary),
                model_name=SETTINGS.model,
                temperature=SETTINGS.temperature,
                max_tokens=SETTINGS.max_tokens,
                request_timeout_sec=SETTINGS.request_timeout_sec,
                top_p=SETTINGS.top_p,
                api_base=url + "/v1",
                game_base_url=url,
            )
            environment = Environment()
            context = AgentContext()
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-key"}):
                await agent.run(
                    "Extra terminal guidance must never reach model",
                    environment,
                    context,
                )
            self.assertFalse(marker.exists())
            self.assertEqual(server.requests[-1], ("/step", {"action": server.action}))
            self.assertEqual(
                server.model_requests[0]["messages"],
                initial_messages("maze", server.initial_prompt),
            )
            self.assertTrue(
                all("test-only-key" not in command for command in environment.commands)
            )
            self.assertEqual(context.metadata["status"], "completed")
            self.assertEqual(context.n_input_tokens, 9)
            self.assertEqual(
                json.loads((Path(temporary) / "agenteval-native.json").read_text())[
                    "reward"
                ],
                1,
            )


if __name__ == "__main__":
    unittest.main()
