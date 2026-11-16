"""Transport and ownership checks for the host-side Harbor coordinator (no Docker/LLM)."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from harbor.models.agent.context import AgentContext
from harbor.models.environment_type import EnvironmentType

from codeclash.contract import config_digest
from codeclash.harbor_agent import (
    CodeClashAgent,
    EVIDENCE_PATH,
    ROLE_LABEL,
    RUN_LABEL,
    UPSTREAM_COMMIT,
    owned_containers,
    validate_summary,
    validate_task_config,
)


def task_config(
    profile="smoke", names=("north", "east", "south", "west"), agent="dummy"
):
    arena_name = "Bridge" if len(names) == 4 else "BattleSnake"
    config = {
        "tournament": {"rounds": 3},
        "game": {"name": arena_name, "sims_per_round": 10},
        "players": [
            {
                "name": name,
                "agent": agent,
                **(
                    {"config": {"agent": {}, "model": {"model_name": "source-model"}}}
                    if agent == "mini"
                    else {}
                ),
            }
            for name in names
        ],
        "prompts": {"game_description": "Improve {{player_id}} in round {{round}}."},
    }
    return {
        "schema_version": 2,
        "adapter": "codeclash",
        "arena": arena_name.lower(),
        "arena_name": arena_name,
        "mode": "pvp",
        "profile": profile,
        "upstream_commit": UPSTREAM_COMMIT,
        "resolved_native_config": config,
        "resolved_config_sha256": config_digest(config),
        "counting_unit": "arena",
        "source_config_path": f"configs/test/{arena_name.lower()}.yaml",
        "source_config_sha256": "b" * 64,
        "arena_sources": [
            {
                "repository": f"https://github.com/CodeClash-ai/{arena_name}.git",
                "commit": "a" * 40,
            }
        ],
        "config_origin": "upstream_diagnostic",
    }


def container_records(config):
    roles = ["arena"] + [
        f"player:{player['name']}"
        for player in config["resolved_native_config"]["players"]
    ]
    return [
        {
            "container_id": f"{index + 1:064x}",
            "role": role,
            "image_id": "sha256:" + "d" * 64,
        }
        for index, role in enumerate(roles)
    ]


CONFIG = task_config()
PLAYERS = [player["name"] for player in CONFIG["resolved_native_config"]["players"]]
CONTAINERS = container_records(CONFIG)


def inspection(record, run_id="ours", **updates):
    value = {
        "Id": record["container_id"],
        "Image": record["image_id"],
        "Name": "/minisweagent-1234abcd",
        "Config": {"Labels": {RUN_LABEL: run_id, ROLE_LABEL: record["role"]}},
    }
    value.update(updates)
    return json.dumps([value])


def write_result(output, run_id, status="success", config=CONFIG):
    output.mkdir()
    records = container_records(config)
    native = config["resolved_native_config"]
    names = [player["name"] for player in native["players"]]
    (output / "containers.json").write_text(
        json.dumps({"run_id": run_id, "containers": records})
    )
    (output / "runner-summary.json").write_text(
        json.dumps(
            {
                "status": status,
                "run_id": run_id,
                "arena": config["arena"],
                "profile": config["profile"],
                "rounds": native["tournament"]["rounds"],
                "sims_per_round": native["game"]["sims_per_round"],
                "player_names": names,
                "cleanup": [
                    {
                        "container_id": row["container_id"],
                        "removed": True,
                        "inventory_verified": True,
                    }
                    for row in records
                ],
            }
        )
    )
    (output / "metadata.json").write_text(
        json.dumps({"agents": [{"name": name, "agent_stats": {}} for name in names]})
    )


class ConfigAndEvidenceTests(unittest.TestCase):
    def test_rejects_source_and_resolved_config_drift(self):
        for field, value in (
            ("upstream_commit", "bad"),
            ("resolved_config_sha256", "c" * 64),
            ("mode", "ladder"),
            ("schema_version", True),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_task_config({**CONFIG, field: value})

        changed = deepcopy(CONFIG)
        changed["resolved_native_config"]["game"]["sims_per_round"] = 100
        with self.assertRaises(ValueError):
            validate_task_config(changed)

    def test_native_multiplayer_budget_preserved_without_battlesnake_defaults(self):
        config = validate_task_config(task_config(profile="benchmark"))
        self.assertEqual(config["resolved_native_config"]["tournament"]["rounds"], 3)
        self.assertEqual(len(config["resolved_native_config"]["players"]), 4)

    def test_summary_requires_success_and_all_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "native"
            write_result(output, "ours")
            self.assertEqual(
                validate_summary(output, "ours", CONFIG)["status"], "success"
            )
            summary = json.loads((output / "runner-summary.json").read_text())
            summary["cleanup"][0]["inventory_verified"] = False
            (output / "runner-summary.json").write_text(json.dumps(summary))
            with self.assertRaises(RuntimeError):
                validate_summary(output, "ours", CONFIG)

    def test_summary_rejects_wrong_run_arena_budget_and_players(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "native"
            write_result(output, "ours")
            summary = json.loads((output / "runner-summary.json").read_text())
            for field, value in (
                ("run_id", "foreign"),
                ("arena", "battlesnake"),
                ("rounds", 15),
                ("sims_per_round", 1000),
                ("player_names", ["p1", "p2"]),
                ("profile", "benchmark"),
            ):
                with (
                    self.subTest(field=field),
                    self.assertRaisesRegex(RuntimeError, field),
                ):
                    (output / "runner-summary.json").write_text(
                        json.dumps({**summary, field: value})
                    )
                    validate_summary(output, "ours", CONFIG)

    def test_success_requires_all_named_player_containers(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "native"
            write_result(output, "ours")
            (output / "containers.json").write_text(
                json.dumps(
                    {
                        "run_id": "ours",
                        "containers": CONTAINERS[:-1],
                    }
                )
            )
            with self.assertRaisesRegex(RuntimeError, "every player"):
                validate_summary(output, "ours", CONFIG)

    def test_manifest_rejects_foreign_run_and_duplicate_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "native"
            write_result(output, "ours")
            with self.assertRaises(ValueError):
                owned_containers(output, "someone-else", PLAYERS)
            records = deepcopy(CONTAINERS)
            records[1]["container_id"] = records[0]["container_id"]
            (output / "containers.json").write_text(
                json.dumps({"run_id": "ours", "containers": records})
            )
            with self.assertRaises(ValueError):
                owned_containers(output, "ours", PLAYERS)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = deepcopy(CONFIG)
        self.agent = CodeClashAgent(
            logs_dir=self.root / "logs",
            native_python=sys.executable,
            upstream_repo=str(self.root),
            extra_env={"OPENAI_API_KEY": "test-host-only"},
        )
        self.environment = MagicMock()
        self.environment.type.return_value = EnvironmentType.DOCKER
        self.environment.capabilities.mounted = True
        self.environment.upload_dir = AsyncMock()

        async def download(source, destination):
            Path(destination).write_text(json.dumps(self.config))

        self.environment.download_file = AsyncMock(side_effect=download)
        self.agent._docker = AsyncMock(return_value=(0, ""))

    async def test_provider_rejected_before_native_execution(self):
        self.environment.type.return_value = EnvironmentType.DAYTONA
        with self.assertRaisesRegex(ValueError, "local Docker"):
            await self.agent.setup(self.environment)
        self.environment.download_file.assert_not_awaited()

    async def test_credentials_are_host_only(self):
        self.assertEqual(self.agent.extra_env, {})
        self.assertEqual(self.agent._extra_env["OPENAI_API_KEY"], "test-host-only")

    async def test_smoke_rejects_model_overrides_instead_of_ignoring_them(self):
        self.agent.settings.player_models = {"north": "some-model"}
        with self.assertRaisesRegex(ValueError, "Smoke uses scripted editors"):
            await self.agent.setup(self.environment)

    async def test_positional_model_options_require_two_native_mini_players(self):
        for names, agent in ((("red", "blue"), "dummy"), (tuple(PLAYERS), "mini")):
            self.config = task_config(profile="benchmark", names=names, agent=agent)
            self.agent.model_name = "new-model"
            with (
                self.subTest(names=names, agent=agent),
                self.assertRaisesRegex(ValueError, "exactly two native mini"),
            ):
                await self.agent.setup(self.environment)

    async def test_named_models_reject_unknown_and_dummy_players(self):
        self.config = task_config(profile="benchmark")
        for name in ("north", "not_a_player"):
            self.agent.settings.player_models = {name: "new-model"}
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "existing native mini"),
            ):
                await self.agent.setup(self.environment)

    async def test_mixed_model_override_styles_are_rejected(self):
        self.config = task_config(
            profile="benchmark", names=("red", "blue"), agent="mini"
        )
        self.agent.model_name = "new-model"
        self.agent.settings.player_models = {"red": "other-model"}
        with self.assertRaisesRegex(ValueError, "either positional"):
            await self.agent.setup(self.environment)

    async def test_two_named_native_models_allow_positional_overrides(self):
        self.config = task_config(
            profile="benchmark", names=("red", "blue"), agent="mini"
        )
        self.agent.model_name = "new-red-model"
        self.agent.settings.opponent_model_name = "new-blue-model"
        await self.agent.setup(self.environment)
        self.assertEqual(
            self.agent.task_config["resolved_native_config"]["players"],
            self.config["resolved_native_config"]["players"],
        )

    async def test_named_multiplayer_models_and_custom_image_reach_native_request(self):
        self.config = task_config(profile="benchmark", agent="mini")
        self.agent.settings.player_models = {name: f"model-{name}" for name in PLAYERS}
        self.agent.settings.image = "custom-bridge:locked"
        await self.agent.setup(self.environment)
        process = MagicMock(returncode=0)
        process.wait = AsyncMock(return_value=0)

        async def create(*args, **kwargs):
            request = json.loads(Path(args[3]).read_text())
            self.assertEqual(
                request["player_models"], self.agent.settings.player_models
            )
            self.assertIsNone(request["model_name"])
            self.assertEqual(request["native_image"], "custom-bridge:locked")
            self.assertEqual(request["task_config"], self.config)
            write_result(
                Path(request["output_dir"]), request["run_id"], config=self.config
            )
            return process

        with patch(
            "codeclash.harbor_agent.asyncio.create_subprocess_exec", side_effect=create
        ):
            await self.agent.run("", self.environment, AgentContext())
        self.environment.upload_dir.assert_awaited_once()

    async def test_complete_result_uploaded_and_context_identifies_diagnostic(self):
        venv_python = self.root / "native-venv-python"
        venv_python.symlink_to(sys.executable)
        self.agent.settings.native_python = str(venv_python)
        await self.agent.setup(self.environment)
        process = MagicMock(returncode=0)
        process.wait = AsyncMock(return_value=0)

        async def create(*args, **kwargs):
            self.assertEqual(args[0], str(venv_python))
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["env"]["OPENAI_API_KEY"], "test-host-only")
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            request = json.loads(Path(args[3]).read_text())
            self.assertEqual(
                set(request),
                {
                    "upstream_repo",
                    "native_image",
                    "task_config",
                    "output_dir",
                    "model_name",
                    "opponent_model_name",
                    "player_models",
                    "run_id",
                },
            )
            self.assertEqual(request["task_config"], self.config)
            self.assertEqual(
                request["native_image"],
                f"codeclash-adapter/bridge:{UPSTREAM_COMMIT[:12]}",
            )
            self.assertTrue(Path(args[1]).is_file())
            write_result(
                Path(request["output_dir"]), request["run_id"], config=self.config
            )
            return process

        context = AgentContext()
        with patch(
            "codeclash.harbor_agent.asyncio.create_subprocess_exec", side_effect=create
        ):
            await self.agent.run(
                "no changes to upstream prompts", self.environment, context
            )
        self.environment.upload_dir.assert_awaited_once_with(
            self.agent.logs_dir / "native", EVIDENCE_PATH
        )
        self.assertEqual(context.cost_usd, 0)
        self.assertEqual(context.metadata["profile"], "smoke")
        self.assertEqual(context.metadata["player_names"], PLAYERS)

    async def test_incomplete_runner_never_uploads_evidence(self):
        await self.agent.setup(self.environment)
        process = MagicMock(returncode=0)
        process.wait = AsyncMock(return_value=0)

        async def create(*args, **kwargs):
            request = json.loads(Path(args[3]).read_text())
            write_result(
                Path(request["output_dir"]), request["run_id"], status="failed"
            )
            return process

        with patch(
            "codeclash.harbor_agent.asyncio.create_subprocess_exec", side_effect=create
        ):
            with self.assertRaisesRegex(RuntimeError, "did not complete"):
                await self.agent.run("", self.environment, AgentContext())
        self.environment.upload_dir.assert_not_awaited()

    async def test_cancel_terminates_process_group_and_keeps_error(self):
        await self.agent.setup(self.environment)
        process = MagicMock(returncode=None, pid=12345)
        process.wait = AsyncMock(side_effect=[asyncio.CancelledError(), 0])
        with patch(
            "codeclash.harbor_agent.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            with patch("codeclash.harbor_agent.os.killpg") as kill:
                with self.assertRaises(asyncio.CancelledError):
                    await self.agent.run("", self.environment, AgentContext())
        kill.assert_called_once_with(12345, signal.SIGTERM)
        self.environment.upload_dir.assert_not_awaited()
        self.agent._docker.assert_any_await("ps", "-aq", "--no-trunc")

    async def test_cleanup_refuses_image_mismatch(self):
        await self.agent.setup(self.environment)
        output = self.agent.logs_dir / "native"
        write_result(output, "ours")
        owned = CONTAINERS[0]["container_id"]
        self.agent._docker.side_effect = [
            (0, owned + "\n"),
            (0, owned + "\n"),
            (0, inspection(CONTAINERS[0], Image="sha256:" + "e" * 64)),
        ]
        with self.assertRaisesRegex(RuntimeError, "do not establish ownership"):
            await self.agent._audit_cleanup(output, "ours", remove_remaining=True)
        self.assertFalse(
            any(call.args[0] == "rm" for call in self.agent._docker.await_args_list)
        )

    async def test_cleanup_removes_only_owned_matching_container(self):
        await self.agent.setup(self.environment)
        output = self.agent.logs_dir / "native"
        write_result(output, "ours")
        owned = CONTAINERS[0]["container_id"]
        foreign = "f" * 64
        self.agent._docker.side_effect = [
            (0, owned + "\n" + foreign + "\n"),
            (0, owned + "\n"),
            (0, inspection(CONTAINERS[0])),
            (0, owned),
            (0, foreign + "\n"),
        ]
        audit = await self.agent._audit_cleanup(output, "ours", remove_remaining=True)
        self.assertTrue(audit["all_owned_containers_absent"])
        self.agent._docker.assert_any_await("rm", "-f", owned)
        self.assertFalse(
            any(foreign in call.args for call in self.agent._docker.await_args_list)
        )

    async def test_hardkill_recovers_container_created_before_manifest_write(self):
        await self.agent.setup(self.environment)
        output = self.agent.logs_dir / "native"
        output.mkdir()
        (output / "provenance.json").write_text(
            json.dumps(
                {
                    "upstream_commit": UPSTREAM_COMMIT,
                    "arena": self.config["arena"],
                    "resolved_config_sha256": self.config["resolved_config_sha256"],
                    "run_id": "ours",
                    "image_id": CONTAINERS[0]["image_id"],
                }
            )
        )
        record = CONTAINERS[-1]
        owned = record["container_id"]
        self.agent._docker.side_effect = [
            (0, owned + "\n"),
            (0, owned + "\n"),
            (0, inspection(record)),
            (0, owned),
            (0, ""),
        ]
        process = MagicMock(returncode=None, pid=12345)
        process.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), 0])
        with patch("codeclash.harbor_agent.os.killpg") as kill:
            await self.agent._stop_and_cleanup(process, output, "ours")
        self.assertEqual(
            [call.args for call in kill.call_args_list],
            [
                (12345, signal.SIGTERM),
                (12345, signal.SIGKILL),
            ],
        )
        self.agent._docker.assert_any_await("rm", "-f", owned)
        audit = json.loads(
            (self.agent.logs_dir / "native-cleanup-audit.json").read_text()
        )
        self.assertEqual(audit["recovered_from_run_labels"], [record])
        self.assertTrue(audit["all_owned_containers_absent"])

    async def test_run_label_cannot_override_foreign_container_identity(self):
        await self.agent.setup(self.environment)
        output = self.agent.logs_dir / "native"
        write_result(output, "ours")
        owned = CONTAINERS[0]["container_id"]
        for bad_inspection in (
            inspection(CONTAINERS[0], run_id="foreign"),
            inspection(CONTAINERS[0], Name="/unrelated-user-container"),
        ):
            with self.subTest(inspection=bad_inspection):
                self.agent._docker.reset_mock()
                self.agent._docker.side_effect = [
                    (0, owned),
                    (0, owned),
                    (0, bad_inspection),
                ]
                with self.assertRaisesRegex(RuntimeError, "do not establish ownership"):
                    await self.agent._audit_cleanup(
                        output, "ours", remove_remaining=True
                    )
                self.assertFalse(
                    any(
                        call.args[0] == "rm"
                        for call in self.agent._docker.await_args_list
                    )
                )

    async def test_recovery_rejects_provenance_from_another_task_or_run(self):
        await self.agent.setup(self.environment)
        output = self.agent.logs_dir / "native"
        output.mkdir()
        provenance = {
            "upstream_commit": UPSTREAM_COMMIT,
            "arena": self.config["arena"],
            "resolved_config_sha256": self.config["resolved_config_sha256"],
            "run_id": "ours",
            "image_id": CONTAINERS[0]["image_id"],
        }
        for field, value in (
            ("arena", "battlesnake"),
            ("resolved_config_sha256", "e" * 64),
            ("run_id", "another-run"),
        ):
            with self.subTest(field=field):
                (output / "provenance.json").write_text(
                    json.dumps({**provenance, field: value})
                )
                self.agent._docker.reset_mock()
                self.agent._docker.side_effect = [
                    (0, CONTAINERS[0]["container_id"]),
                    (0, CONTAINERS[0]["container_id"]),
                ]
                with self.assertRaisesRegex(RuntimeError, "mismatched provenance"):
                    await self.agent._audit_cleanup(
                        output, "ours", remove_remaining=True
                    )
                self.assertFalse(
                    any(
                        call.args[0] == "rm"
                        for call in self.agent._docker.await_args_list
                    )
                )

    async def test_untracked_container_is_not_claimed_without_pinned_image_evidence(
        self,
    ):
        await self.agent.setup(self.environment)
        output = self.agent.logs_dir / "native"
        self.agent._docker.side_effect = [
            (0, CONTAINERS[0]["container_id"]),
            (0, CONTAINERS[0]["container_id"]),
        ]
        with self.assertRaisesRegex(RuntimeError, "Cannot establish the native image"):
            await self.agent._audit_cleanup(output, "ours", remove_remaining=True)
        self.assertFalse(
            any(call.args[0] == "rm" for call in self.agent._docker.await_args_list)
        )

    async def test_cost_sums_all_named_players_without_fabricating_tokens(self):
        await self.agent.setup(self.environment)
        self.agent.task_config["profile"] = "benchmark"
        output = self.agent.logs_dir / "native"
        write_result(output, "ours")
        (output / "metadata.json").write_text(
            json.dumps(
                {
                    "agents": [
                        {
                            "name": name,
                            "agent_stats": {"1": {"cost": 0.1, "api_calls": 2}},
                        }
                        for name in PLAYERS
                    ]
                }
            )
        )
        context = AgentContext()
        self.agent._populate_context(context, output, {"status": "success"})
        self.assertAlmostEqual(context.cost_usd, 0.4)
        self.assertIsNone(context.n_input_tokens)
        self.assertEqual(
            context.metadata["api_calls_by_player"], {name: 2 for name in PLAYERS}
        )

    async def test_custom_dummy_benchmark_remains_diagnostic_in_context(self):
        for native_agent, expected in (("dummy", True), ("mini", False)):
            with self.subTest(native_agent=native_agent):
                self.config = task_config(profile="benchmark", agent=native_agent)
                self.config["config_origin"] = "user_supplied"
                await self.agent.setup(self.environment)
                output = self.agent.logs_dir / native_agent
                write_result(output, "ours", config=self.config)
                context = AgentContext()
                self.agent._populate_context(context, output, {"status": "success"})
                self.assertIs(context.metadata["diagnostic_only"], expected)

    async def test_native_mini_test_models_remain_diagnostic_in_context(self):
        for model_class in (
            "deterministic",
            "minisweagent.models.test_models.DeterministicModel",
            "minisweagent.models.test_models.DeterministicToolcallModel",
            "minisweagent.models.test_models.DeterministicResponseAPIToolcallModel",
        ):
            with self.subTest(model_class=model_class):
                self.config = task_config(profile="benchmark", agent="mini")
                self.config["config_origin"] = "user_supplied"
                native = self.config["resolved_native_config"]
                for player in native["players"]:
                    player["config"]["model"]["model_class"] = model_class
                self.config["resolved_config_sha256"] = config_digest(native)
                await self.agent.setup(self.environment)
                output = self.agent.logs_dir / model_class
                write_result(output, "ours", config=self.config)
                context = AgentContext()
                self.agent._populate_context(context, output, {"status": "success"})
                self.assertIs(context.metadata["diagnostic_only"], True)

    async def test_model_against_fixed_opponent_is_not_diagnostic(self):
        for opponent in (
            {"name": "fixed", "agent": "dummy"},
            {
                "name": "fixed",
                "agent": "mini",
                "config": {"model": {"model_class": "deterministic"}},
            },
        ):
            with self.subTest(opponent=opponent):
                self.config = task_config(
                    profile="benchmark", names=("evaluated", "fixed"), agent="mini"
                )
                self.config["config_origin"] = "user_supplied"
                native = self.config["resolved_native_config"]
                native["players"][1] = opponent
                self.config["resolved_config_sha256"] = config_digest(native)
                await self.agent.setup(self.environment)
                output = self.agent.logs_dir / opponent["agent"]
                write_result(output, "ours", config=self.config)
                context = AgentContext()
                self.agent._populate_context(context, output, {"status": "success"})
                self.assertIs(context.metadata["diagnostic_only"], False)


if __name__ == "__main__":
    unittest.main()
