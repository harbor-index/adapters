"""Checks of the simulator-container supervisor with mocked native commands."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from robodojo.catalog import UPSTREAM_REVISION, XPOLICYLAB_REVISION
from robodojo.direct import supervisor
from robodojo.runtime import EvaluationError

from helpers import native_result, task_spec_dict

POLICY = Path(__file__).parents[1] / "src" / "robodojo" / "direct" / "policy"


class FakeServer:
    def __init__(self, command, **kwargs):
        self.command, self.kwargs = command, kwargs
        self.pid, self.returncode, self.terminated = 1, None, False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "RoboDojo"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "scripts" / "robodojo.sh").write_text("# mock simulator\n")
        (self.root / "XPolicyLab" / "policy").mkdir(parents=True)
        (self.root / ".harbor-upstream-revision").write_text(UPSTREAM_REVISION)
        (self.root / ".harbor-xpolicylab-revision").write_text(XPOLICYLAB_REVISION)
        self.results = self.directory / "results"
        self.spec_path = self.directory / "task_spec.json"
        self.spec = task_spec_dict()
        self.spec_path.write_text(json.dumps(self.spec))
        self.servers, self.commands = [], []

    def popen(self, command, **kwargs):
        self.servers.append(FakeServer(command, **kwargs))
        return self.servers[-1]

    def result_path(self, command, environment):
        task = command[command.index("--task") + 1]
        return supervisor.native_result_path(self.root, task, self.spec["seed"], environment["ROBODOJO_RUN_ID"])

    def write_result(self, command, kwargs, value):
        path = self.result_path(command, kwargs["env"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) if not isinstance(value, str) else value)
        return path

    def supervise(self, run):
        def recorded(command, **kwargs):
            self.commands.append((command, kwargs))
            return run(command, **kwargs)
        return supervisor.supervise(
            self.spec_path, self.root, self.results, POLICY,
            popen=self.popen, run=recorded, wait=lambda *args: None,
        )

    def test_native_server_and_client_commands_are_unchanged_entry_points(self):
        def run(command, **kwargs):
            kwargs["stdout"].write("native simulator output\n")
            self.write_result(command, kwargs, native_result())
            return subprocess.CompletedProcess(command, 0)

        with patch.dict(os.environ, {"EVAL_NUM": "1", "ROBODOJO_RUN_ID": "old-run"}):
            verdict = self.supervise(run)
        server = self.servers[0]
        policy_dir = self.root / "XPolicyLab" / "policy" / "HarborDirect"
        self.assertEqual(server.command[:3], ["bash", str(self.root / "scripts" / "robodojo.sh"), "server"])
        self.assertEqual(server.command[server.command.index("--policy-dir") + 1], str(policy_dir))
        self.assertEqual(server.command[server.command.index("--bind-host") + 1], "127.0.0.1")
        self.assertEqual(server.kwargs["env"]["HARBOR_ROBODOJO_RESULTS"], str(self.results))
        self.assertFalse(server.terminated)
        for name in ("deploy.py", "deploy.yml", "model.py", "setup_eval_policy_server.sh"):
            self.assertTrue((policy_dir / name).is_file(), name)
        command, kwargs = self.commands[0]
        self.assertEqual(command[2], "client")
        self.assertEqual(command[command.index("--policy-name") + 1], "HarborDirect")
        self.assertEqual(command[command.index("--eval-num") + 1], "native")
        self.assertEqual(command[-4:], ["--ckpt", "direct", "--action-type", "joint"])
        self.assertEqual(kwargs["env"]["EVAL_NUM"], "native")
        self.assertNotEqual(kwargs["env"]["ROBODOJO_RUN_ID"], "old-run")
        self.assertNotIn("shell", kwargs)
        self.assertEqual(verdict["status"], "completed")
        self.assertEqual(verdict["policy_name"], "HarborDirect")
        self.assertEqual(verdict["variants"][0]["episodes"], 50)
        copied = json.loads((self.results / "native" / "insert_tubes" / "_result.json").read_text())
        self.assertEqual(copied, native_result())
        self.assertIn("native simulator output", (self.results / "logs" / "insert_tubes.log").read_text())

    def test_generalization_runs_both_variants_sequentially(self):
        self.spec = task_spec_dict("stack_bowls", "generalization")
        self.spec_path.write_text(json.dumps(self.spec))

        def run(command, **kwargs):
            self.write_result(command, kwargs, native_result(25, 5))
            return subprocess.CompletedProcess(command, 0)

        verdict = self.supervise(run)
        tasks = [command[command.index("--task") + 1] for command, _ in self.commands]
        self.assertEqual(tasks, ["stack_bowls", "stack_bowls_random"])
        self.assertEqual(len({kwargs["env"]["ROBODOJO_RUN_ID"] for _, kwargs in self.commands}), 2)
        self.assertEqual([variant["name"] for variant in verdict["variants"]], tasks)
        self.assertEqual(len(self.servers), 1)

    def test_failures_never_publish_a_native_result(self):
        def stale(command, **kwargs):
            os.utime(self.write_result(command, kwargs, native_result()), ns=(1, 1))
            return subprocess.CompletedProcess(command, 0)

        def duplicate(command, **kwargs):
            path = self.write_result(command, kwargs, native_result())
            (path.parent / "copy").mkdir()
            (path.parent / "copy" / "_result.json").write_text("{}")
            return subprocess.CompletedProcess(command, 0)

        def symlink(command, **kwargs):
            path = self.result_path(command, kwargs["env"])
            path.parent.mkdir(parents=True)
            target = self.directory / "elsewhere.json"
            target.write_text(json.dumps(native_result()))
            path.symlink_to(target)
            return subprocess.CompletedProcess(command, 0)

        def crash(command, **kwargs):
            raise subprocess.CalledProcessError(1, command)

        def timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 1)

        cases = (
            (stale, "stale"), (duplicate, "exactly one native result"), (symlink, "symlinks"),
            (crash, "Native simulator client failed"), (timeout, "Native simulator client failed"),
            (lambda command, **kwargs: subprocess.CompletedProcess(command, 0), "exactly one native result"),
            (lambda command, **kwargs: (self.write_result(command, kwargs, '{"score": NaN}'),
                                        subprocess.CompletedProcess(command, 0))[1], "Non-finite"),
        )
        for index, (run, pattern) in enumerate(cases):
            with self.subTest(pattern=pattern, index=index):
                self.results = self.directory / f"results-{index}"
                policy_dir = self.root / "XPolicyLab" / "policy" / "HarborDirect"
                if policy_dir.exists():
                    for path in sorted(policy_dir.rglob("*"), reverse=True):
                        path.unlink() if path.is_file() else path.rmdir()
                    policy_dir.rmdir()
                with self.assertRaisesRegex(EvaluationError, pattern):
                    self.supervise(run)
                self.assertFalse((self.results / "native").exists())
                # The broker stays reachable so the agent can read the failure.
                self.assertFalse(self.servers[-1].terminated)

    def test_wrong_revisions_and_dirty_state_stop_before_anything_runs(self):
        fail = lambda *args, **kwargs: self.fail("Nothing may run")
        (self.root / ".harbor-upstream-revision").write_text("wrong")
        with self.assertRaisesRegex(EvaluationError, "pinned revision"):
            supervisor.supervise(self.spec_path, self.root, self.results, POLICY, popen=fail, run=fail)
        (self.root / ".harbor-upstream-revision").write_text(UPSTREAM_REVISION)
        (self.root / ".harbor-xpolicylab-revision").write_text("wrong")
        with self.assertRaisesRegex(EvaluationError, "xpolicylab-revision"):
            supervisor.supervise(
                self.spec_path, self.root, self.directory / "results-2", POLICY, popen=fail, run=fail,
            )
        (self.root / ".harbor-xpolicylab-revision").write_text(XPOLICYLAB_REVISION)
        (self.root / "XPolicyLab" / "policy" / "HarborDirect").mkdir()
        with self.assertRaisesRegex(EvaluationError, "already exists"):
            supervisor.supervise(
                self.spec_path, self.root, self.directory / "results-3", POLICY, popen=fail, run=fail,
            )
        dirty = self.directory / "results-4"
        dirty.mkdir()
        (dirty / "supervisor.json").write_text('{"status": "completed"}')
        with self.assertRaisesRegex(EvaluationError, "must be empty"):
            supervisor.supervise(self.spec_path, self.root, dirty, POLICY, popen=fail, run=fail)

    def test_policy_server_exit_is_an_infrastructure_failure(self):
        def run(command, **kwargs):
            self.write_result(command, kwargs, native_result())
            self.servers[-1].returncode = 1
            return subprocess.CompletedProcess(command, 0)

        with self.assertRaisesRegex(EvaluationError, "Policy server exited"):
            self.supervise(run)

    def test_main_records_the_verdict_for_the_agent_and_verifier(self):
        with patch.object(supervisor, "supervise", side_effect=EvaluationError("simulator crashed")):
            code = supervisor.main([
                "--spec", str(self.spec_path), "--root", str(self.root),
                "--results-dir", str(self.results), "--policy-source", str(POLICY), "--exit-when-done",
            ])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads((self.results / "supervisor.json").read_text()),
                         {"status": "failed", "error": "simulator crashed"})


if __name__ == "__main__":
    unittest.main()
