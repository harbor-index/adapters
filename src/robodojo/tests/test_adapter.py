"""CPU-only checks of RoboDojo task packaging and generation boundaries."""

import asyncio
import json
from pathlib import Path
import re
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from robodojo.adapter import RoboDojoAdapter
from robodojo.catalog import DIMENSION_TASKS, UPSTREAM_REVISION


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.assets = self.directory / "Robot Assets with spaces"
        self.assets.mkdir()
        self.output = self.directory / "tasks"

    def generate(self, **options):
        return RoboDojoAdapter(
            self.output, assets_dir=self.assets, **options,
        ).run()

    @staticmethod
    def snapshot(directory):
        return {
            str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()
        }

    def test_task_published_after_preflight_survives_without_overwrite(self):
        first = RoboDojoAdapter(self.output, mock=True, seed=1)
        second = RoboDojoAdapter(self.output, mock=True, seed=2)
        fill = second._fill_mock

        def publish_first(staging):
            first.run()
            fill(staging)

        with patch.object(second, "_fill_mock", publish_first), self.assertRaises(OSError):
            second.run()
        spec = self.output / "mock-reach-targets" / "tests" / "task_spec.json"
        self.assertEqual(json.loads(spec.read_text())["seed"], 1)

    def test_full_generation_preserves_canonical_counts_and_episode_protocol(self):
        tasks = self.generate(seed=2)
        self.assertEqual(len(tasks), 42)
        self.assertEqual(
            {dimension: len(names) for dimension, names in DIMENSION_TASKS.items()},
            {"generalization": 12, "memory": 6, "precision": 8,
             "long-horizon": 8, "open": 8},
        )
        required = {
            "task.toml", "instruction.md", "environment/Dockerfile",
            "environment/simulator.Dockerfile", "environment/bridge_client.py",
            "environment/docker-compose.yaml", "environment/simulator/task_spec.json",
            "environment/simulator/robodojo/catalog.py", "environment/simulator/robodojo/runtime.py",
            "environment/simulator/robodojo/direct/broker.py",
            "environment/simulator/robodojo/direct/contract.py",
            "environment/simulator/robodojo/direct/episode.py",
            "environment/simulator/robodojo/direct/kinematics.py",
            "environment/simulator/robodojo/direct/supervisor.py",
            "environment/simulator/robodojo/direct/GPT_AS_POLICY_LICENSE",
            "environment/simulator/robodojo/direct/policy/deploy.py",
            "environment/simulator/robodojo/direct/policy/deploy.yml",
            "environment/simulator/robodojo/direct/policy/model.py",
            "environment/simulator/robodojo/direct/policy/setup_eval_policy_server.sh",
            "solution/solve.sh", "tests/test.sh", "tests/Dockerfile",
            "tests/task_spec.json", "tests/robodojo/__init__.py",
            "tests/robodojo/catalog.py", "tests/robodojo/runtime.py", "tests/robodojo/verifier.py",
        }
        names, runtime_variants, total_episodes = set(), 0, 0
        for task in tasks:
            with self.subTest(task=task.name):
                self.assertTrue(required <= set(self.snapshot(task)))
                config = tomllib.loads((task / "task.toml").read_text())
                name = config["task"]["name"]
                self.assertRegex(name, r"^robodojo/[a-z0-9]+(?:-[a-z0-9]+)*$")
                self.assertNotIn(name, names)
                names.add(name)
                self.assertEqual(config["version"], "1.0")
                self.assertTrue(config["task"]["authors"])
                # Harbor applies [environment] to the agent container, which is small.
                self.assertEqual(
                    {key: config["environment"][key] for key in ("cpus", "memory_mb", "gpus")},
                    {"cpus": 1, "memory_mb": 2048, "gpus": 0},
                )
                self.assertEqual(
                    {key: value for key, value in config["metadata"].items() if key.startswith("simulator_")},
                    {"simulator_cpus": 8, "simulator_memory_mb": 65536,
                     "simulator_storage_mb": 307200, "simulator_gpus": 1},
                )
                # The verifier grades collected simulator files in its own small container.
                self.assertEqual(config["verifier"]["environment_mode"], "separate")
                self.assertEqual(config["verifier"]["environment"]["gpus"], 0)
                self.assertEqual(config["artifacts"], [
                    {"source": "/harbor-robodojo/results", "service": "simulator"},
                ])
                self.assertGreater(config["agent"]["timeout_sec"], config["verifier"]["timeout_sec"])
                spec = json.loads((task / "tests/task_spec.json").read_text())
                self.assertEqual(
                    json.loads((task / "environment/simulator/task_spec.json").read_text()), spec,
                )
                self.assertEqual(config["metadata"]["upstream_task_id"], spec["task_id"])
                self.assertEqual(spec["upstream_revision"], UPSTREAM_REVISION)
                self.assertEqual(spec["seed"], 2)
                self.assertEqual(config["metadata"]["seed"], 2)
                self.assertEqual(config["metadata"]["dimension"], spec["dimension"])
                if spec["dimension"] == "generalization":
                    self.assertEqual(spec["variants"], [
                        {"name": spec["task_id"], "episodes": 25},
                        {"name": spec["task_id"] + "_random", "episodes": 25},
                    ])
                else:
                    self.assertEqual(spec["variants"], [
                        {"name": spec["task_id"], "episodes": 50},
                    ])
                runtime_variants += len(spec["variants"])
                total_episodes += sum(variant["episodes"] for variant in spec["variants"])
                self.assertTrue((task / "tests/test.sh").stat().st_mode & 0o111)
                self.assertTrue((task / "solution/solve.sh").stat().st_mode & 0o111)
        self.assertEqual(runtime_variants, 54)
        self.assertEqual(total_episodes, 2100)

    def test_generation_is_byte_stable_across_output_directories(self):
        self.generate(task_ids=["push_T", "play_Xylophone", "insert_tubes"])
        second = self.directory / "another-output"
        RoboDojoAdapter(
            second, assets_dir=self.assets,
            task_ids=["insert_tubes", "push_T", "play_Xylophone"],
        ).run()
        self.assertEqual(self.snapshot(self.output), self.snapshot(second))

    def test_selection_is_applied_before_limit_in_stable_order(self):
        tasks = self.generate(task_ids=["swap_T", "push_T"], limit=1)
        self.assertEqual([task.name for task in tasks], ["push-t"])
        spec = json.loads((tasks[0] / "tests/task_spec.json").read_text())
        self.assertEqual(spec["task_id"], "push_T")

    def test_unknown_and_random_variant_ids_are_rejected_before_writing(self):
        for task_id in ("unknown", "stack_bowls_random", "../insert_tubes"):
            with self.subTest(task_id=task_id):
                with self.assertRaisesRegex(ValueError, "Unknown canonical task IDs"):
                    self.generate(task_ids=["align_blocks", task_id], limit=1)
                self.assertFalse(self.output.exists())

    def test_invalid_limit_seed_and_image_inputs_are_rejected(self):
        cases = [
            {"limit": 0}, {"limit": -1}, {"limit": True}, {"limit": 1.5},
            {"seed": -1}, {"seed": True}, {"seed": 1.5},
            {"image": ""}, {"image": "robot image"},
            {"image": "robot\nRUN touch /tmp/injected"},
            {"image": "robot;touch/tmp/injected"}, {"image": "$(touch /tmp/injected)"},
        ]
        for options in cases:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.generate(**options)
                self.assertFalse(self.output.exists())

    def test_existing_task_collision_does_not_modify_any_selected_task(self):
        self.generate(task_ids=["insert_tubes"])
        marker = self.output / "insert-tubes" / "keep.txt"
        marker.write_text("preserve existing task")
        before = self.snapshot(self.output)
        with self.assertRaises(FileExistsError):
            self.generate(task_ids=["align_blocks", "insert_tubes"])
        self.assertEqual(self.snapshot(self.output), before)
        self.assertFalse((self.output / "align-blocks").exists())

    def test_overwrite_only_replaces_selected_task(self):
        self.generate(task_ids=["align_blocks", "insert_tubes"])
        selected_marker = self.output / "align-blocks" / "old.txt"
        selected_marker.write_text("replace selected task")
        other_marker = self.output / "insert-tubes" / "keep.txt"
        other_marker.write_text("keep unselected task")
        self.generate(task_ids=["align_blocks"], overwrite=True, seed=1)
        self.assertFalse(selected_marker.exists())
        self.assertEqual(other_marker.read_text(), "keep unselected task")
        spec = json.loads((self.output / "align-blocks/tests/task_spec.json").read_text())
        self.assertEqual(spec["seed"], 1)

    def test_overwrite_refuses_symbolic_link_without_touching_destination(self):
        outside = self.directory / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("do not remove")
        self.output.mkdir()
        target = self.output / "insert-tubes"
        target.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            self.generate(task_ids=["insert_tubes"], overwrite=True)
        self.assertTrue(target.is_symlink())
        self.assertEqual(marker.read_text(), "do not remove")

    def test_assets_mounts_preserve_spaces_and_are_read_only(self):
        task = self.generate(task_ids=["insert_tubes"])[0]
        compose = json.loads((task / "environment/docker-compose.yaml").read_text())
        self.assertEqual(set(compose["services"]), {"main", "simulator"})
        # The agent container gets no assets, no GPU and no published port.
        self.assertEqual(compose["services"]["main"], {"platform": "linux/amd64"})
        service = compose["services"]["simulator"]
        self.assertNotIn("ports", service)
        self.assertEqual(service["build"], {"context": "${CONTEXT_DIR}", "dockerfile": "simulator.Dockerfile"})
        self.assertEqual(service["platform"], "linux/amd64")
        self.assertEqual(
            {mount["target"] for mount in service["volumes"]},
            {"/workspace/RoboDojo/Assets", str(self.assets)},
        )
        for mount in service["volumes"]:
            self.assertEqual(mount["source"], str(self.assets))
            self.assertTrue(mount["read_only"])
            self.assertFalse(mount["bind"]["create_host_path"])
        devices = service["deploy"]["resources"]["reservations"]["devices"]
        self.assertEqual(devices, [
            {"driver": "nvidia", "count": 1, "capabilities": ["gpu"]},
        ])
        self.assertEqual((service["cpus"], service["mem_limit"]), (8.0, "65536m"))

    def test_missing_or_interpolated_assets_paths_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            RoboDojoAdapter(self.output, assets_dir=self.directory / "missing")
        for name in ("Assets$HOME", "Assets\nnext-line"):
            path = self.directory / name
            path.mkdir()
            with self.subTest(path=name):
                with self.assertRaisesRegex(ValueError, "interpolation or newlines"):
                    RoboDojoAdapter(self.output, assets_dir=path)

    def test_rendered_templates_keep_shell_expressions_and_no_adapter_placeholders(self):
        task = self.generate(task_ids=["play_Xylophone"])[0]
        dockerfile = (task / "environment/simulator.Dockerfile").read_text()
        self.assertIn("FROM robodojo-harbor:f57e1633ffed\n", dockerfile)
        self.assertIn('"$(cat /workspace/RoboDojo/.harbor-upstream-revision)"', dockerfile)
        self.assertIn(UPSTREAM_REVISION, dockerfile)
        self.assertIn('CMD ["python", "-m", "robodojo.direct.supervisor"]', dockerfile)
        main = (task / "environment/Dockerfile").read_text()
        self.assertIn("COPY bridge_client.py /opt/robodojo/bridge_client.py", main)
        self.assertNotIn("robodojo-harbor", main)
        self.assertIn("COPY . /tests", (task / "tests/Dockerfile").read_text())
        for relative in (
            "task.toml", "instruction.md", "environment/Dockerfile", "environment/simulator.Dockerfile",
        ):
            text = (task / relative).read_text()
            self.assertFalse(re.search(
                r"\$(?:task_id|harbor_name|dimension|seed|upstream_revision|image)\b", text,
            ), relative)

    def test_agent_container_code_uses_only_the_standard_library(self):
        task = self.generate(task_ids=["insert_tubes"])[0]
        allowed = {
            "__future__", "argparse", "json", "os", "pathlib", "shutil", "sys", "urllib",
        }
        imported = set(re.findall(
            r"^(?:import|from) ([a-z_]+)", (task / "environment/bridge_client.py").read_text(), re.MULTILINE,
        ))
        self.assertTrue(imported <= allowed, imported - allowed)
        # The verifier never imports simulator-side or agent-side modules.
        self.assertFalse((task / "tests/robodojo/direct").exists())

    def test_generated_tasks_parse_with_harbor(self):
        try:
            from harbor.models.task.config import TaskConfig
        except ImportError:
            self.skipTest("Harbor is not installed")
        for task in self.generate(task_ids=["insert_tubes", "stack_bowls"]):
            config = TaskConfig.model_validate_toml((task / "task.toml").read_text())
            self.assertEqual(config.verifier.environment_mode.value, "separate")
            self.assertEqual(config.artifacts[0].service, "simulator")
            self.assertEqual(config.environment.gpus, 0)

    def test_harbor_resource_overlay_keeps_simulator_limits_on_the_simulator(self):
        """Harbor writes the [environment] resources onto ``main`` only.

        The effective configuration is the generated Compose file plus the
        overlay that Harbor's Docker environment really writes for this task.
        """
        try:
            from harbor.environments.docker.docker import DockerEnvironment
            from harbor.models.task.config import TaskConfig
            from harbor.models.trial.paths import TrialPaths
        except ImportError:
            self.skipTest("Harbor is not installed")
        task = self.generate(task_ids=["insert_tubes"])[0]
        config = TaskConfig.model_validate_toml((task / "task.toml").read_text())
        trial = TrialPaths(trial_dir=self.directory / "trial")
        trial.mkdir()
        environment = DockerEnvironment(
            environment_dir=task / "environment", environment_name="robodojo-test",
            session_id="session", trial_paths=trial, task_env_config=config.environment,
        )
        self.addCleanup(environment._cleanup_resources_compose_file)
        overlay = json.loads(asyncio.run(environment._write_resources_compose_file()).read_text())
        # Harbor cannot move the simulator's limits to the agent container...
        self.assertEqual(overlay, {"services": {"main": {"cpus": 1.0, "mem_limit": "2048m"}}})
        # ...and merging its overlay leaves the simulator service as generated.
        services = json.loads((task / "environment/docker-compose.yaml").read_text())["services"]
        effective = {**services, "main": {**services["main"], **overlay["services"]["main"]}}
        self.assertEqual(effective["main"], {"platform": "linux/amd64", "cpus": 1.0, "mem_limit": "2048m"})
        simulator = effective["simulator"]
        self.assertEqual((simulator["cpus"], simulator["mem_limit"]), (8.0, "65536m"))
        self.assertEqual(simulator["deploy"]["resources"]["reservations"]["devices"], [
            {"driver": "nvidia", "count": 1, "capabilities": ["gpu"]},
        ])


if __name__ == "__main__":
    unittest.main()
