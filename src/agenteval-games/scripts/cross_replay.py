"""Replay each side's recorded model actions through the other side's environment.

A model-free check on native-policy parity runs. The replies recorded on the
original side are replayed through this adapter's game engine (the code that
runs in the Harbor game sidecar), and the replies recorded on the Harbor side
are replayed through a running upstream `lmrlgym` server. For every episode,
the initial state and every step result must equal what was recorded, so the
two environments are shown to respond identically to the exact action
sequences the model produced. No model is called.
"""

import argparse
import asyncio
import json
from pathlib import Path

from agenteval_games.adapter import DATA
from agenteval_games.original_runner import OriginalEnvironment
from agenteval_games.runtime.engine import Session

FIELDS = ("observation", "reward", "done", "turns_remaining", "episode_finished")


def task_of(task_id: str) -> tuple[str, int]:
    _, game, index = task_id.rsplit("/", 1)[-1].split("-")
    return game, int(index)


def recorded(episode: dict) -> tuple[list[str], list[dict]]:
    if episode["status"] != "completed":
        raise ValueError("Only completed episodes can be replayed")
    actions = [attempt["action"] for attempt in episode["attempts"]]
    states = [episode["initial"]] + [a["result"] for a in episode["attempts"]]
    return actions, states


def original_episodes(path: Path):
    for episode in json.loads(path.read_text())["episodes"]:
        yield episode["task_id"], episode


def harbor_episodes(job_dir: Path):
    for path in sorted(job_dir.glob("*/agent/agenteval-native.json")):
        trial = json.loads((path.parent.parent / "result.json").read_text())
        yield trial["task_name"].rsplit("/", 1)[-1], json.loads(path.read_text())


def through_adapter(task_id: str, actions: list[str]) -> list[dict]:
    game, index = task_of(task_id)
    session = Session({"game": game, "index": index}, DATA)
    return [session.initial] + [session.step(action) for action in actions]


async def through_upstream(base_url, timeout, task_id, actions) -> list[dict]:
    game, index = task_of(task_id)
    environment = OriginalEnvironment(base_url, game, timeout)
    states = [await environment.reset(index)]
    for action in actions:
        states.append(await environment.step(action))
    return states


def differences(task_id: str, expected: list[dict], actual: list[dict]) -> list[str]:
    found = []
    for turn, (want, got) in enumerate(zip(expected, actual, strict=True)):
        for field in FIELDS:
            if want[field] != got[field]:
                found.append("%s turn %d: %s differs" % (task_id, turn, field))
    return found


async def main_async(args) -> dict:
    report = {"comparisons": [], "mismatches": []}
    for source, paths, loader in (
        ("original", args.original, original_episodes),
        ("harbor", args.harbor, harbor_episodes),
    ):
        for path in paths:
            episodes = steps = 0
            for task_id, episode in loader(path):
                actions, states = recorded(episode)
                if source == "original":
                    replayed = through_adapter(task_id, actions)
                else:
                    replayed = await through_upstream(
                        args.environment_base_url, args.timeout, task_id, actions
                    )
                report["mismatches"] += differences(task_id, states, replayed)
                episodes += 1
                steps += len(actions)
            report["comparisons"].append(
                {
                    "recorded_on": source,
                    "run": path.name,
                    "replayed_through": "adapter engine"
                    if source == "original"
                    else "upstream server",
                    "episodes": episodes,
                    "steps": steps,
                }
            )
    report["passed"] = not report["mismatches"]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, nargs="*", default=[])
    parser.add_argument("--harbor", type=Path, nargs="*", default=[])
    parser.add_argument(
        "--environment-base-url",
        default="http://127.0.0.1:36001",
        help="Running upstream server, used to replay Harbor-side episodes",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()
    report = asyncio.run(main_async(args))
    for row in report["comparisons"]:
        print(
            "%(recorded_on)s %(run)s -> %(replayed_through)s: "
            "%(episodes)d episodes, %(steps)d steps" % row
        )
    print("mismatches:", len(report["mismatches"]))
    for line in report["mismatches"][:20]:
        print(" ", line)
    if args.output_json:
        args.output_json.write_text(json.dumps(report, indent=2) + "\n")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
