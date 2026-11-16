"""No-container tests for native config preservation, scoring replay and ownership."""

from copy import deepcopy
from dataclasses import dataclass
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from codeclash.catalog import ARENAS, config_for
from codeclash.runtime.native_runner import (
    build_config,
    initial_summary,
    configure_environment,
    isolate_container,
    edit_source,
    scripted_edit,
    cleanup_containers,
    valid_role,
    validate_owned_inspection,
    RUN_LABEL,
    ROLE_LABEL,
    ISOLATE_REFS_COMMAND,
)
from codeclash.runtime.replay import hash_round_files, install_score_replay, primitive


def request(arena="battlesnake", profile="benchmark", **overrides):
    return {
        "task_config": {
            "profile": profile,
            "resolved_native_config": config_for(arena, profile),
        },
        "model_name": None,
        "opponent_model_name": None,
        "player_models": {},
        **overrides,
    }


class ConfigurationTests(unittest.TestCase):
    def test_ref_isolation_batches_tags_and_symbolic_remotes_keeps_local_branch(self):
        with tempfile.TemporaryDirectory() as tmp:

            def git(*args, **kwargs):
                return subprocess.run(
                    ["git", *args],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                    check=True,
                    **kwargs,
                ).stdout.strip()

            git("init", "-b", "main")
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "initial",
            )
            head = git("rev-parse", "HEAD")
            git(
                "update-ref",
                "--stdin",
                input="".join(f"create refs/tags/tag-{i} {head}\n" for i in range(200)),
            )
            git("update-ref", "refs/remotes/origin/main", head)
            git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
            subprocess.run(
                ["bash", "-c", ISOLATE_REFS_COMMAND],
                cwd=tmp,
                capture_output=True,
                check=True,
            )
            self.assertEqual(git("for-each-ref", "refs/tags", "refs/remotes"), "")
            self.assertEqual(git("rev-parse", "refs/heads/main"), head)

    def test_host_model_credentials_preserved_only_for_benchmark(self):
        credentials = {
            "OPENROUTER_API_KEY": "test-openrouter",
            "AZURE_API_KEY": "test-azure",
            "LLAMA_API_KEY": "test-llama",
            "GITHUB_TOKEN": "test-github",
        }
        for profile in ("benchmark", "smoke"):
            with (
                self.subTest(profile=profile),
                patch.dict(os.environ, credentials, clear=True),
            ):
                configure_environment(profile, Path("/tmp/codeclash-test"))
                for key, value in credentials.items():
                    if key != "GITHUB_TOKEN":
                        self.assertEqual(
                            os.environ.get(key),
                            value if profile == "benchmark" else None,
                        )
                self.assertEqual(os.environ["GITHUB_TOKEN"], "")
                self.assertEqual(os.environ["PYTHON_DOTENV_DISABLED"], "1")
                self.assertEqual(os.environ["COPYFILE_DISABLE"], "1")

    def test_container_environment_cannot_receive_host_credentials(self):
        inspection = [
            {"Image": "image", "Config": {"Env": ["OPENROUTER_API_KEY=secret"]}}
        ]
        with patch(
            "codeclash.runtime.native_runner.execute",
            return_value=json.dumps(inspection),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "credential variable OPENROUTER_API_KEY"
            ):
                isolate_container(
                    SimpleNamespace(container_id="container"), "arena", "image", {}
                )

    def test_summary_keeps_config_order_even_if_tournament_shuffles_players(self):
        config = config_for("bridge", "smoke")
        names = [player["name"] for player in config["players"]]
        summary = initial_summary(
            {"task_config": {"profile": "smoke", "arena": "bridge"}, "run_id": "run"},
            config,
        )
        config["players"].reverse()
        self.assertEqual(summary["player_names"], names)
        self.assertEqual(len(names), 4)

    def test_all_arenas_keep_native_names_player_count_prompts_and_rules(self):
        for arena in ARENAS:
            with self.subTest(arena=arena):
                req = request(arena)
                before = deepcopy(req)
                actual, overrides = build_config(req)
                expected = deepcopy(before["task_config"]["resolved_native_config"])
                for player in expected["players"]:
                    if player["agent"] == "mini":
                        player["no_internet"] = False
                self.assertEqual(actual, expected)
                self.assertEqual(req, before)
                self.assertEqual(overrides, {})

    def test_smoke_scripted_editors_keep_original_player_count_and_prompts(self):
        for arena in ARENAS:
            with self.subTest(arena=arena):
                req = request(arena, "smoke")
                original = req["task_config"]["resolved_native_config"]
                config, _ = build_config(req)
                self.assertEqual(
                    [p["name"] for p in config["players"]],
                    [p["name"] for p in original["players"]],
                )
                self.assertTrue(
                    all(p["agent"] == "scripted" for p in config["players"])
                )
                self.assertEqual(config["prompts"], original["prompts"])
                self.assertEqual(config["tournament"], original["tournament"])
                self.assertEqual(config["game"], original["game"])

    def test_named_model_override_keeps_provider_and_options(self):
        req = request()
        player = req["task_config"]["resolved_native_config"]["players"][0]
        req["player_models"] = {player["name"]: "@new/model"}
        actual, _ = build_config(req)
        expected = deepcopy(player["config"])
        expected["model"]["model_name"] = "@new/model"
        self.assertEqual(actual["players"][0]["config"], expected)

    def test_unknown_or_dummy_override_is_rejected(self):
        for req in (
            request(player_models={"missing": "model"}),
            request(
                "bridge",
                player_models={
                    config_for("bridge", "benchmark")["players"][0]["name"]: "model"
                },
            ),
        ):
            with self.assertRaises(ValueError):
                build_config(req)

    def test_smoke_mixed_and_multiplayer_positional_overrides_rejected(self):
        for req in (
            request(profile="smoke", model_name="model"),
            request(model_name="model", player_models={"x": "y"}),
            request("bridge", model_name="model"),
        ):
            with self.assertRaises(ValueError):
                build_config(req)

    def test_nonmini_positional_and_branch_overrides_rejected(self):
        req = request("bomberland", model_name="model")
        with self.assertRaises(ValueError):
            build_config(req)
        for field in ("branch", "branch_init", "push", "force_push"):
            req = request()
            req["task_config"]["resolved_native_config"]["players"][0][field] = "bad"
            with self.assertRaises(ValueError):
                build_config(req)

    def test_all_catalog_smoke_edits_preserve_source_and_prior_round(self):
        for arena in ARENAS.values():
            before = b"original source\n"
            after, first = edit_source(
                before, arena["smoke_edit_path"], "named_player", 1, b'{"round_num":0}'
            )
            final, second = edit_source(
                after, arena["smoke_edit_path"], "named_player", 2, b'{"round_num":1}'
            )
            self.assertTrue(after.startswith(before))
            self.assertTrue(final.startswith(after))
            self.assertEqual(first["sha256_after"], second["sha256_before"])
            self.assertTrue(second["previous_marker_verified"])
        with self.assertRaises(ValueError):
            edit_source(b"", "../escape.py", "player", 1, b'{"round_num":0}')
        with self.assertRaisesRegex(ValueError, "Prior source"):
            edit_source(b"unchanged", "source.py", "player", 2, b'{"round_num":1}')
        with self.assertRaisesRegex(ValueError, "feedback"):
            edit_source(b"", "source.py", "player", 1, b'{"round_num":9}')
        self.assertIn(
            b"(* ", edit_source(b"", "main.ml", "player", 1, b'{"round_num":0}')[0]
        )

    def test_staged_editor_roundtrip_needs_no_container_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "container/workspace"
            workspace.mkdir(parents=True)
            source = workspace / "warrior.red"
            source.write_text("MOV 0, 1\n")
            output = root / "output"
            output.mkdir()
            commands = []

            def copy(*args, **kwargs):
                self.assertEqual(args[:2], ("docker", "cp"))
                paths = [
                    root / "container" / p.split(":", 1)[1].lstrip("/")
                    if p.startswith("container:")
                    else Path(p)
                    for p in args[2:]
                ]
                shutil.copyfile(*paths)
                return ""

            def execute(command):
                commands.append(command)
                return {"returncode": 0, "output": ""}

            player = SimpleNamespace(
                name="player",
                environment=SimpleNamespace(
                    container_id="container",
                    config=SimpleNamespace(cwd="/workspace"),
                    execute=execute,
                ),
                game_context=SimpleNamespace(round=1),
            )
            with patch("codeclash.runtime.native_runner.execute", side_effect=copy):
                for round_number in (1, 2):
                    feedback = (
                        root / f"container/logs/rounds/{round_number - 1}/results.json"
                    )
                    feedback.parent.mkdir(parents=True)
                    feedback.write_text(json.dumps({"round_num": round_number - 1}))
                    player.game_context.round = round_number
                    proof = scripted_edit(
                        player, output, {"smoke_edit_path": "warrior.red"}
                    )
                    self.assertTrue(proof["previous_marker_verified"])
            self.assertIn("harbor-codeclash-smoke-round-2", source.read_text())
            self.assertTrue(all("python" not in command for command in commands))


class FakeStats:
    def __init__(self):
        self.winner = None
        self.details = []
        self.player_stats = {
            "north": {"valid_submit": True},
            "south": {"valid_submit": True},
        }
        self.scores = {"north": 0, "south": 0}

    def to_dict(self):
        return deepcopy(vars(self))


@dataclass
class CompileResult:
    status: str
    reason: str


class FakeArena:
    def __init__(self, root):
        self.root = root
        self.environment = object()
        self._round_result = CompileResult("no_contest", "compile error")

    def log_round(self, number):
        return self.root

    def get_results(self, agents, number, stats):
        # Original scorers may legitimately change validity and add details.
        self._round_result.reason += " parsed"
        stats.winner = "/".join(a.name for a in agents)
        stats.details.append(self._round_result.reason)
        stats.scores = {a.name: -1000000.0 for a in agents}
        for name in stats.player_stats:
            stats.player_stats[name]["valid_submit"] = False


class ReplayTests(unittest.TestCase):
    def test_original_scorer_replayed_with_pre_state_and_all_native_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            arena = FakeArena(Path(tmp))
            (arena.root / "nested").mkdir()
            (arena.root / "nested/raw.log").write_text("native evidence")
            players = [SimpleNamespace(name=n) for n in ("south", "north")]
            records, stats = {}, FakeStats()
            original_environment = arena.environment
            install_score_replay(arena, records)
            arena.get_results(players, 1, stats)
            self.assertEqual(stats.winner, "south/north")
            self.assertEqual(arena._round_result.reason, "compile error parsed")
            self.assertIs(arena.environment, original_environment)
            self.assertEqual(records["1"]["scorer_players"], ["south", "north"])
            self.assertEqual(
                records["1"]["scorer_state"]["_round_result"]["reason"], "compile error"
            )
            self.assertTrue(
                records["1"]["pre_score_stats"]["player_stats"]["north"]["valid_submit"]
            )
            self.assertTrue(records["1"]["native_replay_verified"])

    def test_environment_execution_forbidden_during_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            arena = FakeArena(Path(tmp))
            arena.environment = SimpleNamespace(execute=lambda command: None)

            def scorer(agents, number, stats):
                arena.environment.execute("not permitted during replay")

            arena.get_results = scorer
            install_score_replay(arena, {})
            with self.assertRaisesRegex(RuntimeError, "environment access"):
                arena.get_results([], 1, FakeStats())

    def test_replay_divergence_and_log_mutation_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            arena = FakeArena(Path(tmp))
            counter = []

            def scorer(agents, number, stats):
                counter.append(1)
                stats.winner = str(len(counter))

            arena.get_results = scorer
            install_score_replay(arena, {})
            with self.assertRaisesRegex(RuntimeError, "diverged"):
                arena.get_results([], 1, FakeStats())

            def mutator(agents, number, stats):
                (arena.root / "new.log").write_text("changed")

            arena.get_results = mutator
            install_score_replay(arena, {})
            with self.assertRaisesRegex(RuntimeError, "modified"):
                arena.get_results([], 1, FakeStats())

    def test_recursive_hashes_cover_every_file_and_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nested").mkdir()
            (root / "nested/whatever.bin").write_bytes(b"\x00raw")
            (root / "results.json").write_text("{}")
            self.assertEqual(
                set(hash_round_files(root)), {"nested/whatever.bin", "results.json"}
            )
            (root / "link").symlink_to(root / "nested")
            with self.assertRaisesRegex(ValueError, "Symlink"):
                hash_round_files(root)
        self.assertEqual(
            primitive(CompileResult("ok", "")), {"status": "ok", "reason": ""}
        )


class OwnershipTests(unittest.TestCase):
    def test_dynamic_player_roles_and_owned_image_checks(self):
        self.assertTrue(valid_role("arena"))
        self.assertTrue(valid_role("player:player_5"))
        self.assertFalse(valid_role("p1"))
        self.assertFalse(valid_role("player:../escape"))
        inspection = {
            "Image": "ours",
            "Name": "/minisweagent-abcdef12",
            "Config": {"Labels": {RUN_LABEL: "run", ROLE_LABEL: "player:player_5"}},
        }
        self.assertEqual(
            validate_owned_inspection(inspection, "ours", "run"), "player:player_5"
        )
        with self.assertRaises(RuntimeError):
            validate_owned_inspection(inspection, "wrong", "run")

    def test_cleanup_recovers_partial_construction_by_label(self):
        owned = []
        container = "a" * 64
        inspection = {
            "Image": "ours",
            "Name": "/minisweagent-abcdef12",
            "Config": {"Labels": {RUN_LABEL: "run", ROLE_LABEL: "player:fourth"}},
        }
        responses = [
            container,
            json.dumps([inspection]),
            container,
            json.dumps([inspection]),
            container,
            "",
        ]
        with patch(
            "codeclash.runtime.native_runner.execute", side_effect=responses
        ) as execute:
            result = cleanup_containers(owned, "ours", "run")
        self.assertEqual(owned[0]["role"], "player:fourth")
        self.assertTrue(result[0]["removed"])
        execute.assert_any_call("docker", "rm", "-f", container, timeout=15)


if __name__ == "__main__":
    unittest.main()
