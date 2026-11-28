from collections import Counter
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

import yaml

from loopsbench.adapter import LoopsBenchAdapter, Resources
from loopsbench import verifier_network as network


class VerifierNetworkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def make_inputs(self, task_id, client=None, tester=None):
        source = self.root / task_id
        source.mkdir()
        (source / "task.yaml").write_text("instruction: test\n", encoding="utf-8")
        compose = {
            "services": {
                "client": {"build": {"context": "."}, **(client or {})},
                "tester": {"image": "task-client", **(tester or {})},
            },
        }
        (source / "docker-compose.yaml").write_text(yaml.safe_dump(compose), encoding="utf-8")
        (source / "Dockerfile").write_text("FROM scratch\nCOPY gold_patches/ /gold/\n", encoding="utf-8")
        (source / "tests").mkdir()
        (source / "tests/run.py").write_bytes(b"original tests")
        (source / "solution.sh").write_bytes(b"original solution")
        (source / "gold_patches").mkdir()
        (source / "gold_patches/a.diff").write_bytes(b"original reference patch")

        harbor = self.root / (task_id + "-generated")
        harbor.mkdir()
        environment = harbor / "environment"
        environment.mkdir()
        (environment / "Dockerfile").write_text(
            "FROM scratch\nCOPY .harbor-build-inputs/base/ /workspace/\n", encoding="utf-8"
        )
        (environment / "Dockerfile.dockerignore").write_bytes(b".harbor-build-inputs/base/.git\n")
        inputs = environment / ".harbor-build-inputs/base"
        inputs.mkdir(parents=True)
        (inputs / "input").write_bytes(b"binary\x00content")
        (inputs / "link").symlink_to("input")
        (harbor / "task.toml").write_text(self.render_harbor_task(task_id), encoding="utf-8")
        return source, harbor

    def render_harbor_task(self, task_id):
        LoopsBenchAdapter(
            self.root, self.root, resources=Resources(cpus=13, memory_mb=12288)
        )._write_task_toml(
            self.root / task_id, self.root, {}
        )
        template = (self.root / "task.toml").read_text(encoding="utf-8")
        for key, value in network.harbor_network_values(task_id).items():
            template = template.replace("{" + key + "}", value)
        return template

    def test_every_reviewed_task_renders_a_restricted_harbor_verifier(self):
        modes = Counter()
        for task_id in network._POLICIES:
            with self.subTest(task=task_id):
                task = tomllib.loads(self.render_harbor_task(task_id))
                verifier = task["verifier"]
                self.assertEqual(verifier["environment_mode"], "separate")
                modes[verifier["network_mode"]] += 1
                self.assertEqual("allowed_hosts" in verifier, verifier["network_mode"] == "allowlist")
                self.assertEqual(task["agent"]["network_mode"], "allowlist")
                self.assertEqual(task["agent"]["allowed_hosts"], [])
                self.assertEqual(task["environment"]["network_mode"], "public")
        self.assertEqual(modes, {"allowlist": 69, "no-network": 35})

    def test_matching_harbor_and_upstream_policies(self):
        cases = {
            "task_net_os_labs": {"network_mode": "no-network"},
            **{
                task: {
                    "network_mode": "allowlist",
                    "allowed_hosts": ["files.pythonhosted.org", "pypi.org"],
                }
                for task in ("task_panda3d_collision_hard", "task_panda3d_pgraph_medium")
            },
            "task_db_nanodb": {
                "network_mode": "allowlist",
                "allowed_hosts": ["repo.maven.apache.org", "repo1.maven.org"],
            },
            "task_ns3_network_simulation": {
                "network_mode": "allowlist",
                "allowed_hosts": [
                    "astral.sh", "files.pythonhosted.org", "github.com", "mirrors.aliyun.com",
                    "pypi.org", "release-assets.githubusercontent.com", "releases.astral.sh",
                ],
            },
        }
        for task_id, expected in cases.items():
            with self.subTest(task=task_id):
                source, harbor = self.make_inputs(task_id)
                run = network.prepare_upstream(source, harbor, self.root / (task_id + "-compare"))
                config = yaml.safe_load(run.read_text())
                self.assertEqual(config["task_ids"], [task_id])
                self.assertEqual(config["agent"], "oracle")
                self.assertEqual(config["network"]["agent"], {"network_mode": "no-network"})
                self.assertEqual(config["network"]["verifier"], expected)
                verifier = tomllib.loads((harbor / "task.toml").read_text())["verifier"]
                self.assertEqual({key: verifier[key] for key in expected}, expected)

    def test_prepare_cli_preserves_native_files_and_uses_exact_build_inputs(self):
        source, harbor = self.make_inputs("task_db_nanodb")
        source_compose = (source / "docker-compose.yaml").read_bytes()
        output = self.root / "compare"
        command = [
            sys.executable, "-m", "loopsbench.verifier_network", "prepare-upstream",
            "--source-task", str(source), "--harbor-task", str(harbor),
            "--output-dir", str(output),
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=True)
        run = output / "run.yaml"
        self.assertEqual(result.stdout.strip(), str(run))
        config = yaml.safe_load(run.read_text())
        self.assertEqual(config["docker_image_strategy"], "local-build")
        tasks = Path(config["dataset_path"])
        self.assertEqual([task.name for task in tasks.iterdir()], [source.name])
        staged = tasks / source.name

        self.assertEqual((source / "docker-compose.yaml").read_bytes(), source_compose)
        compose = yaml.safe_load((staged / "docker-compose.yaml").read_text())
        original = yaml.safe_load(source_compose)
        build = compose["services"]["client"].pop("build")
        original["services"]["client"].pop("build")
        for service in compose["services"].values():
            self.assertEqual(service.pop("cpus"), 13)
            self.assertEqual(service.pop("mem_limit"), "12288m")
        self.assertEqual(compose, original)
        self.assertEqual(build["dockerfile"], "Dockerfile")
        local_context = (staged / build["context"]).resolve()
        self.assertTrue(local_context.is_relative_to(staged))
        for relative in [
            "Dockerfile", "Dockerfile.dockerignore", ".harbor-build-inputs/base/input",
        ]:
            self.assertEqual((local_context / relative).read_bytes(),
                             (harbor / "environment" / relative).read_bytes())
        self.assertEqual((local_context / ".harbor-build-inputs/base/link").readlink(), Path("input"))
        self.assertFalse((local_context / "gold_patches").exists())
        for relative in ["task.yaml", "Dockerfile", "tests/run.py", "solution.sh", "gold_patches/a.diff"]:
            self.assertEqual((staged / relative).read_bytes(), (source / relative).read_bytes())

    def test_prepare_rejects_existing_output_and_stale_harbor_config(self):
        source, harbor = self.make_inputs("task_db_nanodb")
        output = self.root / "compare"
        output.mkdir()
        existing = output / "keep"
        existing.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            network.prepare_upstream(source, harbor, output)
        self.assertEqual(existing.read_text(), "keep")
        with self.assertRaisesRegex(ValueError, "outside both input task directories"):
            network.prepare_upstream(source, harbor, source / "nested")
        generated = harbor / "task.toml"
        valid = generated.read_text()
        generated.write_text(valid.replace(f'upstream_task_id = "{source.name}"', 'upstream_task_id = "wrong"'))
        with self.assertRaisesRegex(ValueError, "upstream_task_id"):
            network.prepare_upstream(source, harbor, output)
        generated.write_text(valid.replace(network.harbor_network_values(source.name)["verifier_network"],
                                           'network_mode = "public"'))
        with self.assertRaisesRegex(ValueError, "regenerate the task"):
            network.prepare_upstream(source, harbor, output)
        with self.assertRaisesRegex(ValueError, "No reviewed verifier network policy"):
            network.harbor_network_values("task_unreviewed_example")

    def test_upstream_host_client_is_loopback_only_and_keeps_verifier_allowlist(self):
        task_id = "task_riscv_cpu_verilog_three"
        source, harbor = self.make_inputs(task_id, client={"network_mode": "host"})
        run = network.prepare_upstream(source, harbor, self.root / "compare")
        config = yaml.safe_load(run.read_text())
        compose = yaml.safe_load((Path(config["dataset_path"]) / task_id / "docker-compose.yaml").read_text())
        self.assertEqual(compose["services"]["client"]["network_mode"], "none")
        self.assertEqual(config["agent"], "oracle")
        self.assertEqual(config["network"]["verifier"]["network_mode"], "allowlist")

    def test_prepare_aligns_existing_deploy_limits(self):
        original = {"resources": {"limits": {"cpus": "2", "memory": "4g", "pids": 20}}}
        source, harbor = self.make_inputs("task_db_nanodb", tester={"deploy": original})
        output = self.root / "compare"
        network.prepare_upstream(source, harbor, output)
        staged = output / "tasks" / source.name / "docker-compose.yaml"
        tester = yaml.safe_load(staged.read_text())["services"]["tester"]
        self.assertEqual(tester["deploy"]["resources"]["limits"],
                         {"cpus": "13", "memory": "12288m", "pids": 20})

    def test_prepare_normalizes_only_two_exact_managed_log_bindings(self):
        log_bindings = [
            "${LOOPSBENCH_TASK_LOGS_PATH:-./logs}:${LOOPSBENCH_CONTAINER_LOGS_PATH:-/logs}",
            "${LOOPSBENCH_TASK_AGENT_LOGS_PATH:-./agent_logs}:${LOOPSBENCH_CONTAINER_AGENT_LOGS_PATH:-/agent_logs}",
            "${LOOPSBENCH_TASK_LOGS_PATH:-./custom}:${LOOPSBENCH_CONTAINER_LOGS_PATH:-/logs}",
            "loopsbench_shared:/shared",
            {"type": "bind", "source": "./local", "target": "/local"},
        ]
        source, harbor = self.make_inputs("task_mlir_circuit_ir", client={"volumes": log_bindings})
        source_compose = source / "docker-compose.yaml"
        original = source_compose.read_bytes()
        output = self.root / "compare"
        network.prepare_upstream(source, harbor, output)
        staged = output / "tasks" / source.name / source_compose.name
        self.assertEqual(source_compose.read_bytes(), original)
        self.assertEqual(yaml.safe_load(staged.read_text())["services"]["client"]["volumes"], [
            "${LOOPSBENCH_TASK_LOGS_PATH}:${LOOPSBENCH_CONTAINER_LOGS_PATH}",
            "${LOOPSBENCH_TASK_AGENT_LOGS_PATH}:${LOOPSBENCH_CONTAINER_AGENT_LOGS_PATH}",
            *log_bindings[2:],
        ])

    def test_prepare_handles_released_task_without_compose(self):
        source, harbor = self.make_inputs("task_graphics_raster_2d")
        source_compose = source / "docker-compose.yaml"
        source_compose.unlink()
        output = self.root / "compare"
        network.prepare_upstream(source, harbor, output)
        staged = output / "tasks" / source.name / source_compose.name
        self.assertFalse(source_compose.exists())
        services = yaml.safe_load(staged.read_text())["services"]
        self.assertEqual(services["client"]["build"]["context"], "./.harbor-environment")
        self.assertEqual(services["client"]["image"], services["tester"]["image"])

    def test_upstream_tcp_static_client_and_tester_retain_loopback_capability(self):
        task_id = "task_tcp_course_stack"
        interfaces = {"cap_add": ["NET_ADMIN"], "networks": ["default"]}
        source, harbor = self.make_inputs(task_id, client=interfaces, tester=interfaces)
        run = network.prepare_upstream(source, harbor, self.root / "compare")
        config = yaml.safe_load(run.read_text())
        compose = yaml.safe_load((Path(config["dataset_path"]) / task_id / "docker-compose.yaml").read_text())
        for name in ("client", "tester"):
            service = compose["services"][name]
            self.assertEqual(service["network_mode"], "none")
            self.assertEqual(service["cap_add"], ["NET_ADMIN"])
            self.assertNotIn("networks", service)
        self.assertEqual(config["agent"], "oracle")
        self.assertIn("Compose isolates the TCP tester", run.read_text())

    def test_pinned_dependency_baking_is_idempotent(self):
        for task_id, anchor, expected in [
            (
                "task_riscv_cpu_six_labs",
                "COPY requirements/ /workspace/requirements/",
                "/tmp/loopsbench-venv/bin/pip install --no-cache-dir pytest==8.4.1",
            ),
            (
                "task_sicp_py_scheme_sql",
                "RUN pip install --no-cache-dir pytest",
                "RUN pip install --no-cache-dir pytest==8.4.1",
            ),
        ]:
            with self.subTest(task=task_id):
                original = f"FROM ubuntu:24.04\n{anchor}\n"
                baked = network.bake_verifier_dependencies(task_id, original)
                self.assertIn(expected, baked)
                self.assertEqual(network.bake_verifier_dependencies(task_id, baked), baked)
                with self.assertRaisesRegex(ValueError, "expected one Dockerfile line"):
                    network.bake_verifier_dependencies(task_id, "FROM ubuntu:24.04\n")


if __name__ == "__main__":
    unittest.main()
