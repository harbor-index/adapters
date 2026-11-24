import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


path = Path(__file__).parents[1] / 'src/gamelogicbench/task-template/tests/verify.py'
spec = importlib.util.spec_from_file_location('glb_verifier', path)
verifier = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verifier
spec.loader.exec_module(verifier)


class VerifierTests(unittest.TestCase):
    def test_controller_project_does_not_receive_frozen_judge_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'workspace'
            frozen = root / 'frozen'
            workspace.mkdir()
            frozen.mkdir()
            (workspace / 'project.godot').write_text('controller project')
            (frozen / 'project.godot').write_text('judge project')
            (frozen / 'assertions.gd').touch()

            worker = verifier.prepare_project(workspace, root / 'worker')
            judge = verifier.prepare_project(workspace, root / 'judge', frozen_judge=frozen)

            self.assertEqual((worker / 'project.godot').read_text(), 'controller project')
            self.assertFalse((worker / 'assertions.gd').exists())
            self.assertEqual((judge / 'project.godot').read_text(), 'judge project')
            self.assertTrue((judge / 'assertions.gd').is_file())

    def test_proxy_interface_does_not_allow_source_injection(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'interface.json'
            uid = path.parent.stat().st_uid
            path.write_text(json.dumps({'methods': [{'name': 'on_tick', 'arguments': 1}], 'signals': []}))
            source = verifier.controller_proxy(path, uid)
            self.assertIn('func on_tick(arg_0):', source)
            self.assertIn('_glb_call("on_tick", [arg_0])', source)
            for data in (
                [{'name': 'x():\n\tOS.execute("sh", [])\nfunc y', 'arguments': 0}],
                [{'name': 'x', 'arguments': True}],
                [{'name': 'x', 'arguments': 17}],
                [{'name': 'x', 'arguments': 0}] * 2,
                {'name': 'x', 'arguments': 0},
            ):
                path.write_text(json.dumps({'methods': data, 'signals': []}))
                with self.subTest(data=data), self.assertRaises(verifier.InvalidSubmission):
                    verifier.controller_proxy(path, uid)

    def test_proxy_signal_interface_preserves_typed_callbacks(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'interface.json'
            uid = path.parent.stat().st_uid
            interface = {'methods': [{'name': 'advance', 'arguments': 1}],
                         'signals': [{'name': 'expired', 'types': [4]}]}
            path.write_text(json.dumps(interface))
            self.assertIn('signal expired(arg_0: String)', verifier.controller_proxy(path, uid))
            for types in ([True], [24], [4] * 5):
                interface['signals'][0]['types'] = types
                path.write_text(json.dumps(interface))
                with self.subTest(types=types), self.assertRaises(verifier.InvalidSubmission):
                    verifier.controller_proxy(path, uid)

    def test_proxy_interface_requires_bounded_owned_regular_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'interface.json'
            path.write_text('x' * (64 * 1024 + 1))
            uid = path.stat().st_uid
            with self.assertRaises(verifier.InvalidSubmission):
                verifier.controller_proxy(path, uid)
            path.write_text('{}')
            with self.assertRaises(verifier.InvalidSubmission):
                verifier.controller_proxy(path, uid + 1)
            path.write_text('not json')
            with self.assertRaises(verifier.InvalidSubmission):
                verifier.controller_proxy(path, uid)

    def test_game_observation_requires_a_bounded_owned_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = root / 'trace.jsonl'
            trace.write_bytes(b'{}\n')
            uid = trace.stat().st_uid
            self.assertEqual(verifier.game_observation(trace, uid), b'{}\n')
            with self.assertRaises(ValueError):
                verifier.game_observation(trace, uid + 1)
            link = root / 'redirect.jsonl'
            link.symlink_to(trace)
            with self.assertRaises(OSError):
                verifier.game_observation(link, uid)
            trace.write_bytes(b'x' * (2 * 1024 * 1024 + 1))
            with self.assertRaises(ValueError):
                verifier.game_observation(trace, uid)

    def test_factory_observation_requires_all_ticks_and_checks_provider_removal(self):
        bridge = path.parents[2] / 'task-bridges/factory_grid.py'
        factory_spec = importlib.util.spec_from_file_location('factory_grid', bridge)
        factory = importlib.util.module_from_spec(factory_spec)
        factory_spec.loader.exec_module(factory)
        world = {
            'budget': 1, 'providers': ['source'], 'cut': 'source', 'surgery': 1,
            'receivers': [], 'turrets': [], 'heat_receivers': [],
        }
        first = {
            'tick': 0, 'supply': 0, 'money': 0, 'stocks': {'source': 2},
            'intake': {}, 'tick_intake': {}, 'fires': {}, 'delivered': {},
            'tick_delivered': {}, 'delivery_events': {}, 'first_delivery': {},
            'crusher': 0, 'foreign': '',
        }
        second = dict(first, tick=1, stocks={})
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'trace.jsonl'
            factory.normalize_trace(f'{json.dumps(first)}\n{json.dumps(second)}'.encode(), destination, world)
            self.assertEqual([json.loads(line) for line in destination.read_text().splitlines()], [first, second])
            for rows in ([first], [first, first], [first, dict(second, stocks=first['stocks'])],
                         [first, dict(second, passed=True)]):
                destination.unlink(missing_ok=True)
                with self.subTest(rows=rows), self.assertRaises(ValueError):
                    data = '\n'.join(json.dumps(row) for row in rows).encode()
                    factory.normalize_trace(data, destination, world)

    def test_full_coverage_and_private_cell_evidence(self):
        manifest = {
            'task_id': 'atom_test', 'mode': 'headless',
            'deliverable_paths': ['res://logic/controller.gd'],
            'cells': [
                {'scenario': 'baseline', 'seed': 1, 'press': '', 'tier': 'public'},
                {'scenario': 'hidden', 'seed': 2, 'press': '', 'tier': 'hidden'},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'judge').mkdir()
            (root / 'judge/judge.tscn').touch()
            (root / 'evaluation.json').write_text(json.dumps(manifest))
            with (
                patch.object(verifier, 'PRIVATE', root),
                patch.object(verifier, 'LOGS', root),
                patch.object(verifier.os, 'geteuid', return_value=0),
                patch.object(verifier, 'validate_workspace'),
                patch.object(verifier.subprocess, 'run') as probe,
                patch.object(verifier, 'evaluate_cell') as evaluate_cell,
            ):
                probe.return_value.stdout = '4.4.stable.official.4c311cbee\n'
                evaluate_cell.side_effect = [
                    verifier.Verdict('pass', True), verifier.Verdict('behavior_fail', False),
                ]
                self.assertEqual(verifier.evaluate(), 0)
                rows = [json.loads(line) for line in (root / 'cells.jsonl').read_text().splitlines()]
                self.assertEqual([(r['seed'], r['passed']) for r in rows], [(1, True), (2, False)])
                self.assertEqual(evaluate_cell.call_count, 2)
                evaluate_cell.side_effect = [verifier.Verdict('pass', True)] * 2
                self.assertEqual(verifier.evaluate(), 1)
                evaluate_cell.side_effect = [verifier.Verdict('infra_error', None)]
                with self.assertRaises(verifier.VerifierError):
                    verifier.evaluate()

    def test_verdict_rejects_symlink_and_inconsistent_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'result.json'
            output.write_text(json.dumps({'pass': True, 'scenario': 'baseline', 'seed': 1}))
            cell = verifier.Cell('baseline', 1, '', 'public')
            self.assertTrue(verifier.read_verdict(output, output.stat().st_uid, cell, 0, False).passed)
            with self.assertRaises(verifier.VerifierError):
                verifier.read_verdict(output, output.stat().st_uid, cell, 1, False)
            link = root / 'link.json'
            link.symlink_to(output)
            with self.assertRaises(verifier.InvalidSubmission):
                verifier.read_verdict(link, output.stat().st_uid, cell, 0, False)

    def test_verifier_error_clears_stale_reward(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reward = root / 'reward.txt'
            reward.write_text('1\n')
            with (
                patch.object(verifier, 'LOGS', root),
                patch.object(verifier, 'evaluate', side_effect=verifier.VerifierError('unusable judge')),
            ):
                self.assertEqual(verifier.main(), 2)
            self.assertFalse(reward.exists())


if __name__ == '__main__':
    unittest.main()
