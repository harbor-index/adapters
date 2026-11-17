#!/usr/bin/env python3
"""Run the official validator against the submitted project and private tests."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

OUTPUT = Path('/logs/verifier')
REPOSITORY = Path('/opt/gamedevbench')
sys.path.insert(0, str(REPOSITORY))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify():
    from gamedevbench.src.benchmark_runner import GodotBenchmarkRunner

    binding = json.loads(Path('/tests/payload/binding.json').read_text())
    report = {**binding, 'passed': False, 'confinement': 'strict',
              'kernel_release': os.uname().release, 'compatibility_actions': []}
    report['outer_security'] = {
        line.split(':', 1)[0]: line.split(':', 1)[1].strip()
        for line in Path('/proc/self/status').read_text().splitlines()
        if line.startswith(('Uid:', 'Gid:', 'CapEff:', 'CapPrm:', 'CapBnd:', 'NoNewPrivs:', 'Seccomp:'))}
    try:
        report['apparmor_profile'] = Path('/proc/self/attr/current').read_text().strip()
    except OSError:
        report['apparmor_profile'] = None
    started = time.monotonic()
    try:
        task = binding['task_id']
        report['expected_task_id'] = os.environ.get('GAMEDEVBENCH_TASK_ID')
        if report['expected_task_id'] != task:
            raise RuntimeError('Private verifier belongs to a different task')
        project = REPOSITORY / 'tasks' / task
        if project.exists():
            raise RuntimeError('Validation project exists before collection')
        GodotBenchmarkRunner._assert_safe_solver_workspace(Path('/workspace'))
        shutil.copytree('/workspace', project)
        for relative, expected in binding['verifier_hashes'].items():
            source = Path('/tests/payload/official') / relative
            if digest(source) != expected:
                raise RuntimeError('Official verifier checksum mismatch: ' + relative)
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        config = json.loads(Path('/tests/payload/original_task_config.json').read_text())
        if binding['requires_display'] != bool(config.get('requires_display')):
            config['requires_display'] = binding['requires_display']
            report['compatibility_actions'].append({'kind': 'display_default', 'requires_display': True})
        (project / 'task_config.json').write_text(json.dumps(config, indent=2) + '\n')
        aliases = {'task_0051': ('scenes/Main.tscn', 'scenes/main.tscn'),
                   'task_0052': ('scenes/player.tscn', 'scenes/Player.tscn')}
        if task in aliases:
            alias, canonical = [project / x for x in aliases[task]]
            if not alias.exists() and canonical.is_file():
                alias.symlink_to(canonical.name)
                report['compatibility_actions'].append({'kind': 'case_alias', 'alias': str(alias.relative_to(project)),
                                                         'canonical': str(canonical.relative_to(project))})
        run_name = 'harbor-' + task
        (REPOSITORY / 'results' / run_name).mkdir(parents=True, exist_ok=True)
        runner = GodotBenchmarkRunner(use_gt=False, debug=True, run_name=run_name, confinement='strict')
        result = runner.validate_task(task)
        report['official_result'] = result.to_dict()
        report['passed'] = result.success is True
        report['verifier_hashes_after'] = {p: digest(project / p) for p in binding['verifier_hashes']}
        if report['verifier_hashes_after'] != binding['verifier_hashes']:
            raise RuntimeError('Official verifier bytes changed during validation')
    except Exception as error:
        report['passed'] = False
        report['exception'] = {'type': type(error).__name__, 'message': str(error)}
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    (OUTPUT / 'official_result.json').write_text(json.dumps(report, indent=2) + '\n')
    (OUTPUT / 'reward.txt').write_text('1\n' if report['passed'] else '0\n')
    print(json.dumps(report), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with (OUTPUT / 'official_validation.log').open('w') as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            code = verify()
    print((OUTPUT / 'official_validation.log').read_text())
    raise SystemExit(code)
