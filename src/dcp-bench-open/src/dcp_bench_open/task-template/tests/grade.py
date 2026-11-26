"""Thin wrapper over the unchanged native evaluator; never import candidate code."""
import json
from pathlib import Path
import subprocess
import sys
from evaluation import evaluate


def main():
    case = json.loads(Path('/tests/case.json').read_text())
    image_id = subprocess.check_output(['docker', 'image', 'inspect', 'dcp-eval/cpmpy_python:v1', '--format', '{{.Id}}'], text=True).strip()
    if image_id != case['solver_image_id']:
        raise RuntimeError(f'Native solver image identity mismatch: {image_id}')
    result = evaluate('/workspace/model.py', case['problem'], 'cpmpy_python',
        instance_ids=case['instance_ids'], solution_limit=case['solution_limit'],
        execution_timeout=case['execution_timeout'], reference_timeout=case['reference_timeout'],
        memory_mb=case['memory_mb'], cpus=case['cpus'])
    Path('/logs/verifier/native-result.json').write_text(json.dumps(result, indent=2) + '\n')
    Path('/logs/verifier/case.json').write_text(json.dumps(case, indent=2) + '\n')
    if case['control'] != 'correct':
        # Diagnostics only: native aggregate early-stop and task reward remain
        # unchanged; isolated per-instance calls expose later control outcomes.
        diagnostics = []
        for instance_id in case['instance_ids']:
            diagnostics.append(evaluate('/workspace/model.py', case['problem'], 'cpmpy_python',
                instance_ids=[instance_id], solution_limit=case['solution_limit'],
                execution_timeout=case['execution_timeout'], reference_timeout=case['reference_timeout'],
                memory_mb=case['memory_mb'], cpus=case['cpus']))
        Path('/logs/verifier/native-instance-results.json').write_text(json.dumps(diagnostics, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    Path('/logs/verifier/reward.txt').write_text('1\n' if result['accepted'] else '0\n')
    return 0 if result['accepted'] else 1


if __name__ == '__main__':
    sys.exit(main())
