"""Generate the two complete AgentEval game test splits (25 tasks each)."""

import hashlib
import json
from pathlib import Path
import shutil
from .runtime.engine import reference_actions

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
UPSTREAM_HASHES = {
    "maze_test.json": "412d4572c7310ee82efe008336503aaf17140fabaf057c5d59a5fda6070d4b92",
    "wordle_test.json": "35fafff8375a0587b491eab80f4a5beb2300a63c2dce0a861838114285ca804a",
    "tweet_words.txt": "dfa9fafe23f836d59babd9c4e9085eacb5c6336fd6cf129d6eb5c89a7303dc11",
}


def records():
    for name, expected in UPSTREAM_HASHES.items():
        if hashlib.sha256((DATA / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Pinned upstream asset changed: " + name)
    result = []
    for game in ("maze", "wordle"):
        for record in json.loads((DATA / (game + "_test.json")).read_text()):
            upstream_id = record["item_id"]
            index = int(upstream_id.rsplit("_", 1)[1])
            result.append(
                {
                    "game": game,
                    "index": index,
                    "upstream_id": upstream_id,
                    "task_id": "agenteval-" + game + "-" + str(index).zfill(2),
                }
            )
    return result


class AgentEvalGamesAdapter:
    def __init__(
        self, output_dir, limit=None, overwrite=False, task_ids=None, split="all"
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split

    def run(self):
        tasks = records()
        if self.split != "all":
            tasks = [t for t in tasks if t["game"] == self.split]
        if self.task_ids:
            requested = set(self.task_ids)
            known = {
                alias
                for t in tasks
                for alias in (
                    t["task_id"],
                    t["upstream_id"],
                    "agentgym/" + t["task_id"],
                )
            }
            if requested - known:
                raise ValueError(
                    "Unknown task IDs: " + ", ".join(sorted(requested - known))
                )
            tasks = [
                t
                for t in tasks
                if requested.intersection(
                    (t["task_id"], t["upstream_id"], "agentgym/" + t["task_id"])
                )
            ]
        if self.limit is not None:
            if self.limit < 1:
                raise ValueError("--limit must be positive")
            tasks = tasks[: self.limit]
        if not tasks:
            raise ValueError("No tasks selected")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        destinations = [self.output_dir / task["task_id"] for task in tasks]
        if not self.overwrite and any(path.exists() for path in destinations):
            raise FileExistsError(
                "Task output already exists; use --overwrite to regenerate"
            )
        for task, destination in zip(tasks, destinations):
            if destination.is_symlink():
                raise ValueError("Refusing to overwrite a symlink: " + str(destination))
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(ROOT / "task-template", destination)
            config = {"game": task["game"], "index": task["index"]}
            for area in ("environment", "tests"):
                shutil.copytree(
                    ROOT / "runtime",
                    destination / area / "runtime",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                shutil.copytree(DATA, destination / area / "data")
                (destination / area / "config.json").write_text(
                    json.dumps(config, sort_keys=True) + "\n"
                )
            (destination / "solution/actions.json").write_text(
                json.dumps(reference_actions(config, DATA)) + "\n"
            )
            values = dict(
                task,
                role="You are an expert maze solver."
                if task["game"] == "maze"
                else "You are an expert wordle player.",
                max_turns=15 if task["game"] == "maze" else 8,
            )
            for name in ("task.toml", "instruction.md"):
                file = destination / name
                text = file.read_text()
                for key, value in values.items():
                    text = text.replace("{" + key + "}", str(value))
                file.write_text(text)
            for script in ("tests/test.sh", "solution/solve.sh", "environment/game.py"):
                (destination / script).chmod(0o755)
        return destinations
