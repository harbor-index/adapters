#!/usr/bin/env python3
"""Check native Harbor wiring with a local reference-action replay, not model parity.

Runs only maze-00 and wordle-00. The mock reads their generated oracle actions
and returns them through a loopback Chat Completions endpoint with a dummy key.
No model provider is contacted. Docker/Harbor must already be installed.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading


TASKS = ("agenteval-maze-00", "agenteval-wordle-00")
MODEL = "local/mock-agent"
KEY = "local-test-only"
KEY_ENV = "AGENTEVAL_LOCAL_TEST_KEY"
ACK = "OK. I'll follow your instructions and try my best to solve the task."


class MockServer(ThreadingHTTPServer):
    def __init__(self, actions):
        super().__init__(("127.0.0.1", 0), Handler)
        self.actions = actions
        self.records = []
        self.errors = []
        self.lock = threading.Lock()

    def completion(self, payload):
        if payload.get("model") != MODEL or "tools" in payload:
            raise ValueError("Unexpected model or tools in native request")
        messages = payload["messages"]
        roles = {
            "You are an expert maze solver.": "maze",
            "You are an expert wordle player.": "wordle",
        }
        game = roles[messages[0]["content"]]
        if messages[1] != {"role": "assistant", "content": ACK}:
            raise ValueError("Native acknowledgement differs from upstream")
        if len(messages) < 3 or (len(messages) - 3) % 2:
            raise ValueError("Unexpected native history length")
        turn = (len(messages) - 3) // 2
        if [message["role"] for message in messages] != [
            "user",
            "assistant",
            "user",
        ] + ["assistant", "user"] * turn:
            raise ValueError("Unexpected native role sequence")
        actions = self.actions[game]
        if turn >= len(actions):
            raise ValueError("Native agent requested more actions than the oracle")
        if [messages[3 + 2 * index]["content"] for index in range(turn)] != actions[
            :turn
        ]:
            raise ValueError("Raw assistant actions changed in native history")
        if sum(record["game"] == game for record in self.records) != turn:
            raise ValueError("Duplicate or out-of-order model request")
        self.records.append(
            {
                "game": game,
                "turn": turn + 1,
                "action": actions[turn],
                "initial_roles": [message["role"] for message in messages[:3]],
            }
        )
        return {
            "choices": [
                {"message": {"content": actions[turn]}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        with self.server.lock:
            try:
                if (
                    self.path != "/v1/chat/completions"
                    or self.headers.get("Authorization") != "Bearer " + KEY
                ):
                    raise ValueError("Unexpected endpoint or dummy credentials")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1_048_576:
                    raise ValueError("Invalid request length")
                payload = self.server.completion(json.loads(self.rfile.read(length)))
                code = 200
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                self.server.errors.append(type(exc).__name__ + ": " + str(exc))
                payload, code = {"error": "Native protocol check failed"}, 400
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def harbor_passed(result):
    stats = result.get("stats", {})
    if (
        result.get("n_total_trials") != 2
        or stats.get("n_completed_trials") != 2
        or stats.get("n_errored_trials") != 0
    ):
        return False
    passed = []
    for evaluation in stats.get("evals", {}).values():
        for reward, trials in (
            evaluation.get("reward_stats", {}).get("reward", {}).items()
        ):
            if float(reward) == 1:
                passed.extend(name.split("__", 1)[0] for name in trials)
    return sorted(passed) == sorted(TASKS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--harbor",
        required=True,
        help="Harbor executable in the environment with this adapter installed",
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        required=True,
        help="Generated task directory containing maze-00 and wordle-00",
    )
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument(
        "--job-name",
        required=True,
        help="Fresh name; existing jobs/check artifacts are never overwritten",
    )
    args = parser.parse_args()
    executable = shutil.which(os.path.expanduser(args.harbor))
    if not executable:
        parser.error("--harbor must name an executable")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.job_name):
        parser.error(
            "--job-name must start with a letter/digit and contain only letters, digits, dots, underscores, or hyphens"
        )
    root, jobs = args.dataset_dir.resolve(), args.jobs_dir.resolve()
    artifacts = {
        kind: jobs / (args.job_name + "." + kind + ".json")
        for kind in ("smoke-config", "mock-requests", "smoke-check")
    }
    if any(path.exists() for path in [jobs / args.job_name, *artifacts.values()]):
        parser.error("Job name already exists; choose a fresh --job-name")
    actions = {}
    for game, task in zip(("maze", "wordle"), TASKS):
        try:
            values = json.loads((root / task / "solution/actions.json").read_text())
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) for value in values)
            ):
                raise ValueError("Expected a non-empty list of action strings")
            actions[game] = values
        except (OSError, ValueError) as exc:
            parser.error("Cannot load generated oracle for " + task + ": " + str(exc))
    jobs.mkdir(parents=True, exist_ok=True)
    server = MockServer(actions)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {
        "job_name": args.job_name,
        "jobs_dir": str(jobs),
        "datasets": [{"path": str(root), "task_names": list(TASKS)}],
        "agents": [
            {
                "import_path": "agenteval_games.harbor_agent:AgentEvalNativeAgent",
                "model_name": MODEL,
                "kwargs": {
                    "temperature": 0.0,
                    "max_tokens": 128,
                    "request_timeout_sec": 10,
                    "episode_timeout_sec": 120,
                    "api_key_env": KEY_ENV,
                    "api_base": "http://127.0.0.1:" + str(server.server_port) + "/v1",
                },
            }
        ],
        "environment": {"type": "docker", "delete": True},
        "orchestrator": {
            "type": "local",
            "n_concurrent_trials": 2,
            "retry": {"max_retries": 0},
        },
    }
    artifacts["smoke-config"].write_text(json.dumps(config, indent=2) + "\n")
    return_code = None
    try:
        result = subprocess.run(
            [executable, "run", "-c", str(artifacts["smoke-config"])],
            env={
                **os.environ,
                KEY_ENV: KEY,
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            },
        )
        return_code = result.returncode
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        artifacts["mock-requests"].write_text(
            json.dumps({"requests": server.records, "errors": server.errors}, indent=2)
            + "\n"
        )
        try:
            result = json.loads((jobs / args.job_name / "result.json").read_text())
            passed = return_code == 0 and not server.errors and harbor_passed(result)
            passed = passed and all(
                sum(record["game"] == game for record in server.records) == len(values)
                for game, values in actions.items()
            )
        except (OSError, ValueError, TypeError):
            passed = False
        artifacts["smoke-check"].write_text(
            json.dumps(
                {
                    "kind": "local_reference_action_replay_not_model_parity",
                    "passed": passed,
                    "harbor_return_code": return_code,
                    "task_ids": list(TASKS),
                    "model_requests": len(server.records),
                    "external_model_calls": 0,
                },
                indent=2,
            )
            + "\n"
        )
    print(
        "Native Harbor wiring check "
        + ("passed" if passed else "failed")
        + ": "
        + str(artifacts["smoke-check"])
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
