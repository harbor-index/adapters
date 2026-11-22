#!/usr/bin/env python3
"""Replay the original AgentGym clients/servers against the Harbor game engine.

Use Python 3.9.12 with numpy==1.24.4 and termcolor==2.4.0. Original source
modules are loaded unchanged. Stubs reject execution of unrelated scikit-image
map builders/IPython debugger; training-only transformers/tqdm and controller
base types are import shims. No evaluated transition, prompt, parsing, RNG,
reward, vocabulary, or environment implementation is stubbed.
This is deterministic compatibility validation, not LLM/model parity.
"""

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import random
import sys
import types
from urllib.parse import urlparse, parse_qs

SOURCE_SHA256 = {
    "agentenv/agentenv/envs/lmrlgym.py": "1e7036bec046bf83845036d9fa15c91975f2480c2688b1fad179a6ac83edf6b2",
    "agentenv/agentenv/controller/types.py": "d2eb055f0278955c33c315227c4671ce70c63e12f3d14298964026b349508f01",
    "agentenv/agentenv/controller/task.py": "67bc046f80b4cec2bcfe82a652c0264c94c860c8ece80f8d55cfbaee12e3da7c",
    "agentenv/examples/agentevol/train_agentevol_multi_task.sh": "716873bef0e305b24d70a6b46b0d32b005ff6d8a6e5aa8c266f32e0bf55c8e26",
    "agentenv-lmrlgym/agentenv_lmrlgym/maze/environment.py": "41b8c773cc53711672aa9751d4bbbf5fe6623f5d48af7f3b96b2e3727baf4e2e",
    "agentenv-lmrlgym/agentenv_lmrlgym/maze/utils.py": "8b0a29c8fabacc4e1a6fd2b510c28998c779675eb1285edd5c3871aabd20af6d",
    "agentenv-lmrlgym/agentenv_lmrlgym/wordle/environment.py": "24907bbed4b8a3a363ce3f9c010128c2f99dd04222cbebe8a24cd114d69772ab",
    "agentenv-lmrlgym/lmrlgym/LLM_RL/environment.py": "7d5a47fe62e8b66b46735d6facacbc649558d2323f316d116a56c063a14414b5",
    "agentenv-lmrlgym/lmrlgym/llm_rl_scripts/maze/env/env.py": "d52422227f0e494da5e0e5b4b745de3877cc3c16000a14f5224619682ad0d604",
    "agentenv-lmrlgym/lmrlgym/llm_rl_scripts/maze/env/mazes.py": "97f0786cdf672079c0e68434f416faa42b21b96c0d5ca9710ed5af20b9bfb1d9",
    "agentenv-lmrlgym/lmrlgym/llm_rl_scripts/maze/env/randomness.py": "84e9f30d44068143b126fe3ed868704a77ed47a44dc7104233ae513d5bb8cd4a",
    "agentenv-lmrlgym/lmrlgym/llm_rl_scripts/wordle/env/env.py": "24518e606bbd180c9c130eb7d13dbe2241bf3cc056b72995682e04b255d55889",
    "agentenv-lmrlgym/lmrlgym/llm_rl_scripts/wordle/env/game.py": "64692aae33678eb3a84a46249fc6c2a531bfa13edb4e9daa12b218fa41e57d8a",
    "agentenv-lmrlgym/lmrlgym/llm_rl_scripts/wordle/env/utils.py": "eb85c8979607abe3aaad78116cb229703b5017c951b2046a6653e7531ff162ea",
    "agentenv-lmrlgym/lmrlgym/llm_rl_scripts/wordle/vocab/tweet_words.txt": "dfa9fafe23f836d59babd9c4e9085eacb5c6336fd6cf129d6eb5c89a7303dc11",
}


def verify_sources(root, data):
    for relative, expected in SOURCE_SHA256.items():
        actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError("Pinned upstream source differs: " + relative)
    provenance = json.loads((data / "PROVENANCE.json").read_text())
    for relative, expected in provenance["asset_sha256"].items():
        actual = hashlib.sha256((data / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError("Packaged asset differs: " + relative)
    for game in ("maze", "wordle"):
        records = json.loads((data / (game + "_test.json")).read_text())
        expected = [{"item_id": "lmrlgym_" + game + "_" + str(i)} for i in range(25)]
        if records != expected:
            raise ValueError("AgentEval test IDs changed: " + game)


def package(name, path=None):
    result = types.ModuleType(name)
    if path is not None:
        result.__path__ = [str(path)]
    sys.modules[name] = result
    return result


def reject_unused(*args, **kwargs):
    raise AssertionError("An unrelated dependency stub was unexpectedly executed")


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_original(root):
    lmrl = root / "agentenv-lmrlgym/lmrlgym"
    sys.path.insert(0, str(lmrl))
    package("transformers", "")
    tokenization = package("transformers.tokenization_utils")
    tokenization.PreTrainedTokenizer = type("PreTrainedTokenizer", (), {})
    package("tqdm", "")
    tqdm_auto = package("tqdm.auto")
    tqdm_auto.tqdm = reject_unused
    ipython = package("IPython")
    ipython.embed = reject_unused
    package("skimage", "")
    drawing = package("skimage.draw")
    drawing.disk = drawing.random_shapes = drawing.rectangle = reject_unused
    envroot = root / "agentenv-lmrlgym/agentenv_lmrlgym"
    package("agentenv_lmrlgym", envroot)
    package("agentenv_lmrlgym.maze", envroot / "maze")
    package("agentenv_lmrlgym.wordle", envroot / "wordle")
    maze = load_file(
        "agentenv_lmrlgym.maze.environment", envroot / "maze/environment.py"
    )
    wordle = load_file(
        "agentenv_lmrlgym.wordle.environment", envroot / "wordle/environment.py"
    )

    # Preserve real client classes, their initial prompts, and action parsing.
    package("agentenv", root / "agentenv/agentenv")
    controller = package("agentenv.controller", root / "agentenv/agentenv/controller")

    class BaseEnvClient:
        def __init__(self, *args, **kwargs):
            pass

    controller.BaseEnvClient = BaseEnvClient
    controller.BaseTask = type("BaseTask", (), {})
    load_file(
        "agentenv.controller.types", root / "agentenv/agentenv/controller/types.py"
    )
    package("requests", "")
    req_errors = package("requests.exceptions")
    req_errors.RequestException = type("RequestException", (Exception,), {})
    client = load_file(
        "original_lmrlgym_client", root / "agentenv/agentenv/envs/lmrlgym.py"
    )
    return maze.Lmrlgym_MazeEnv, wordle.Lmrlgym_WordleEnv, client


class Response:
    status_code = 200

    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class Reference:
    def __init__(self, game, index, upstream):
        maze_cls, wordle_cls, client_module = upstream
        self.game = game
        self.server = maze_cls() if game == "maze" else wordle_cls()
        self.rounds = 0
        self.limit = 15 if game == "maze" else 8
        self.episode_finished = False
        self.reward = 0
        self.done = False

        def post(url, json=None, timeout=None):
            route = urlparse(url).path.strip("/")
            payload = json or {}
            return Response(
                getattr(self.server, route)(
                    **(
                        {
                            "idx": payload["id"],
                            **{k: v for k, v in payload.items() if k != "id"},
                        }
                        if "id" in payload
                        else payload
                    )
                )
            )

        def get(url, timeout=None):
            parsed = urlparse(url)
            idx = int(parse_qs(parsed.query)["id"][0])
            return Response(getattr(self.server, "get_" + parsed.path.strip("/"))(idx))

        self.requests = types.SimpleNamespace(post=post, get=get)
        self.client_module = client_module
        with contextlib.redirect_stdout(io.StringIO()):
            client_module.requests = self.requests
            cls = (
                client_module.MazeEnvClient
                if game == "maze"
                else client_module.WordleEnvClient
            )
            self.client = cls("http://local", 25)
            self.client.reset(index)
        self.observation = self.client.observe()

    def public(self):
        return {
            "observation": self.observation,
            "reward": self.reward,
            "done": self.done,
            "turns_remaining": max(0, self.limit - self.rounds),
            "episode_finished": self.episode_finished,
        }

    def step(self, raw):
        if self.episode_finished:
            raise ValueError("Original evaluator has completed this episode")
        with contextlib.redirect_stdout(io.StringIO()):
            self.client_module.requests = self.requests
            result = self.client.step(raw)
        self.rounds += 1
        self.observation, self.reward, self.done = (
            result.state,
            result.reward,
            result.done,
        )
        self.episode_finished = bool(self.done or self.rounds >= self.limit)
        return self.public()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument(
        "--adapter",
        type=Path,
        required=True,
        help="Path containing src/agenteval_games",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data = args.adapter / "src/agenteval_games/data"
    verify_sources(args.upstream, data)
    upstream = load_original(args.upstream)
    sys.path.insert(0, str(args.adapter / "src"))
    from agenteval_games.runtime.engine import Session, reference_actions

    data = args.adapter / "src/agenteval_games/data"
    results = []
    comparisons = 0

    def compare(game, index, name, actions):
        nonlocal comparisons
        original = Reference(game, index, upstream)
        candidate = Session({"game": game, "index": index}, data)

        def native_fields(result):
            if result["game"] != game:
                raise AssertionError("Incorrect adapter game metadata")
            return {key: value for key, value in result.items() if key != "game"}

        assert original.public() == native_fields(candidate.public()), (
            game,
            index,
            name,
            "initial",
            original.public(),
            candidate.public(),
        )
        comparisons += 1
        for turn, action in enumerate(actions):
            if original.episode_finished:
                break
            expected, actual = (
                original.step(action),
                native_fields(candidate.step(action)),
            )
            assert expected == actual, (
                game,
                index,
                name,
                turn,
                repr(action),
                expected,
                actual,
            )
            comparisons += 1
        if original.episode_finished:
            for obj in (original, candidate):
                try:
                    obj.step("panic")
                except ValueError:
                    pass
                else:
                    raise AssertionError(
                        "Episode can be restarted/continued after completion"
                    )
        results.append(
            {
                "game": game,
                "index": index,
                "scenario": name,
                "steps": original.rounds,
                "reward": original.reward,
                "engine_done": original.done,
                "episode_finished": original.episode_finished,
            }
        )
        return original

    for index in range(25):
        actions = reference_actions({"game": "maze", "index": index}, data)
        compare("maze", index, "oracle", actions)
        compare(
            "maze",
            index,
            "success-on-budget-boundary",
            ["invalid"] * (15 - len(actions)) + actions,
        )
        compare(
            "maze",
            index,
            "goal-after-budget-disallowed",
            ["invalid"] * (16 - len(actions)) + actions,
        )
        compare(
            "maze",
            index,
            "invalid-and-parser",
            [
                "MOVE RIGHT",
                "right",
                "move right\nmove down",
                "Action:move left Action:move down",
                "Thought:ok\nAction: move right",
                "move down</s>",
                "move down </s>",
                "",
                "move up",
                "move left",
                "move down",
                "move right",
            ]
            * 2,
        )
        rng = random.Random(1000 + index)
        compare(
            "maze",
            index,
            "random-walk",
            [
                rng.choice(["move up", "move down", "move right", "move left"])
                for _ in range(15)
            ],
        )
        for variant in (0, 1):
            rng = random.Random(index + 2000 + variant)
            words = (data / "tweet_words.txt").read_text().splitlines()
            compare(
                "wordle",
                index,
                "random-words-" + str(variant),
                [rng.choice(words) for _ in range(6)],
            )
        compare(
            "wordle",
            index,
            "invalid-six",
            ["PANIC", "p\ta\tn\ti\tc", "xxxxx", "aaaaa", "", "solar\nrobot"],
        )
        compare(
            "wordle",
            index,
            "repeated-letters",
            ["knoll", "swill", "dodge", "aroma", "robot", "other"],
        )
        compare(
            "wordle",
            index,
            "parser-and-invalid-rng",
            [
                "Thought:ok\nAction:p a n i c",
                "xxxxx",
                "solar</s>",
                "solar </s>",
                "Action:robin Action:panic",
                "r o b o t",
            ],
        )
        oracle = reference_actions({"game": "wordle", "index": index}, data)
        compare("wordle", index, "oracle", oracle)
        compare(
            "wordle",
            index,
            "success-on-sixth-attempt",
            ["xxxxx"] * (6 - len(oracle)) + oracle,
        )
        compare(
            "wordle",
            index,
            "goal-after-sixth-attempt-disallowed",
            ["xxxxx"] * (7 - len(oracle)) + oracle,
        )
        compare("wordle", index, "repeat-valid-outside-candidates", ["panic"] * 6)

    # Exercise original server's internal Maze budget independently of external15 cap.
    maze_server = upstream[0]()
    with contextlib.redirect_stdout(io.StringIO()):
        eid = maze_server.create()["id"]
        maze_server.reset(eid, 0)
    boundary = [maze_server.step(eid, "invalid") for _ in range(51)]
    assert all(row["reward"] == 0 and not row["done"] for row in boundary[:50])
    assert boundary[-1] == {"observation": "Failure\n", "reward": -1.0, "done": True}
    summary = {
        "validation": "deterministic upstream differential replay (not model parity)",
        "python": sys.version,
        "upstream_agentgym_commit": "3ef9235d23e68e7c2920c5422ad957dc8ced5c6c",
        "upstream_lmrl_commit": "83abeedb3a461c3d7d20572b73318a259d85f2ac",
        "pinned_source_files_verified": len(SOURCE_SHA256),
        "packaged_asset_hashes_verified": True,
        "adapter_only_game_field_verified_separately": True,
        "tasks": 50,
        "scenarios": len(results),
        "state_comparisons": comparisons,
        "oracle_successes": sum(
            x["reward"] == 1 for x in results if x["scenario"] == "oracle"
        ),
        "original_maze_51st_step_failure_verified": True,
        "scenarios_detail": results,
    }
    if args.output:
        args.output.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k != "scenarios_detail"}, indent=2
        )
    )


if __name__ == "__main__":
    main()
