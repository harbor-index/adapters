import hashlib
import json
from pathlib import Path
import tempfile
import tomllib
import unittest

from codeclash.adapter import CodeClashAdapter
from codeclash.catalog import ARENAS, config_for
from codeclash.configuration import load_native_config
from codeclash.contract import is_diagnostic, validate_task_config


class GenerationTests(unittest.TestCase):
    def test_all_arenas_keep_native_configs_and_stable_task_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            for profile in ("smoke", "benchmark"):
                paths = CodeClashAdapter(Path(tmp) / profile, profile=profile).run()
                self.assertEqual([p.name for p in paths], sorted(ARENAS))
                for task in paths:
                    config = validate_task_config(
                        json.loads((task / "environment/task-config.json").read_text())
                    )
                    self.assertEqual(
                        config["resolved_native_config"], config_for(task.name, profile)
                    )
                    self.assertEqual(config["counting_unit"], "arena")
                    self.assertEqual(
                        tomllib.loads((task / "task.toml").read_text())["task"]["name"],
                        f"codeclash/{task.name}",
                    )
                    self.assertEqual(
                        (task / "tests/task-config.json").read_bytes(),
                        (task / "environment/task-config.json").read_bytes(),
                    )
                    self.assertTrue((task / "tests/contract.py").is_file())
                    self.assertIn(
                        task.name,
                        tomllib.loads((task / "task.toml").read_text())["metadata"][
                            "tags"
                        ],
                    )

    def test_generation_is_reproducible_and_protects_existing_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = CodeClashAdapter(root, profile="smoke", task_ids=["battlesnake"])
            task = adapter.run()[0]

            def digest():
                return {
                    str(p.relative_to(task)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in task.rglob("*")
                    if p.is_file()
                }

            before = digest()
            with self.assertRaises(FileExistsError):
                adapter.run()
            CodeClashAdapter(
                root, profile="smoke", task_ids=["battlesnake"], overwrite=True
            ).run()
            self.assertEqual(before, digest())
            (task / "environment/task-config.json").unlink()
            with self.assertRaisesRegex(ValueError, "not generated"):
                CodeClashAdapter(root, task_ids=["battlesnake"], overwrite=True).run()

    def test_unknown_ids_and_negative_limits_fail_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "output"
            for kwargs in ({"task_ids": ["../escape"]}, {"limit": -1}):
                with self.assertRaises(ValueError):
                    CodeClashAdapter(target, **kwargs).run()
            self.assertFalse(target.exists())
            self.assertEqual(CodeClashAdapter(target, limit=0).run(), [])

    def test_symlink_target_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "safe").mkdir()
            (root / "battlesnake").symlink_to(root / "safe")
            with self.assertRaisesRegex(ValueError, "symlink"):
                CodeClashAdapter(root, overwrite=True).run()
            self.assertTrue((root / "safe").is_dir())

    def test_custom_config_preserves_transparency_and_player_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = config_for("bridge", "benchmark")
            config["tournament"]["transparent"] = True
            config["players"][0]["name"] = "north_agent"
            source = root / "config.json"
            source.write_text(json.dumps(config))
            tasks = CodeClashAdapter(root / "tasks", config=[source]).run()
            self.assertEqual([p.name for p in tasks], ["bridge"])
            manifest = json.loads(
                (tasks[0] / "environment/task-config.json").read_text()
            )
            self.assertEqual(manifest["resolved_native_config"], config)
            self.assertEqual(manifest["config_origin"], "user_supplied")
            self.assertTrue(is_diagnostic(manifest))
            with self.assertRaisesRegex(ValueError, "Multiple configurations"):
                CodeClashAdapter(root / "other", config=[source, source]).run()

    def test_unsupported_workflows_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = config_for("chess", "benchmark")
            for mutation in (
                lambda c: c.update(ladder={}),
                lambda c: c["players"][0].update(branch_init="main"),
                lambda c: c["players"][0].update(push=True),
            ):
                value = json.loads(json.dumps(config))
                mutation(value)
                source = root / "config.json"
                source.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    CodeClashAdapter(root / "tasks", config=[source]).run()
                self.assertFalse((root / "tasks").exists())

    def test_native_yaml_includes_and_merge_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prompt.yaml").write_text('system: "preserve exactly"\n')
            (root / "player.yaml").write_text("name: south\nagent: dummy\n")
            (root / "config.yaml").write_text(
                "prompts: !include prompt.yaml\nplayers:\n  -\n    <<: !include player.yaml\n    name: north\n"
            )
            self.assertEqual(
                load_native_config(root / "config.yaml"),
                {
                    "prompts": {"system": "preserve exactly"},
                    "players": [{"name": "north", "agent": "dummy"}],
                },
            )
            (root / "config.yaml").write_text("x: !include ../outside.yaml\n")
            with self.assertRaisesRegex(ValueError, "escapes"):
                load_native_config(root / "config.yaml")
            (root / "config.yaml").write_text("!include config.yaml\n")
            with self.assertRaisesRegex(ValueError, "Cyclic"):
                load_native_config(root / "config.yaml")
