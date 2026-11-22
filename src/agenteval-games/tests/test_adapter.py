from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
import urllib.error
import urllib.request
from agenteval_games.adapter import AgentEvalGamesAdapter, DATA, records
from agenteval_games.runtime.engine import Session, parse_action, reference_actions
from agenteval_games.runtime.server import GameServer
from agenteval_games.runtime.verification import verify


class EngineTests(unittest.TestCase):
    def test_all_reference_episodes_win_and_cannot_restart(self):
        for task in records():
            config = {"game": task["game"], "index": task["index"]}
            with self.subTest(config=config):
                session = Session(config, DATA)
                for action in reference_actions(config, DATA):
                    result = session.step(action)
                self.assertEqual(result["reward"], 1)
                self.assertTrue(result["done"])
                with self.assertRaises(ValueError):
                    session.step("move down")

    def test_wordle_invalid_guesses_consume_six_attempts_without_reward(self):
        session = Session({"game": "wordle", "index": 0}, DATA)
        for i in range(6):
            response = session.step("INVALID")
            self.assertEqual(response["observation"], "invalid word")
            self.assertEqual(response["reward"], 0)
            self.assertEqual(response["done"], i == 5)
        self.assertTrue(session.done)

    def test_maze_external_cap_is_not_internal_terminal(self):
        session = Session({"game": "maze", "index": 0}, DATA)
        for _ in range(15):
            response = session.step("move up")
        self.assertTrue(response["episode_finished"])
        self.assertFalse(response["done"])
        self.assertEqual(response["reward"], 0)
        self.assertEqual(response["turns_remaining"], 0)

    def test_original_parser_edge_cases(self):
        self.assertEqual(parse_action("Thought: hi\nAction: move down\n"), "move down")
        self.assertEqual(parse_action("move down</s>"), "move dow")
        self.assertEqual(parse_action("Action:move leftAction:move right"), "move left")
        self.assertEqual(parse_action("MOVE UP"), "MOVE UP")


class GeneratorTests(unittest.TestCase):
    def test_all_tasks_reproducible_and_valid(self):
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            a = AgentEvalGamesAdapter(first).run()
            b = AgentEvalGamesAdapter(second).run()
            self.assertEqual(len(a), 50)
            self.assertEqual(
                len(
                    {
                        tomllib.loads((p / "task.toml").read_text())["task"]["name"]
                        for p in a
                    }
                ),
                50,
            )
            for left, right in zip(a, b):
                self.assertEqual(left.name, right.name)
                files = [p.relative_to(left) for p in left.rglob("*") if p.is_file()]
                for file in files:
                    self.assertEqual(
                        (left / file).read_bytes(), (right / file).read_bytes()
                    )
                main = (left / "environment/Dockerfile").read_text()
                self.assertNotIn("COPY data", main)
                self.assertNotIn("COPY config", main)
                self.assertNotIn("COPY runtime", main)
                compose = (left / "environment/docker-compose.yaml").read_text()
                self.assertNotIn("volumes", compose)
                self.assertNotIn("docker.sock", compose)
                task = tomllib.loads((left / "task.toml").read_text())
                self.assertEqual(task["verifier"]["environment_mode"], "separate")
                self.assertEqual(
                    task["artifacts"],
                    [{"source": "/game-evidence/transcript.json", "service": "game"}],
                )
                verifier = (left / "tests/Dockerfile").read_text()
                self.assertIn("COPY . /tests/", verifier)
                self.assertTrue((left / "tests/runtime/verification.py").is_file())

    def test_selection_validation_and_overwrite(self):
        with tempfile.TemporaryDirectory() as out:
            adapter = AgentEvalGamesAdapter(
                out, task_ids=["lmrlgym_wordle_24", "agenteval-maze-00"]
            )
            self.assertEqual(
                [p.name for p in adapter.run()],
                ["agenteval-maze-00", "agenteval-wordle-24"],
            )
            with self.assertRaises(FileExistsError):
                adapter.run()
            self.assertEqual(
                len(AgentEvalGamesAdapter(out, limit=1, overwrite=True).run()), 1
            )
            with self.assertRaises(ValueError):
                AgentEvalGamesAdapter(out, task_ids=["typo"]).run()
            with self.assertRaises(ValueError):
                AgentEvalGamesAdapter(out, limit=0).run()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"game": "wordle", "index": 0}
        self.server = GameServer(
            ("127.0.0.1", 0), Session(self.config, DATA), self.temp.name
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.evidence = Path(self.temp.name) / "transcript.json"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, path, body=None):
        req = urllib.request.Request(
            self.url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def transcript(self):
        return json.loads(self.evidence.read_text())

    def test_authoritative_trace_is_persisted_and_replayed(self):
        initial = self.request("/observation")
        self.assertEqual(initial["game"], "wordle")
        self.assertNotIn("index", initial)
        self.assertEqual(self.request("/observation"), initial)
        actions = reference_actions(self.config, DATA)
        for turn, action in enumerate(actions, 1):
            result = self.request("/step", {"action": action})
            # Each turn is on disk by the time the agent sees its result.
            recorded = self.transcript()["turns"]
            self.assertEqual(len(recorded), turn)
            self.assertEqual(recorded[-1], {"action": action, "result": result})
        transcript = self.transcript()
        self.assertEqual(verify(transcript, self.config, DATA), 1)
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/step", {"action": "again"})
        self.assertEqual(transcript, self.transcript())
        self.assertEqual(
            sorted(p.name for p in self.evidence.parent.iterdir()), ["transcript.json"]
        )
        with self.assertRaises(ValueError):
            verify(transcript, {"game": "wordle", "index": 1}, DATA)

    def test_replay_rejects_edited_transcripts(self):
        actions = reference_actions(self.config, DATA)
        for action in actions[:-1]:
            self.request("/step", {"action": action})
        losing = self.transcript()
        self.assertEqual(verify(losing, self.config, DATA), 0)
        # Claiming a win without the winning move does not survive replay.
        claimed = deepcopy(losing)
        claimed["turns"][-1]["result"].update(reward=1, done=True)
        claimed["final"].update(reward=1, done=True)
        with self.assertRaises(ValueError):
            verify(claimed, self.config, DATA)
        swapped = deepcopy(losing)
        swapped["turns"][0]["action"] = "cheat"
        with self.assertRaises(ValueError):
            verify(swapped, self.config, DATA)
        padded = deepcopy(losing)
        padded["turns"] = padded["turns"] * 9
        with self.assertRaises(ValueError):
            verify(padded, self.config, DATA)
        for malformed in ([], {"version": 1}, {**losing, "version": 2}):
            with self.assertRaises(ValueError):
                verify(malformed, self.config, DATA)
        with self.assertRaises(ValueError):
            verify({**losing, "turns": ["move"]}, self.config, DATA)

    def test_no_reset_secret_or_transport_cheat(self):
        for path in (
            "/reset",
            "/create",
            "/config",
            "/filtered_vocab",
            "/transcript",
            "/game-evidence/transcript.json",
        ):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path, {"action": "hello"})
            self.assertEqual(error.exception.code, 404)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/step", {"action": [], "reward": 1})
        transcript = self.transcript()
        self.assertEqual(len(transcript["turns"]), 0)
        self.assertEqual(verify(transcript, self.config, DATA), 0)
        self.assertNotIn("seed", json.dumps(transcript))


class VerifierEntrypointTests(unittest.TestCase):
    """Run a generated task's tests/verify.py as the verifier image does."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = {"game": "maze", "index": 0}
        (task,) = AgentEvalGamesAdapter(
            root / "tasks", task_ids=["agenteval-maze-00"]
        ).run()
        self.script = task / "tests/verify.py"
        self.evidence = root / "transcript.json"
        self.logs = root / "logs"

    def tearDown(self):
        self.temp.cleanup()

    def grade(self, transcript=None):
        if transcript is not None:
            self.evidence.write_text(json.dumps(transcript))
        result = subprocess.run(
            [sys.executable, "-I", self.script, self.evidence, self.logs],
            capture_output=True,
            text=True,
        )
        return result.returncode, (self.logs / "reward.txt").read_text()

    def play(self, actions):
        session = Session(self.config, DATA)
        for action in actions:
            session.step(action)
        return session.transcript()

    def test_reference_scores_one_and_wrong_submissions_score_zero(self):
        reference = reference_actions(self.config, DATA)
        self.assertEqual(self.grade(self.play(reference)), (0, "1\n"))
        self.assertEqual(self.grade(self.play([])), (0, "0\n"))
        self.assertEqual(self.grade(self.play(["move up"] * 15)), (0, "0\n"))

    def test_missing_or_invalid_evidence_fails_closed(self):
        code, reward = self.grade()
        self.assertNotEqual(code, 0)
        self.assertEqual(reward, "0\n")
        forged = self.play(["move up"] * 15)
        forged["final"].update(reward=1, done=True)
        code, reward = self.grade(forged)
        self.assertNotEqual(code, 0)
        self.assertEqual(reward, "0\n")
        # A valid winning transcript for another task is not accepted here.
        other = {"game": "maze", "index": 1}
        session = Session(other, DATA)
        for action in reference_actions(other, DATA):
            session.step(action)
        code, reward = self.grade(session.transcript())
        self.assertNotEqual(code, 0)
        self.assertEqual(reward, "0\n")


if __name__ == "__main__":
    unittest.main()
