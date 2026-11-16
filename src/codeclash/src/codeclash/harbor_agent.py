"""Host coordinator for native CodeClash; learning agents run in owned containers."""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import re
import signal
from typing import Any
from uuid import uuid4

from harbor.agents.base import BaseAgent
from harbor.agents.options import AgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.environment_type import EnvironmentType
from pydantic import Field

from codeclash.contract import UPSTREAM_COMMIT, is_diagnostic, validate_task_config

TASK_CONFIG_PATH = "/codeclash/task-config.json"
EVIDENCE_PATH = "/codeclash/evidence"
RUN_LABEL = "org.harbor.codeclash.run-id"
ROLE_LABEL = "org.harbor.codeclash.role"


class CodeClashOptions(AgentOptions):
    native_python: str = Field(
        description="Python in the separate upstream CodeClash environment."
    )
    upstream_repo: str = Field(
        description="Clean checkout of the pinned upstream CodeClash revision."
    )
    image: str | None = Field(
        default=None,
        description="Pinned native image; defaults to the task arena image.",
    )
    opponent_model_name: str | None = None
    player_models: dict[str, str] = Field(
        default_factory=dict, description="Native player name to model-name override."
    )
    timeout_sec: float | None = Field(default=None, gt=0)


def player_names(task_config: dict[str, Any]) -> list[str]:
    return [
        player["name"] for player in task_config["resolved_native_config"]["players"]
    ]


def container_roles(expected_players: list[str]) -> set[str]:
    return {"arena", *(f"player:{name}" for name in expected_players)}


def owned_containers(
    output: Path, run_id: str, expected_players: list[str]
) -> list[dict[str, str]]:
    path = output / "containers.json"
    if not path.exists():
        return []
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("run_id") != run_id:
        raise ValueError("Container manifest does not belong to this run")
    records = value.get("containers")
    if not isinstance(records, list):
        raise ValueError("Container manifest must contain a list")
    seen: set[str] = set()
    roles: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Invalid container manifest entry")
        container_id = record.get("container_id", "")
        image_id = record.get("image_id", "")
        role = record.get("role")
        if not isinstance(container_id, str) or not re.fullmatch(
            r"[0-9a-f]{64}", container_id
        ):
            raise ValueError("Invalid owned container ID")
        if not isinstance(image_id, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", image_id
        ):
            raise ValueError("Invalid owned image ID")
        if (
            not isinstance(role, str)
            or role not in container_roles(expected_players)
            or role in roles
            or container_id in seen
        ):
            raise ValueError("Duplicate or unknown container ownership")
        seen.add(container_id)
        roles.add(role)
    return records


def validate_summary(
    output: Path, run_id: str, task_config: dict[str, Any]
) -> dict[str, Any]:
    summary = json.loads((output / "runner-summary.json").read_text())
    if not isinstance(summary, dict) or summary.get("status") != "success":
        raise RuntimeError("Native CodeClash runner did not complete successfully")
    config = task_config["resolved_native_config"]
    expected_players = player_names(task_config)
    expected = {
        "run_id": run_id,
        "arena": task_config["arena"],
        "profile": task_config["profile"],
        "rounds": config["tournament"]["rounds"],
        "sims_per_round": config["game"]["sims_per_round"],
        "player_names": expected_players,
    }
    for key, value in expected.items():
        if summary.get(key) != value or type(summary.get(key)) is not type(value):
            raise RuntimeError(f"Native summary does not match the task: {key}")
    records = owned_containers(output, run_id, expected_players)
    if {record["role"] for record in records} != container_roles(expected_players):
        raise RuntimeError(
            "Successful run must account for the arena and every player container"
        )
    cleanup = summary.get("cleanup")
    if not isinstance(cleanup, list) or len(cleanup) != len(records):
        raise RuntimeError("Native cleanup evidence is incomplete")
    expected_ids = {record["container_id"] for record in records}
    if {
        entry.get("container_id") for entry in cleanup if isinstance(entry, dict)
    } != expected_ids:
        raise RuntimeError("Native cleanup container IDs do not match ownership")
    if not all(
        entry.get("removed") is True and entry.get("inventory_verified") is True
        for entry in cleanup
    ):
        raise RuntimeError("Native runner did not verify cleanup")
    return summary


class CodeClashAgent(BaseAgent):
    """Run an arena's native PvP workflow from the Harbor host.

    The Harbor container is a trusted evidence handoff environment. Player
    code never executes there and never receives the host Docker socket.
    """

    options_model = CodeClashOptions

    @staticmethod
    def name() -> str:
        return "codeclash"

    def version(self) -> str:
        return "0.2.0"

    @property
    def extra_env(self) -> dict[str, str]:
        # Harbor scopes this property into environment.exec calls. Model
        # credentials belong only in the host runner process, never containers.
        return {}

    @property
    def settings(self) -> CodeClashOptions:
        assert isinstance(self.options, CodeClashOptions)
        return self.options

    async def setup(self, environment: BaseEnvironment) -> None:
        if environment.type() != EnvironmentType.DOCKER:
            raise ValueError(
                "CodeClash currently requires the local Docker environment"
            )
        # Preserve the venv/bin/python path: resolving its symlink would invoke
        # the base interpreter and lose the native harness's installed deps.
        python = Path(self.settings.native_python).expanduser().absolute()
        upstream = Path(self.settings.upstream_repo).expanduser().resolve()
        if not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError(
                "native_python must be an executable in the upstream environment"
            )
        if not upstream.is_dir():
            raise ValueError("upstream_repo must be an existing checkout")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        task_path = self.logs_dir / "task-config.json"
        await environment.download_file(TASK_CONFIG_PATH, task_path)
        self.task_config = validate_task_config(json.loads(task_path.read_text()))
        self._validate_model_overrides()

    def _validate_model_overrides(self) -> None:
        players = self.task_config["resolved_native_config"]["players"]
        model_players = {
            player["name"] for player in players if player["agent"] == "mini"
        }
        positional = (
            self.model_name is not None or self.settings.opponent_model_name is not None
        )
        if self.task_config["profile"] == "smoke" and (
            positional or self.settings.player_models
        ):
            raise ValueError(
                "Smoke uses scripted editors and does not accept model overrides"
            )
        if positional and (len(players) != 2 or len(model_players) != 2):
            raise ValueError(
                "model_name/opponent_model_name require exactly two native mini players; "
                "use player_models for named model overrides"
            )
        if positional and self.settings.player_models:
            raise ValueError("Use either positional model options or player_models")
        if set(self.settings.player_models) - model_players:
            raise ValueError(
                "player_models may override only existing native mini players"
            )
        model_names = [self.model_name, self.settings.opponent_model_name]
        model_names.extend(self.settings.player_models.values())
        if any(value is not None and not value.strip() for value in model_names):
            raise ValueError("Model-name overrides must not be empty")

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        if not hasattr(self, "task_config"):
            raise RuntimeError("CodeClash setup must complete before run")
        output = self.logs_dir / "native"
        if output.exists():
            raise FileExistsError("Refusing to overwrite a native tournament attempt")
        run_id = uuid4().hex
        request = {
            "upstream_repo": str(
                Path(self.settings.upstream_repo).expanduser().resolve()
            ),
            "native_image": self.settings.image
            or f"codeclash-adapter/{self.task_config['arena']}:{UPSTREAM_COMMIT[:12]}",
            "task_config": self.task_config,
            "output_dir": str(output.resolve()),
            "model_name": self.model_name,
            "opponent_model_name": self.settings.opponent_model_name,
            "player_models": self.settings.player_models,
            "run_id": run_id,
        }
        request_path = self.logs_dir / "native-request.json"
        request_path.write_text(json.dumps(request, indent=2) + "\n")
        runtime_env = {**os.environ, **self._extra_env}
        runtime_env.pop("PYTHONPATH", None)
        runtime_env.pop("PYTHONHOME", None)
        runtime_env["PYTHON_DOTENV_DISABLED"] = "1"
        runner = Path(__file__).parent / "runtime" / "native_runner.py"
        process: asyncio.subprocess.Process | None = None
        with (self.logs_dir / "native-runner.log").open("wb") as log:
            try:
                process = await asyncio.create_subprocess_exec(
                    str(Path(self.settings.native_python).expanduser().absolute()),
                    str(runner),
                    "--request",
                    str(request_path.resolve()),
                    cwd=request["upstream_repo"],
                    env=runtime_env,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                if self.settings.timeout_sec is None:
                    return_code = await process.wait()
                else:
                    return_code = await asyncio.wait_for(
                        process.wait(), self.settings.timeout_sec
                    )
                if return_code != 0:
                    raise RuntimeError(
                        f"Native CodeClash exited with status {return_code}; see native-runner.log"
                    )
                summary = validate_summary(output, run_id, self.task_config)
                audit = await self._audit_cleanup(
                    output, run_id, remove_remaining=False
                )
                if not audit["all_owned_containers_absent"]:
                    raise RuntimeError("Native CodeClash left owned containers running")
                self._populate_context(context, output, summary)
                await environment.upload_dir(output, EVIDENCE_PATH)
                # Mounted Docker already shares this directory. On another Docker
                # implementation, protect host-produced logs before Harbor syncs.
                if not environment.capabilities.mounted:
                    await environment.upload_dir(
                        self.logs_dir, str(self.environment_logs_dir)
                    )
            except BaseException:
                cleanup_task = asyncio.create_task(
                    self._stop_and_cleanup(process, output, run_id)
                )
                try:
                    await asyncio.shield(cleanup_task)
                except BaseException as cleanup_error:
                    self.logger.error(
                        "CodeClash cleanup could not be verified: %s", cleanup_error
                    )
                raise

    @staticmethod
    async def _docker(*args: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        except BaseException:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise
        return process.returncode, (
            stdout if process.returncode == 0 else stderr
        ).decode()

    async def _audit_cleanup(
        self, output: Path, run_id: str, *, remove_remaining: bool
    ) -> dict[str, Any]:
        expected_players = player_names(self.task_config)
        records = owned_containers(output, run_id, expected_players)
        code, inventory = await self._docker("ps", "-aq", "--no-trunc")
        if code != 0:
            raise RuntimeError("Could not verify native container inventory")
        active_ids = set(inventory.splitlines())
        code, labeled_inventory = await self._docker(
            "ps", "-aq", "--no-trunc", "--filter", f"label={RUN_LABEL}={run_id}"
        )
        if code != 0:
            raise RuntimeError(
                "Could not discover interrupted native container creation"
            )
        labeled_ids = set(labeled_inventory.splitlines())
        if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in labeled_ids):
            raise RuntimeError("Invalid labeled container identity")
        recorded = {entry["container_id"]: entry for entry in records}
        candidates = (active_ids & recorded.keys()) | labeled_ids
        recovered = []
        if candidates:
            expected_images = {entry["image_id"] for entry in records}
            provenance_path = output / "provenance.json"
            if provenance_path.exists():
                provenance = json.loads(provenance_path.read_text())
                if (
                    provenance.get("upstream_commit") != UPSTREAM_COMMIT
                    or provenance.get("arena") != self.task_config["arena"]
                    or provenance.get("resolved_config_sha256")
                    != self.task_config["resolved_config_sha256"]
                    or provenance.get("run_id") != run_id
                ):
                    raise RuntimeError(
                        "Cannot establish image ownership from mismatched provenance"
                    )
                expected_images.add(provenance.get("image_id"))
            if len(expected_images) != 1:
                raise RuntimeError(
                    "Cannot establish the native image for interrupted cleanup"
                )
            expected_image = next(iter(expected_images))
            if not isinstance(expected_image, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", expected_image
            ):
                raise RuntimeError("Invalid native image ownership")
            for container_id in sorted(candidates):
                code, text = await self._docker("inspect", container_id)
                if code != 0:
                    raise RuntimeError(
                        "Refusing cleanup: container ownership could not be inspected"
                    )
                inspection = json.loads(text)[0]
                labels = inspection.get("Config", {}).get("Labels") or {}
                role = labels.get(ROLE_LABEL)
                if (
                    inspection.get("Id") != container_id
                    or inspection.get("Image") != expected_image
                    or labels.get(RUN_LABEL) != run_id
                    or role not in container_roles(expected_players)
                    or not re.fullmatch(
                        r"/minisweagent-[0-9a-f]{8}", inspection.get("Name", "")
                    )
                    or (
                        container_id in recorded
                        and recorded[container_id]["role"] != role
                    )
                ):
                    raise RuntimeError(
                        "Refusing cleanup: container image, labels, role or name do not establish ownership"
                    )
                if container_id not in recorded:
                    entry = {
                        "container_id": container_id,
                        "image_id": expected_image,
                        "role": role,
                    }
                    records.append(entry)
                    recovered.append(entry)
                if remove_remaining:
                    code, _ = await self._docker("rm", "-f", container_id)
                    if code != 0:
                        raise RuntimeError(
                            "Could not remove an owned CodeClash container"
                        )
        active_ids |= labeled_ids
        if remove_remaining:
            code, inventory = await self._docker("ps", "-aq", "--no-trunc")
            if code != 0:
                raise RuntimeError("Could not verify inventory after CodeClash cleanup")
            active_ids = set(inventory.splitlines())
        audit = {
            "run_id": run_id,
            "owned_container_ids": [entry["container_id"] for entry in records],
            "recovered_from_run_labels": recovered,
            "all_owned_containers_absent": not any(
                entry["container_id"] in active_ids for entry in records
            ),
        }
        (self.logs_dir / "native-cleanup-audit.json").write_text(
            json.dumps(audit, indent=2) + "\n"
        )
        if remove_remaining and not audit["all_owned_containers_absent"]:
            raise RuntimeError("CodeClash owned containers remain after cleanup")
        return audit

    async def _stop_and_cleanup(
        self, process: asyncio.subprocess.Process | None, output: Path, run_id: str
    ) -> None:
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 30)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        await self._audit_cleanup(output, run_id, remove_remaining=True)

    def _populate_context(
        self, context: AgentContext, output: Path, summary: dict[str, Any]
    ) -> None:
        metadata = json.loads((output / "metadata.json").read_text())
        costs: dict[str, float] = {}
        api_calls: dict[str, int] = {}
        for player in metadata.get("agents", []):
            stats = player.get("agent_stats", {})
            values = list(stats.values())
            if values and all(
                isinstance(entry.get("cost"), (int, float))
                and not isinstance(entry.get("cost"), bool)
                and math.isfinite(entry["cost"])
                and entry["cost"] >= 0
                for entry in values
            ):
                costs[player["name"]] = sum(entry["cost"] for entry in values)
            if values and all(
                type(entry.get("api_calls")) is int and entry["api_calls"] >= 0
                for entry in values
            ):
                api_calls[player["name"]] = sum(entry["api_calls"] for entry in values)
        if self.task_config["profile"] == "smoke":
            context.cost_usd = 0.0
        elif len(costs) == len(metadata.get("agents", [])) and costs:
            context.cost_usd = sum(costs.values())
        context.metadata = {
            "arena": self.task_config["arena"],
            "profile": self.task_config["profile"],
            "diagnostic_only": is_diagnostic(self.task_config),
            "native_status": summary["status"],
            "source_revision": UPSTREAM_COMMIT,
            "arena_sources": self.task_config["arena_sources"],
            "resolved_config_sha256": self.task_config["resolved_config_sha256"],
            "player_names": player_names(self.task_config),
            "cost_usd_by_player": costs,
            "api_calls_by_player": api_calls,
            "task_counting": "One arena item; rounds and tournaments are not independent tasks",
        }
