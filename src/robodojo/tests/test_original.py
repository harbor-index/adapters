"""The original-side driver: the same player without Harbor.

The CPU mock stands in for the simulator and a local fake for the model API.
The checks show that the two sides of a parity run execute the same loop; they
say nothing about any RoboDojo task.
"""

import asyncio
import contextlib
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from harbor.models.agent.context import AgentContext

from robodojo import original
from robodojo.catalog import mock_task_spec
from robodojo.direct.player import PLAYER_OPTIONS, DirectPlayer
from robodojo.direct_agent import DirectAgent
from robodojo.mock import fake_openai, local, service
from robodojo.verifier import verify

KEY_VARIABLE = "ROBODOJO_FAKE_OPENAI_KEY"
SOURCE = Path(__file__).parents[1] / "src"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class OriginalSideTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.spec_path = self.directory / "task_spec.json"
        self.spec_path.write_text(json.dumps(mock_task_spec(seed=3, episodes=2)))
        self.fake = fake_openai.FakeOpenAI("fake-key-for-parity-test")
        self.server = fake_openai.make_server(self.fake, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.api_base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        environment = mock.patch.dict("os.environ", {KEY_VARIABLE: "fake-key-for-parity-test"})
        environment.start()
        self.addCleanup(environment.stop)

    def simulator(self, name):
        """One fresh mock simulator; returns its bridge address and results directory."""
        port, results = free_port(), self.directory / name
        threading.Thread(
            target=service.evaluate, args=(self.spec_path, results),
            kwargs={"host": "127.0.0.1", "port": port}, daemon=True,
        ).start()
        url = f"http://127.0.0.1:{port}"
        probe = local.LocalEnvironment(url, self.directory / f"{name}-probe")
        deadline = time.monotonic() + 30
        while probe.call("status").get("status") == "transport_error":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        return url, results

    def drive(self, url, results, output, *extra):
        printed, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(errors):
            code = original.main([
                "--model", "openai/fake-vision-model", "--api-base", self.api_base,
                "--api-key-env", KEY_VARIABLE, "--api-retries", "0", "--wait-sec", "30",
                "--bridge-url", url, "--spec", str(self.spec_path), "--results-dir", str(results),
                "--output", str(output), *extra,
            ])
        return code, printed.getvalue(), errors.getvalue()

    @staticmethod
    def decisions(path):
        """What the model was asked to decide and what it answered, without run-specific identifiers."""
        records = []
        for line in path.read_text().splitlines():
            record = json.loads(line)
            if "arguments" in record:
                arguments = json.loads(record["arguments"])
                arguments.pop("request_id")
                records.append((record["episode_index"], record["step_id"], record["error"], arguments))
        return records

    def test_both_sides_run_the_same_loop_on_the_mock(self):
        # Harbor side: the Harbor agent, with the agent container replaced.
        url, harbor_results = self.simulator("harbor-results")
        agent = DirectAgent(
            logs_dir=self.directory / "harbor-agent", model_name="openai/fake-vision-model",
            api_base=self.api_base, api_key_env=KEY_VARIABLE, api_retries=0, wait_sec=30,
        )
        context = AgentContext()
        environment = local.LocalEnvironment(url, self.directory / "harbor-bridge")

        async def play():
            await agent.setup(environment)
            await agent.run("unused", environment, context)

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(play())
        harbor_reward = verify(self.spec_path, harbor_results, self.directory / "harbor-verifier")
        harbor_calls = len(self.fake.requests)

        # Original side: the driver, with no Harbor object involved.
        url, original_results = self.simulator("original-results")
        output = self.directory / "original"
        code, printed, errors = self.drive(url, original_results, output)
        self.assertEqual(code, 0, errors)
        report = json.loads((output / "original_side.json").read_text())
        self.assertEqual(json.loads(printed), report)

        self.assertEqual(report["runner"], "original")
        # Identical provenance and counters: model, prompt, contract, settings, endpoint, tokens.
        self.assertEqual(report["direct"], context.metadata["robodojo_direct"])
        self.assertEqual(json.loads((output / "robodojo_direct" / "run.json").read_text()), report["direct"])
        self.assertEqual(len(self.fake.requests), 2 * harbor_calls)
        self.assertEqual(
            self.decisions(output / "robodojo_direct" / "decisions.jsonl"),
            self.decisions(self.directory / "harbor-agent" / "robodojo_direct" / "decisions.jsonl"),
        )
        # The same grading, and the native totals as the simulator wrote them.
        self.assertEqual(report["evaluation"]["rewards"], harbor_reward)
        self.assertIs(report["evaluation"]["mock"], True)
        native = json.loads((original_results / "native" / "mock_reach_targets" / "_result.json").read_text())
        self.assertEqual(
            report["native"],
            {"mock_reach_targets": {key: native[key] for key in ("eval_time", "success_rate", "score")}},
        )
        self.assertNotIn("fake-key-for-parity-test", json.dumps(report))

    def test_a_failed_run_has_no_result(self):
        url, results = self.simulator("results")
        with mock.patch.dict("os.environ", {KEY_VARIABLE: "wrong-key"}):
            code, printed, errors = self.drive(url, results, self.directory / "refused")
        self.assertEqual(code, 1)
        self.assertNotIn('"runner"', printed)  # No report; the client library prints its own notice.
        self.assertIn("has no result", errors)
        self.assertFalse((self.directory / "refused" / "original_side.json").exists())
        # The simulator was told to stop, so its run cannot be graded later either.
        self.assertTrue((results / "broker" / "abort.json").exists())
        # A bridge that never answers is a failure, not an empty result.
        code, _, errors = self.drive(
            f"http://127.0.0.1:{free_port()}", results, self.directory / "unreachable",
            "--setup-timeout-sec", "0",
        )
        self.assertEqual(code, 1)
        self.assertIn("did not become reachable", errors)

    def test_finished_bridge_and_crossed_results_cannot_earn_a_score(self):
        url_a, results_a = self.simulator("a")
        valid = self.directory / "played-a"
        self.assertEqual(self.drive(url_a, results_a, valid)[0], 0)
        calls = len(self.fake.requests)
        url_b, results_b = self.simulator("b")

        for name, bridge, results, reason in (
            ("replay", url_a, results_a, "live attempt"),
            ("crossed", url_b, results_a, "different attempts"),
        ):
            with self.subTest(name=name):
                out = self.directory / name
                code, _, error = self.drive(bridge, results, out)
                self.assertEqual(code, 1)
                self.assertIn(reason, error)
                self.assertEqual(len(self.fake.requests), calls)
                self.assertFalse((out / "original_side.json").exists())
                self.assertFalse((out / "verifier" / "reward.json").exists())

        # A rejected mismatch leaves both the old evidence and the fresh bridge usable.
        out_b = self.directory / "played-b"
        self.assertEqual(self.drive(url_b, results_b, out_b)[0], 0)
        mismatch = self.directory / "swapped" / "robodojo_direct"
        mismatch.mkdir(parents=True)
        shutil.copy(out_b / "robodojo_direct" / "attempt.json", mismatch)
        shutil.copy(valid / "robodojo_direct" / "decisions.jsonl", mismatch)
        with self.assertRaisesRegex(original.EvaluationError, "actions or episode IDs"):
            original.grade(self.spec_path, results_b, mismatch.parent)
        self.assertFalse((mismatch.parent / "verifier" / "reward.json").exists())
        verdict_path = results_b / "supervisor.json"
        verdict = json.loads(verdict_path.read_text())
        verdict_path.write_text(json.dumps({**verdict, "attempt_id": "different-attempt"}))
        with self.assertRaisesRegex(original.EvaluationError, "Supervisor verdict"):
            original.grade(self.spec_path, results_b, out_b)

    def test_command_line_is_checked_before_anything_runs(self):
        for arguments in (
            ["--spec", str(self.spec_path)],  # Without its results directory.
            ["--tool-choice", "none"], ["--image-history", "0"],
        ):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                original.main(["--model", "openai/x", "--output", str(self.directory / "unused"), *arguments])
        occupied = self.directory / "occupied"
        occupied.mkdir()
        (occupied / "file").write_text("x")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            original.main(["--model", "openai/x", "--output", str(occupied)])


class SharedPlayerTests(unittest.TestCase):
    def test_both_sides_expose_every_player_option(self):
        """A new player option must be reachable from the driver and from Harbor."""
        self.assertEqual(
            set(original.OPTIONS) | {"save_images", "completion_fn", "cost_fn"}, set(PLAYER_OPTIONS),
        )
        with tempfile.TemporaryDirectory() as directory:
            agent = DirectAgent(
                logs_dir=Path(directory), model_name="fake/model", image_history=2, history_window=3,
                max_cost_usd=1.5, save_images=True,
            )
            player = DirectPlayer("fake/model", Path(directory) / "player", image_history=2, history_window=3,
                                  max_cost_usd=1.5)
            self.assertIsInstance(agent.player, DirectPlayer)
            self.assertEqual(agent.player.settings, player.settings)
            self.assertEqual((agent.player.max_cost_usd, agent.player.save_images), (1.5, True))
            self.assertEqual(agent.provenance(), player.provenance())

    def test_the_driver_does_not_import_harbor(self):
        program = (
            "import sys; sys.path.insert(0, sys.argv[1]); import robodojo.original; "
            "loaded = sorted(name for name in sys.modules if name.split('.')[0] in ('harbor', 'litellm')); "
            "raise SystemExit(', '.join(loaded) or 0)"
        )
        done = subprocess.run([sys.executable, "-c", program, str(SOURCE)], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()
