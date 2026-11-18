"""Checks of the broker state machine, its HTTP surface and the bridge client."""

import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
import zlib

from robodojo.direct import bridge_client
from robodojo.direct.broker import Broker, BrokerConflict, build_observation, encode_png, make_server

from helpers import FakeEnv, hold

PNG = encode_png(2, 1, bytes([255, 0, 0, 0, 255, 0]))


def meta(layout_id=0, variant="insert_tubes"):
    return {
        "variant": variant, "layout_id": layout_id, "layout_seed_group": 0,
        "max_episode_steps": 10, "control_dt": 0.04,
    }


def observation(step_id=0):
    return build_observation(FakeEnv().get_obs(), step_id=step_id, max_episode_steps=10)


def executed(request_id, steps=5, start=0):
    return {
        "request_id": request_id, "requested_steps": steps, "executed_steps": steps,
        "start_step_id": start, "end_step_id": start + steps, "ik_diagnostics": [],
    }


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.results = Path(self.temporary.name) / "results"
        self.broker = Broker(self.results, poll_interval=0.01)

    def open_request(self, layout_id=0):
        episode = self.broker.begin_episode(meta(layout_id))
        posted = self.broker.post_observation(episode["episode_id"], observation(), {"cam_head": PNG})
        return episode["episode_id"], posted["request_id"]

    def ledger(self, name):
        return [json.loads(line) for line in (self.results / "broker" / name).read_text().splitlines()]

    def test_png_encoder_writes_a_valid_lossless_image(self):
        self.assertTrue(PNG.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height, depth, color = struct.unpack("!IIBB", PNG[16:26])
        self.assertEqual((width, height, depth, color), (2, 1, 8, 2))
        start = PNG.index(b"IDAT") + 4
        length = struct.unpack("!I", PNG[start - 8:start - 4])[0]
        self.assertEqual(zlib.decompress(PNG[start:start + length]), b"\x00" + bytes([255, 0, 0, 0, 255, 0]))
        with self.assertRaises(ValueError):
            encode_png(2, 2, b"\x00" * 6)

    def test_observation_passes_native_state_through(self):
        value = observation(step_id=3)
        self.assertEqual(value["instruction"], "Insert the tubes into the rack.")
        self.assertEqual((value["step_id"], value["remaining_steps"]), (3, 7))
        self.assertEqual(value["current_eef"]["right"]["quaternion_wxyz"], [1.0, 0.0, 0.0, 0.0])
        self.assertAlmostEqual(value["current_eef"]["right"]["position"][1], -0.2, places=6)
        self.assertEqual(len(value["joint_state"]["left"]["arm_joints_rad"]), 6)
        native = FakeEnv().get_obs()
        native["state"]["left_ee_pose"] = native["state"]["left_ee_pose"][:6]
        with self.assertRaises(ValueError):
            build_observation(native, step_id=0, max_episode_steps=10)

    def test_decision_is_delivered_exactly_once(self):
        episode_id, request_id = self.open_request()
        self.assertEqual(self.broker.poll_decision(request_id), {"status": "pending"})
        opened = self.broker.observe(0)["observation"]
        self.assertEqual(opened["request_id"], request_id)
        # The wait elapses before the simulator picks the decision up.
        self.assertEqual(self.broker.act(hold(opened), 0)["status"], "executing")
        delivered = self.broker.poll_decision(request_id)
        self.assertEqual(delivered["status"], "decision")
        self.assertEqual(delivered["response"]["request_id"], request_id)
        with self.assertRaisesRegex(BrokerConflict, "already delivered"):
            self.broker.poll_decision(request_id)
        with self.assertRaisesRegex(BrokerConflict, "No observation"):
            self.broker.act(hold(opened), 0)
        second = self.broker.post_observation(
            episode_id, observation(5), {"cam_head": PNG}, executed(request_id),
        )
        self.assertNotEqual(second["request_id"], request_id)
        stale = self.broker.act(hold(opened), 0)
        self.assertEqual(stale["status"], "rejected")
        self.assertIn("stale", stale["error"])
        events = [entry["event"] for entry in self.ledger("decisions/0000.jsonl")]
        self.assertEqual(events, ["accepted", "executed", "rejected"])

    def test_rejected_decision_executes_nothing_and_can_be_corrected(self):
        _, request_id = self.open_request()
        opened = self.broker.observe(0)["observation"]
        far = hold(opened)
        far["target"]["left"]["position"][0] += 0.2
        rejected = self.broker.act(far, 0)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.broker.poll_decision(request_id), {"status": "pending"})
        self.assertEqual(self.broker.act(hold(opened), 0)["status"], "executing")
        self.assertEqual(self.broker.poll_decision(request_id)["status"], "decision")

    def test_episode_end_is_reported_and_recorded(self):
        episode_id, request_id = self.open_request(layout_id=7)
        opened = self.broker.observe(0)["observation"]
        outcome = {}
        actor = threading.Thread(target=lambda: outcome.update(self.broker.act(hold(opened), 5)))
        actor.start()
        while self.broker.status()["state"] != "decided":
            pass
        self.broker.poll_decision(request_id)
        self.broker.end_episode(episode_id, {
            "control_steps": 5, "native_success_observed": True,
            "previous_result": executed(request_id),
        })
        actor.join(5)
        self.assertEqual(outcome["status"], "episode_end")
        record = self.ledger("episodes.jsonl")[0]
        self.assertEqual(
            (record["layout_id"], record["decisions"], record["executed_steps"], record["control_steps"]),
            (7, 1, 5, 5),
        )
        self.assertEqual(record["ended_by"], "native_termination")
        self.assertIs(record["native_success_observed"], True)
        self.assertEqual(self.broker.status()["state"], "idle")
        self.assertEqual(self.broker.observe(0)["status"], "waiting")

    def test_execution_result_must_match_the_delivered_decision(self):
        episode_id, request_id = self.open_request()
        opened = self.broker.observe(0)["observation"]
        self.broker.act(hold(opened), 0)
        self.broker.poll_decision(request_id)
        with self.assertRaisesRegex(BrokerConflict, "does not match"):
            self.broker.post_observation(episode_id, observation(5), {"cam_head": PNG}, None)
        with self.assertRaisesRegex(BrokerConflict, "does not match"):
            self.broker.post_observation(
                episode_id, observation(5), {"cam_head": PNG}, executed("another-request"),
            )
        with self.assertRaisesRegex(BrokerConflict, "open episode"):
            self.broker.end_episode("another-episode", {"control_steps": 5, "native_success_observed": False})

    def test_native_exception_closes_the_episode_without_native_termination(self):
        episode_id, _ = self.open_request(layout_id=3)
        self.broker.abandon_episode(episode_id, "PhysXBrokenError")
        record = self.ledger("episodes.jsonl")[0]
        self.assertEqual(record["ended_by"], "native_exception")
        self.assertNotIn("native_success_observed", record)
        self.assertEqual(self.broker.status()["state"], "idle")
        self.broker.begin_episode(meta(4))

    def test_abort_stops_the_simulator_side_and_is_recorded(self):
        _, request_id = self.open_request()
        self.assertEqual(self.broker.abort("spending limit reached")["status"], "aborted")
        self.assertEqual(self.broker.poll_decision(request_id)["status"], "abort")
        self.assertEqual(self.broker.observe(0)["status"], "aborted")
        self.assertTrue((self.results / "broker" / "abort.json").is_file())
        with self.assertRaisesRegex(BrokerConflict, "aborted"):
            self.broker.act({}, 0)

    def test_supervisor_verdict_ends_the_run_for_the_agent(self):
        self.assertEqual(self.broker.observe(0)["status"], "waiting")
        (self.results / "supervisor.json").write_text('{"status": "completed"}')
        self.assertEqual(self.broker.observe(0)["status"], "finished")
        (self.results / "supervisor.json").write_text('{"status": "failed", "error": "simulator crashed"}')
        failed = self.broker.observe(0)
        self.assertEqual((failed["status"], failed["error"]), ("failed", "simulator crashed"))

    def test_existing_ledger_is_never_continued(self):
        episode_id, _ = self.open_request()
        self.broker.abandon_episode(episode_id, "stop")
        with self.assertRaisesRegex(RuntimeError, "refusing to continue"):
            Broker(self.results)

    def test_wait_is_bounded(self):
        for wait in (-1, 3601, "5", True, None):
            with self.subTest(wait=wait), self.assertRaises(ValueError):
                self.broker.observe(wait)


class BridgeTests(unittest.TestCase):
    """Drive the real HTTP server through the real command-line client."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.broker = Broker(self.directory / "results", poll_interval=0.01)
        self.server = make_server(self.broker, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def client(self, *arguments):
        lines = []
        code = bridge_client.main(
            ["--url", self.url, "--out", str(self.directory / "out"), *arguments], write=lines.append,
        )
        return code, json.loads(lines[-1])

    def test_observation_is_saved_with_one_png_per_camera(self):
        self.assertEqual(self.client("status")[1]["state"], "idle")
        self.assertEqual(self.client("observe", "--wait", "0")[1]["status"], "waiting")
        episode = self.broker.begin_episode(meta())
        self.broker.post_observation(
            episode["episode_id"], observation(), {"cam_head": PNG, "cam_left_wrist": PNG},
        )
        code, result = self.client("observe", "--wait", "1")
        self.assertEqual((code, result["status"]), (0, "observation"))
        directory = Path(result["observation_dir"])
        self.assertEqual(
            sorted(path.name for path in directory.iterdir()),
            ["cam_head.png", "cam_left_wrist.png", "observation.json"],
        )
        self.assertEqual((directory / "cam_head.png").read_bytes(), PNG)
        saved = json.loads((directory / "observation.json").read_text())
        self.assertEqual(saved["request_id"], result["observation"]["request_id"])

        far = hold(saved)
        far["target"]["right"]["position"][2] += 1.0
        code, rejected = self.client("act", "--wait", "0", "--response", json.dumps(far))
        self.assertEqual((code, rejected["status"]), (0, "rejected"))
        code, accepted = self.client("act", "--wait", "0", "--response", json.dumps(hold(saved)))
        self.assertEqual((code, accepted["status"]), (0, "executing"))
        # A second action for the same observation is a definite refusal.
        code, conflict = self.client("act", "--wait", "0", "--response", json.dumps(hold(saved)))
        self.assertEqual((code, conflict["status"]), (0, "conflict"))

    def test_abort_and_bad_requests(self):
        code, result = self.client("abort", "--reason", "decision limit reached")
        self.assertEqual((code, result["status"]), (0, "aborted"))
        code, result = self.client("observe", "--wait", "99999")
        self.assertEqual((code, result["status"]), (0, "bad_request"))

    def test_unreachable_bridge_is_a_transport_error(self):
        self.server.shutdown()
        self.server.server_close()
        code, result = self.client("status")
        self.assertEqual((code, result["status"]), (3, "transport_error"))


if __name__ == "__main__":
    unittest.main()
