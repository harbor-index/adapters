"""Run the shared native policy against AgentGym's actual HTTP environment.

Start the unmodified, pinned upstream ``agentenv_lmrlgym.server:app`` separately.
This module implements its original client protocol; it never imports the
adapter game engine. The supplied checkout is verified, but an HTTP URL cannot
prove which checkout the operator used to start a remote server.
"""

import argparse
import ast
import asyncio
import json
import math
import os
from pathlib import Path
import subprocess

from .native_policy import (
    ChatCompletionsClient,
    DEFAULT_EPISODE_TIMEOUT_SEC,
    ModelSettings,
    ProtocolError,
    ROUND_LIMITS,
    new_report,
    request_json,
    run_episode,
    timestamp,
    validate_base_url,
    validate_state,
    write_report,
)


AGENTGYM_COMMIT = "3ef9235d23e68e7c2920c5422ad957dc8ced5c6c"
LMRL_GYM_COMMIT = "83abeedb3a461c3d7d20572b73318a259d85f2ac"
DATA_DIR = Path(__file__).parent / "data"


def original_parse_action(action: str) -> str:
    # Literal upstream MazeEnvClient/WordleEnvClient.step parser. Preserve its
    # off-by-one EOS slice and first Action: segment, including malformed input.
    if action.endswith("</s>"):
        action = action[:-5]
    _action = action.split("Action:")
    if len(_action) > 1:
        action = _action[1].strip()
    else:
        action = _action[0].strip()
    return action


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True
    )
    if result.returncode:
        raise ValueError("Cannot verify pinned upstream checkout")
    return result.stdout.strip()


def verify_upstream(path: Path) -> dict:
    submodule = path / "agentenv-lmrlgym/lmrlgym"
    if _git(path, "rev-parse", "HEAD") != AGENTGYM_COMMIT:
        raise ValueError("AgentGym checkout does not match the required commit")
    if _git(submodule, "rev-parse", "HEAD") != LMRL_GYM_COMMIT:
        raise ValueError("LMRL-Gym checkout does not match the required commit")
    if _git(
        path,
        "status",
        "--porcelain",
        "--untracked-files=no",
        "--",
        "agentenv",
        "agentenv-lmrlgym",
    ) or _git(submodule, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Upstream environment/client has tracked modifications")
    source = ast.parse((path / "agentenv/agentenv/envs/lmrlgym.py").read_text())
    for game, classname, attribute in (
        ("maze", "MazeEnvClient", "_fully_first_observation"),
        ("wordle", "WordleEnvClient", "first_observation"),
    ):
        cls = next(
            node
            for node in source.body
            if isinstance(node, ast.ClassDef) and node.name == classname
        )
        value = next(
            node.value
            for node in cls.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == attribute
                for target in node.targets
            )
        )
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "strip"
            and isinstance(value.func.value, ast.Constant)
        ):
            raise ValueError("Unexpected upstream prompt declaration")
        if (
            value.func.value.value.strip()
            != (DATA_DIR / (game + "_prompt.txt")).read_text()
        ):
            raise ValueError("Bundled prompt differs from pinned upstream")
    return {
        "agentgym_commit": AGENTGYM_COMMIT,
        "lmrl_gym_commit": LMRL_GYM_COMMIT,
        "checkout_verified": True,
        "server_checkout_operator_asserted": True,
    }


def select_tasks(task_ids=None, split="all") -> list[dict]:
    tasks = []
    for game in ("maze", "wordle"):
        if split not in ("all", game):
            continue
        for row in json.loads((DATA_DIR / (game + "_test.json")).read_text()):
            index = int(row["item_id"].rsplit("_", 1)[1])
            tasks.append(
                {
                    "game": game,
                    "index": index,
                    "upstream_id": row["item_id"],
                    "task_id": "agenteval-" + game + "-" + str(index).zfill(2),
                }
            )
    if task_ids:
        requested = {item for value in task_ids for item in value.split(",") if item}
        known = {
            alias
            for task in tasks
            for alias in (
                task["task_id"],
                task["upstream_id"],
                "agentgym/" + task["task_id"],
            )
        }
        if requested - known:
            raise ValueError(
                "Unknown task IDs: " + ", ".join(sorted(requested - known))
            )
        tasks = [
            task
            for task in tasks
            if requested.intersection(
                (task["task_id"], task["upstream_id"], "agentgym/" + task["task_id"])
            )
        ]
    if not tasks:
        raise ValueError("No tasks selected")
    return tasks


class OriginalEnvironment:
    """HTTP counterpart of the original MazeEnvClient/WordleEnvClient."""

    def __init__(self, base_url: str, game: str, timeout: float):
        self.base_url = validate_base_url(base_url) + "/" + game
        self.game = game
        self.timeout = timeout
        self.env_id = None
        self.turns = 0

    async def _request(self, endpoint, payload=None):
        result = await asyncio.to_thread(
            request_json,
            self.base_url + endpoint,
            payload=payload,
            timeout=self.timeout,
        )
        if isinstance(result, dict) and "error" in result:
            raise ProtocolError("Original game server returned an error")
        return result

    async def reset(self, index: int) -> dict:
        created = await self._request("/create", {})
        if not isinstance(created, dict) or type(created.get("id")) is not int:
            raise ProtocolError("Original create response lacks environment ID")
        self.env_id = created["id"]
        prompt = (DATA_DIR / (self.game + "_prompt.txt")).read_text()
        if self.game == "wordle":
            vocab = await self._request("/filtered_vocab?id=" + str(self.env_id))
            expected = (DATA_DIR / "tweet_words.txt").read_text().splitlines()
            if vocab != expected:
                raise ProtocolError(
                    "Original Wordle vocabulary differs from the pinned benchmark"
                )
            await self._request("/reset", {"id": self.env_id, "seed": index})
            observation = prompt.replace("{{vocab}}", "\n".join(vocab))
        else:
            result = await self._request("/reset", {"id": self.env_id, "game": index})
            if not isinstance(result, dict) or not isinstance(
                result.get("observation"), str
            ):
                raise ProtocolError("Original reset response lacks observation")
            observation = prompt + "\n" + result["observation"]
        self.turns = 0
        return {
            "game": self.game,
            "observation": observation,
            "reward": 0,
            "done": False,
            "turns_remaining": ROUND_LIMITS[self.game],
            "episode_finished": False,
        }

    async def step(self, raw_action: str) -> dict:
        result = validate_state(
            await self._request(
                "/step",
                {"id": self.env_id, "action": original_parse_action(raw_action)},
            )
        )
        self.turns += 1
        return {
            **result,
            "game": self.game,
            "turns_remaining": max(0, ROUND_LIMITS[self.game] - self.turns),
            "episode_finished": result["done"] or self.turns >= ROUND_LIMITS[self.game],
        }


async def run_original(args) -> int:
    output = Path(args.output_json)
    if output.exists():
        raise ValueError("Output already exists; use a new path for each attempt/run")
    tasks = select_tasks(args.task_ids, args.split)
    provenance = verify_upstream(Path(args.upstream_dir))
    settings = ModelSettings(
        args.model,
        args.temperature,
        args.max_tokens,
        args.request_timeout_sec,
        args.top_p,
    )
    client = ChatCompletionsClient(
        settings, args.api_base, os.environ.get(args.api_key_env, "")
    )
    base_url = validate_base_url(args.environment_base_url)
    report = {
        "schema_version": 1,
        "side": "original",
        "run_id": args.run_id,
        "started_at": timestamp(),
        "status": "running",
        "provenance": provenance,
        "api_base": client.base_url,
        "environment_base_url": base_url,
        "environment_timeout_sec": args.environment_timeout_sec,
        "selected_task_ids": [task["task_id"] for task in tasks],
        "episodes": [],
    }

    def checkpoint():
        write_report(output, report)

    checkpoint()
    try:
        for task in tasks:
            episode = new_report(settings, "original")
            episode.update(task)
            report["episodes"].append(episode)
            checkpoint()
            environment = OriginalEnvironment(
                base_url, task["game"], args.environment_timeout_sec
            )
            try:
                initial = await environment.reset(task["index"])
                await run_episode(
                    game=task["game"],
                    initial=initial,
                    complete=client.complete,
                    step=environment.step,
                    report=episode,
                    checkpoint=checkpoint,
                    episode_timeout_sec=args.episode_timeout_sec,
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                episode.update(status="interrupted", ended_at=timestamp())
                raise
            except Exception as exc:
                episode.update(
                    status="error", error_type=type(exc).__name__, ended_at=timestamp()
                )
                raise
        report.update(
            status="completed",
            ended_at=timestamp(),
            score=sum(episode["reward"] for episode in report["episodes"]) / len(tasks),
        )
        checkpoint()
        return 0
    except (asyncio.CancelledError, KeyboardInterrupt):
        report.update(status="interrupted", ended_at=timestamp(), score=None)
        checkpoint()
        raise
    except Exception as exc:
        report.update(
            status="error",
            error_type=type(exc).__name__,
            ended_at=timestamp(),
            score=None,
        )
        checkpoint()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-dir", required=True)
    parser.add_argument(
        "--environment-base-url",
        required=True,
        help="Base URL of actual upstream server, without /maze or /wordle",
    )
    parser.add_argument(
        "--model", required=True, help="Exact provider model identifier; not rewritten"
    )
    parser.add_argument("--temperature", type=float, required=True)
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--request-timeout-sec", type=float, required=True)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--environment-timeout-sec", type=float, default=30.0)
    parser.add_argument(
        "--episode-timeout-sec", type=float, default=DEFAULT_EPISODE_TIMEOUT_SEC
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--split", choices=("all", "maze", "wordle"), default="all")
    args = parser.parse_args()
    for name in ("environment_timeout_sec", "episode_timeout_sec"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(name + " must be positive and finite")
    try:
        raise SystemExit(asyncio.run(run_original(args)))
    except (ValueError, ProtocolError, TimeoutError) as exc:
        parser.exit(1, type(exc).__name__ + ": " + str(exc) + "\n")


if __name__ == "__main__":
    main()
