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


if __name__ == '__main__':
    unittest.main()
