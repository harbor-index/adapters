import importlib.util
import json
from pathlib import Path
import unittest

from agenteval_games.adapter import DATA
from agenteval_games.runtime.engine import Session, reference_actions

SCRIPTS = Path(__file__).parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParityToolTests(unittest.TestCase):
    def test_sem_overlap_and_comparison(self):
        compare = load("parity_compare")
        # The worked example in the adapter guide.
        mean, sem = compare.mean_sem([44.0, 45.5, 46.1])
        self.assertEqual(("%.1f" % mean, "%.4f" % sem), ("45.2", "0.6245"))
        self.assertTrue(compare.ranges_overlap([44.0, 46.1], [43.8, 45.6]))
        self.assertFalse(compare.ranges_overlap([1.0, 2.0], [3.0, 4.0]))
        won = {"agenteval-maze-00": 1.0, "agenteval-wordle-00": 1.0}
        lost = {"agenteval-maze-00": 1.0, "agenteval-wordle-00": 0.0}
        result = compare.compare([won, won], [won, lost])
        rows = {row["subset"]: row for row in result["metrics"]}
        self.assertEqual(rows["all"]["harbor_runs"], [100.0, 50.0])
        self.assertEqual(rows["all"]["harbor"], "75.00 ± 25.00")
        self.assertEqual(rows["wordle"]["original"], "100.00 ± 0.00")
        self.assertTrue(rows["all"]["ranges_overlap"])
        self.assertEqual(
            list(result["tasks_with_differing_rewards"]), ["agenteval-wordle-00"]
        )
        with self.assertRaises(ValueError):
            compare.compare([won], [{"agenteval-maze-00": 1.0}])

    def test_cross_replay_detects_a_changed_reply(self):
        replay = load("cross_replay")
        for task_id in ("agenteval-maze-03", "agenteval-wordle-07"):
            game, index = replay.task_of(task_id)
            config = {"game": game, "index": index}
            session = Session(config, DATA)
            attempts = [
                {"action": action, "result": session.step(action)}
                for action in reference_actions(config, DATA)
            ]
            episode = {
                "status": "completed",
                "initial": session.initial,
                "attempts": json.loads(json.dumps(attempts)),
            }
            actions, states = replay.recorded(episode)
            replayed = replay.through_adapter(task_id, actions)
            self.assertEqual(replay.differences(task_id, states, replayed), [])
            actions[0] = "Action: not a real action"
            changed = replay.through_adapter(task_id, actions)
            self.assertTrue(replay.differences(task_id, states, changed))


if __name__ == "__main__":
    unittest.main()
