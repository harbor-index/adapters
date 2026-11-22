#!/usr/bin/env python3
"""Check original-side wiring with a local reference-action replay, not model parity.

Counterpart of check_native_harbor.py. Runs only maze-00 and wordle-00 through
`agenteval_games.original_runner` against an already running, unmodified
upstream `lmrlgym` server. The model is the same loopback mock, fed the same
generated oracle actions; no model provider is contacted.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

from check_native_harbor import KEY, KEY_ENV, MODEL, TASKS, MockServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream-dir",
        type=Path,
        required=True,
        help="Pinned AgentGym checkout the server was started from",
    )
    parser.add_argument(
        "--environment-base-url",
        required=True,
        help="Running upstream server, without /maze or /wordle",
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        required=True,
        help="Generated task directory containing maze-00 and wordle-00",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--run-name",
        required=True,
        help="Fresh name; existing check artifacts are never overwritten",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.run_name):
        parser.error(
            "--run-name must start with a letter/digit and contain only letters, digits, dots, underscores, or hyphens"
        )
    root, output = args.dataset_dir.resolve(), args.output_dir.resolve()
    artifacts = {
        kind: output / (args.run_name + "." + kind + ".json")
        for kind in ("original-run", "mock-requests", "smoke-check")
    }
    if any(path.exists() for path in artifacts.values()):
        parser.error("Run name already exists; choose a fresh --run-name")
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
    output.mkdir(parents=True, exist_ok=True)
    server = MockServer(actions)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return_code = None
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agenteval_games.original_runner",
                "--upstream-dir",
                str(args.upstream_dir),
                "--environment-base-url",
                args.environment_base_url,
                "--model",
                MODEL,
                "--temperature",
                "0",
                "--max-tokens",
                "128",
                "--request-timeout-sec",
                "10",
                "--episode-timeout-sec",
                "120",
                "--api-base",
                "http://127.0.0.1:" + str(server.server_port) + "/v1",
                "--api-key-env",
                KEY_ENV,
                "--run-id",
                args.run_name,
                "--output-json",
                str(artifacts["original-run"]),
                "--task-ids",
                *TASKS,
            ],
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
            report = json.loads(artifacts["original-run"].read_text())
            episodes = report["episodes"]
            passed = (
                return_code == 0
                and not server.errors
                and report["status"] == "completed"
                and report["score"] == 1
                and [episode["task_id"] for episode in episodes] == list(TASKS)
                and all(
                    episode["status"] == "completed" and episode["reward"] == 1
                    for episode in episodes
                )
            )
            passed = passed and all(
                sum(record["game"] == game for record in server.records) == len(values)
                for game, values in actions.items()
            )
        except (OSError, ValueError, TypeError, KeyError):
            passed = False
        artifacts["smoke-check"].write_text(
            json.dumps(
                {
                    "kind": "local_reference_action_replay_not_model_parity",
                    "side": "original",
                    "passed": passed,
                    "runner_return_code": return_code,
                    "task_ids": list(TASKS),
                    "model_requests": len(server.records),
                    "external_model_calls": 0,
                },
                indent=2,
            )
            + "\n"
        )
    print(
        "Native original-side wiring check "
        + ("passed" if passed else "failed")
        + ": "
        + str(artifacts["smoke-check"])
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
