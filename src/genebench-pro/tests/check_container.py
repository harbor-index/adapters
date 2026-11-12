"""Explicit Docker checks; build the supplied image before running this script."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile


def docker(image, user, script, mounts=(), environment=()):
    command = ["docker", "run", "--rm", "--network", "none", "--user", user, "-i"]
    for source, target, readonly in mounts:
        command += ["--mount", f"type=bind,src={source.resolve()},dst={target}" + (",readonly" if readonly else "")]
    for name, value in environment:
        command += ["--env", f"{name}={value}"]
    command += ["--entrypoint", "/usr/local/bin/python", image, "-I", "-"]
    result = subprocess.run(command, input=script, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"Container check failed ({result.returncode}): {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--tasks", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    tasks = args.tasks.resolve()
    base = tasks / "wf_selection"
    gold = json.loads((base / "solution/eval_answer.json").read_text())
    wrong = deepcopy(gold)
    wrong["answer"]["selected_locus"] = "incorrect"
    inside = deepcopy(gold)
    inside["answer"]["s"] += 0.01
    outside = deepcopy(gold)
    outside["answer"]["s"] += 0.03
    extra = deepcopy(gold)
    extra["answer"]["extra"] = 1
    strict = tasks / "structural_inversion_subhap_expression_risk"
    strict_extra = json.loads((strict / "solution/eval_answer.json").read_text())
    strict_extra["answer"]["extra"] = 1
    boolean = deepcopy(strict_extra)
    del boolean["answer"]["extra"]
    boolean["answer"]["target_support_code"] = True
    cases = [
        ("gold", base, gold, True), ("missing", base, None, False),
        ("malformed_json", base, "{", False), ("non_object", base, [], False),
        ("wrong_exact_value", base, wrong, False),
        ("inside_numeric_tolerance", base, inside, True),
        ("outside_numeric_tolerance", base, outside, False),
        ("extra_key_permitted", base, extra, True),
        ("strict_extra_key", strict, strict_extra, False),
        ("boolean_as_integer", strict, boolean, False),
    ]
    receipts = []
    with tempfile.TemporaryDirectory(prefix="genebench-container-checks-") as temporary:
        root = Path(temporary)
        for name, task, submission, expected in cases:
            workspace, logs = root / name / "workspace", root / name / "logs"
            workspace.mkdir(parents=True)
            logs.mkdir()
            answer = workspace / "eval_answer.json"
            answer.unlink(missing_ok=True)
            if submission is not None:
                answer.write_text(submission if isinstance(submission, str) else json.dumps(submission))
            # Both a stale success and a symlink must be discarded by the verifier.
            sentinel = workspace / "preserve.txt"
            sentinel.write_text("keep")
            reward = logs / "reward.txt"
            reward.unlink(missing_ok=True)
            reward.symlink_to("/workspace/preserve.txt")
            (logs / "reward.json").write_text('{"reward": 1}')
            result = docker(args.image, "root", """
import json
from pathlib import Path
import subprocess
run = subprocess.run(['/bin/bash', '/tests/test.sh'], capture_output=True, text=True)
print(json.dumps({'exit_code': run.returncode,
    'reward': int(Path('/logs/verifier/reward.txt').read_text()),
    'diagnostic': json.loads(Path('/logs/verifier/result.json').read_text())}))
""", [(task / "tests", "/tests", True), (workspace, "/workspace", False), (logs, "/logs/verifier", False)],
                [("PATH", "/workspace:/usr/local/bin:/usr/bin:/bin"), ("PYTHONPATH", "/workspace")])
            if result["reward"] != int(expected) or result["exit_code"] != (0 if expected else 1):
                raise AssertionError(f"{name}: {result}")
            if sentinel.read_text() != "keep" or (logs / "reward.json").exists():
                raise AssertionError(f"{name}: stale reward or symlink was trusted")
            receipts.append({"case": name, "expected_passed": expected, **result})

    permissions = docker(args.image, "analyst", """
import json, os
from pathlib import Path
assert os.getuid() != 0
assert not Path('/solution').exists()
assert not Path('/workspace/eval_config.json').exists()
assert not Path('/workspace/reference_grader.py').exists()
assert not os.access('/tests', os.R_OK | os.W_OK | os.X_OK)
for target in ['/usr/local/bin/python', '/usr/local/lib/python3.12', '/tests/verify.py']:
    assert not os.access(target, os.W_OK), target
try:
    with open('/usr/local/bin/python', 'a'):
        raise AssertionError('interpreter is writable')
except PermissionError:
    pass
assert not Path('/usr/share/licenses/genebench-pro').exists()
assert Path('/usr/share/licenses/dataset/LICENSE').is_file()
Path('/workspace/eval_answer.json').write_text('{}')
print(json.dumps({'agent_uid': os.getuid(), 'interpreter_write_denied': True,
    'tests_inaccessible': True, 'hidden_payloads_absent': True,
    'workspace_writable': True, 'neutral_license_path': True}))
""")
    environment = docker(args.image, "analyst", """
from importlib.metadata import version
import json, platform, subprocess
from pathlib import Path
import numpy as np
import pandas as pd
files = list(Path('/inputs').glob('*/environment/data_files/**/*'))
files = [path for path in files if path.is_file()]
for path in files:
    if path.suffix == '.npz':
        with np.load(path, allow_pickle=False) as archive:
            for key in archive.files:
                archive[key]
    else:
        pd.read_csv(path, sep=',' if '.csv' in path.name else '\\t')
packages = ['numpy', 'pandas', 'scipy', 'scikit-learn', 'statsmodels', 'lifelines',
    'matplotlib', 'seaborn', 'openpyxl', 'pysam', 'bed-reader']
r = subprocess.check_output(['Rscript', '-e',
    'library(MASS); library(Matrix); library(survival); cat(as.character(getRversion()))'], text=True)
print(json.dumps({'python': platform.python_version(), 'r': r,
    'packages': {package: version(package) for package in packages},
    'input_files_read': len(files)}))
""", [(tasks, "/inputs", True)])
    expected_inputs = sum(len(json.loads((task / "tests/eval_config.json").read_text())["data_files"])
        for task in tasks.iterdir() if (task / "task.toml").is_file())
    if environment["input_files_read"] != expected_inputs:
        raise AssertionError("Not all generated inputs were read")
    environment["hidden_payloads_absent"] = permissions["hidden_payloads_absent"]
    # Match Harbor's late Docker copy: macOS bind mounts do not enforce the
    # container filesystem's ownership rules and cannot validate this boundary.
    container = subprocess.check_output([
        "docker", "create", "--network", "none", "--entrypoint", "/bin/sleep",
        args.image, "infinity",
    ], text=True).strip()
    try:
        subprocess.run(["docker", "start", container], capture_output=True, check=True)
        subprocess.run(["docker", "cp", f"{base / 'tests'}/.", f"{container}:/tests"], capture_output=True, check=True)
        result = subprocess.run([
            "docker", "exec", "--user", "analyst", container, "/usr/local/bin/python", "-I", "-c", """
import json, os
from pathlib import Path
assert os.getuid() != 0
try:
    Path('/tests/eval_config.json').read_text()
except PermissionError:
    print(json.dumps({'uploaded_config_read_denied': True}))
else:
    raise AssertionError('uploaded config is readable by the agent')
"""], capture_output=True, text=True, check=True)
        permissions.update(json.loads(result.stdout))
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=True)
    # A shadow interpreter and modules on agent-controlled search paths must not run.
    with tempfile.TemporaryDirectory(prefix="genebench-shadow-check-") as temporary:
        root = Path(temporary)
        workspace, logs = root / "workspace", root / "logs"
        workspace.mkdir()
        logs.mkdir()
        (workspace / "eval_answer.json").write_text(json.dumps(wrong))
        (workspace / "python").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
        (workspace / "python").chmod(0o755)
        for module in ("json", "pathlib", "reference_grader", "sitecustomize"):
            (workspace / f"{module}.py").write_text("raise RuntimeError('agent import executed')\n")
        result = docker(args.image, "root", """
import json, subprocess
from pathlib import Path
run = subprocess.run(['/bin/bash', '/tests/test.sh'], capture_output=True)
assert run.returncode == 1
assert Path('/logs/verifier/reward.txt').read_text().strip() == '0'
assert not json.loads(Path('/logs/verifier/result.json').read_text())['passed']
print(json.dumps({'path_shadow_ignored': True, 'pythonpath_shadow_ignored': True,
    'incorrect_answer_reward': 0}))
""", [(base / "tests", "/tests", True), (workspace, "/workspace", False), (logs, "/logs/verifier", False)],
            [("PATH", "/workspace:/usr/local/bin:/usr/bin:/bin"), ("PYTHONPATH", "/workspace")])
        permissions.update(result)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "verifier-checks.json").write_text(json.dumps(receipts, indent=2) + "\n")
    (args.output_dir / "hardening-checks.json").write_text(json.dumps(permissions, indent=2) + "\n")
    (args.output_dir / "environment-checks.json").write_text(json.dumps(environment, indent=2) + "\n")
    print(json.dumps({"verifier_cases_passed": len(receipts), **permissions}, indent=2))


if __name__ == "__main__":
    main()
