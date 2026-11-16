#!/usr/bin/env python3
"""Run pinned native multi-player PvP in a separate CodeClash interpreter."""

from __future__ import annotations

import argparse
from copy import deepcopy
import importlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
from types import ModuleType
import traceback
import tempfile

RUN_LABEL = "org.harbor.codeclash.run-id"
ROLE_LABEL = "org.harbor.codeclash.role"
ISOLATE_REFS_COMMAND = (
    "git for-each-ref --format='delete %(refname)' refs/remotes refs/tags "
    "| git update-ref --no-deref --stdin"
)
REQUEST_KEYS = {
    "upstream_repo",
    "native_image",
    "task_config",
    "output_dir",
    "model_name",
    "opponent_model_name",
    "player_models",
    "run_id",
}


def adapter_module(name):
    # Upstream and the adapter both export codeclash. Load only trusted adapter
    # helpers under a private package name, leaving codeclash free for upstream.
    package = "_harbor_codeclash_adapter"
    if package not in sys.modules:
        module = ModuleType(package)
        module.__path__ = [str(Path(__file__).resolve().parents[1])]
        sys.modules[package] = module
    return importlib.import_module(f"{package}.{name}")


def execute(*args, cwd=None, timeout=60):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True, timeout=timeout
    ).stdout.strip()


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    temporary.replace(path)


def read_request(path):
    request = json.loads(path.read_text())
    required = {"upstream_repo", "native_image", "task_config", "output_dir", "run_id"}
    if (
        not isinstance(request, dict)
        or not required <= request.keys()
        or request.keys() - REQUEST_KEYS
    ):
        raise ValueError("Unexpected or missing native request fields")
    for key in ("upstream_repo", "native_image", "output_dir", "run_id"):
        if not isinstance(request[key], str) or not request[key].strip():
            raise ValueError(f"{key} must be a nonempty string")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", request["native_image"]):
        raise ValueError("Invalid Docker image reference")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", request["run_id"]):
        raise ValueError("Invalid run identifier")
    for key in ("model_name", "opponent_model_name"):
        request.setdefault(key, None)
        if request[key] is not None and (
            not isinstance(request[key], str) or not request[key].strip()
        ):
            raise ValueError(f"{key} must be null or a nonempty string")
    request.setdefault("player_models", {})
    if request["player_models"] is None:
        request["player_models"] = {}
    if not isinstance(request["player_models"], dict) or any(
        not isinstance(v, str) or not v.strip()
        for v in request["player_models"].values()
    ):
        raise ValueError("player_models must map player names to model names")
    adapter_module("contract").validate_task_config(request["task_config"])
    return request


def build_config(request):
    """Apply explicit model selections; preserve all native players and prompts."""
    manifest = request["task_config"]
    config = deepcopy(manifest["resolved_native_config"])
    players = config["players"]
    positional = any(
        request.get(key) is not None for key in ("model_name", "opponent_model_name")
    )
    named = request.get("player_models") or {}
    if positional and named:
        raise ValueError("Do not combine positional and named model overrides")
    if manifest["profile"] == "smoke" and (positional or named):
        raise ValueError("Smoke never accepts model overrides")
    overrides = dict(named)
    if positional:
        if len(players) != 2 or any(p["agent"] != "mini" for p in players):
            raise ValueError(
                "Positional model overrides require exactly two native mini players"
            )
        for player, key in zip(players, ("model_name", "opponent_model_name")):
            if request.get(key) is not None:
                overrides[player["name"]] = request[key]
    by_name = {player["name"]: player for player in players}
    if overrides.keys() - by_name.keys():
        raise ValueError("Model override names an unknown native player")
    for name, model in overrides.items():
        if by_name[name]["agent"] != "mini":
            raise ValueError("Model overrides cannot convert a non-mini player")
        by_name[name]["config"]["model"]["model_name"] = model
    for player in players:
        if (
            player.get("push")
            or player.get("force_push")
            or player.get("branch")
            or player.get("branch_init")
        ):
            raise ValueError("Remote writes and branch overrides are unsupported")
        if manifest["profile"] == "smoke":
            player["agent"] = "scripted"
            player.pop("config", None)
        elif player["agent"] == "mini":
            # Native Linux nsenter control is replaced by Docker isolation.
            player["no_internet"] = False
    return config, overrides


def credential_name(key):
    return any(
        token in key.upper()
        for token in (
            "API_KEY",
            "TOKEN",
            "SECRET",
            "CREDENTIAL",
            "PRIVATE_KEY",
            "ACCESS_KEY",
        )
    )


def configure_environment(profile, output):
    # Model providers execute on the trusted host. Native Docker environments
    # receive only CodeArena's explicit environment, audited below.
    if profile == "smoke":
        for key in tuple(os.environ):
            if credential_name(key):
                os.environ.pop(key, None)
    os.environ.update(
        GITHUB_TOKEN="",
        PYTHON_DOTENV_DISABLED="1",
        MSWEA_GLOBAL_CONFIG_DIR=str(output / "runtime-config"),
        MSWEA_SILENT_STARTUP="1",
        MPLCONFIGDIR=str(output / "matplotlib-config"),
        # Native tar on macOS otherwise injects AppleDouble pseudo-files.
        COPYFILE_DISABLE="1",
    )
    for key in ("AWS_BATCH_JOB_ID", "PYTEST_CURRENT_TEST", "MSWEA_DOCKER_EXECUTABLE"):
        os.environ.pop(key, None)


def checked_environment_command(environment, command, timeout=None):
    result = (
        environment.execute(command, timeout=timeout)
        if timeout is not None
        else environment.execute(command)
    )
    if result.get("returncode") != 0:
        raise RuntimeError(
            f"Native container command failed: {command}\n{result.get('output', '')}\n"
            f"{result.get('exception_info', '')}"
        )
    return result.get("output", "").strip()


def valid_role(role):
    return isinstance(role, str) and (
        role == "arena"
        or re.fullmatch(r"player:[A-Za-z][A-Za-z0-9_-]{0,79}", role) is not None
    )


def validate_owned_inspection(inspection, image_id, run_id):
    labels = inspection.get("Config", {}).get("Labels") or {}
    role = labels.get(ROLE_LABEL)
    if (
        inspection["Image"] != image_id
        or labels.get(RUN_LABEL) != run_id
        or not valid_role(role)
        or not re.fullmatch(r"/minisweagent-[0-9a-f]{8}", inspection.get("Name", ""))
    ):
        raise RuntimeError(
            "Container image, run label, role or native name does not establish ownership"
        )
    return role


def cleanup_containers(owned, image_id, run_id):
    results = []
    try:
        discovered = execute(
            "docker",
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label={RUN_LABEL}={run_id}",
            timeout=15,
        ).splitlines()
        for container_id in discovered:
            inspection = json.loads(
                execute("docker", "container", "inspect", container_id)
            )[0]
            role = validate_owned_inspection(inspection, image_id, run_id)
            if not any(item["container_id"] == container_id for item in owned):
                owned.append(
                    {
                        "container_id": container_id,
                        "role": role,
                        "image_id": image_id,
                        "recovered_from_run_label": True,
                    }
                )
    except Exception as error:
        results.append(
            {
                "container_id": None,
                "removed": False,
                "inventory_verified": False,
                "error": f"Ownership discovery failed: {type(error).__name__}: {error}",
            }
        )
    for item in owned:
        container_id = item["container_id"]
        record = {
            "container_id": container_id,
            "removed": False,
            "inventory_verified": False,
        }
        try:
            present = set(
                execute("docker", "ps", "-aq", "--no-trunc", timeout=15).splitlines()
            )
            if container_id in present:
                inspection = json.loads(
                    execute("docker", "container", "inspect", container_id)
                )[0]
                validate_owned_inspection(inspection, image_id, run_id)
                execute("docker", "rm", "-f", container_id, timeout=15)
            remaining = set(
                execute("docker", "ps", "-aq", "--no-trunc", timeout=15).splitlines()
            )
            record.update(
                removed=container_id not in remaining, inventory_verified=True
            )
        except Exception as error:
            record["error"] = f"{type(error).__name__}: {error}"
        results.append(record)
    return results


def isolate_container(environment, role, image_id, arena):
    container_id = environment.container_id
    inspection = json.loads(execute("docker", "container", "inspect", container_id))[0]
    if inspection["Image"] != image_id:
        raise RuntimeError("Native container image differs from verified image")
    for item in inspection.get("Config", {}).get("Env", []):
        key, separator, value = item.partition("=")
        if separator and value and credential_name(key):
            raise RuntimeError(f"Native container received credential variable {key}")
    head = checked_environment_command(environment, "git rev-parse HEAD")
    tree = checked_environment_command(environment, "git rev-parse 'HEAD^{tree}'")
    source = arena["workspace_source"]
    if source["kind"] == "repository" and head != source["commit"]:
        raise RuntimeError(f"Native {role} differs from pinned arena source")
    if source["kind"] == "bundled_runtime" and tree != source["tree"]:
        raise RuntimeError(f"Native {role} differs from pinned bundled runtime tree")
    for network in inspection["NetworkSettings"]["Networks"]:
        execute("docker", "network", "disconnect", "--force", network, container_id)
    after = json.loads(execute("docker", "container", "inspect", container_id))[0]
    if after["NetworkSettings"]["Networks"]:
        raise RuntimeError(f"Native {role} still has network access")
    remotes = checked_environment_command(environment, "git remote").splitlines()
    for remote in remotes:
        checked_environment_command(
            environment, "git remote remove " + shlex.quote(remote)
        )
    for command in (
        ISOLATE_REFS_COMMAND,
        "git reflog expire --expire=now --all",
        "git gc --prune=now --quiet",
        "rm -f .git/FETCH_HEAD .git/ORIG_HEAD",
    ):
        checked_environment_command(environment, command, timeout=120)
    remotes = checked_environment_command(environment, "git remote").splitlines()
    refs = checked_environment_command(
        environment, "git for-each-ref --format='%(refname)' refs/remotes refs/tags"
    ).splitlines()
    if remotes or refs:
        raise RuntimeError("Remote repository references remain after isolation")
    return {
        "container_id": container_id,
        "role": role,
        "image_id": image_id,
        "workspace_head": head,
        "workspace_tree": tree,
        "workspace_source": source,
        "networks": [],
        "git_remotes": remotes,
        "remote_refs": refs,
    }


def edit_source(before, edited_path, player_name, round_number, feedback):
    path = Path(edited_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Smoke source path must remain inside the workspace")
    comments = {
        ".py": ("# ", ""),
        ".sh": ("# ", ""),
        ".js": ("// ", ""),
        ".java": ("// ", ""),
        ".rs": ("// ", ""),
        ".cpp": ("// ", ""),
        ".c": ("// ", ""),
        ".h": ("// ", ""),
        ".red": ("; ", ""),
        ".ml": ("(* ", " *)"),
        ".md": ("<!-- ", " -->"),
        ".txt": ("", ""),
    }
    if path.suffix.lower() not in comments:
        raise ValueError(f"Unsupported smoke source type: {path.suffix}")
    if json.loads(feedback)["round_num"] != round_number - 1:
        raise ValueError("Previous round feedback does not match edit round")
    text = before.decode()
    previous_marker = f"harbor-codeclash-smoke-round-{round_number - 1}"
    if round_number != 1 and previous_marker not in text:
        raise ValueError("Prior source state missing")
    marker = f"harbor-codeclash-smoke-round-{round_number}"
    if marker in text:
        raise ValueError("Round marker already present")
    prefix, ending = comments[path.suffix.lower()]
    after = (text + "\n" + prefix + marker + ending + "\n").encode()
    return after, {
        "player": player_name,
        "round": round_number,
        "previous_round": round_number - 1,
        "edited_path": edited_path,
        "sha256_before": hashlib.sha256(before).hexdigest(),
        "sha256_after": hashlib.sha256(after).hexdigest(),
        "previous_marker_verified": round_number == 1 or previous_marker in text,
        "marker_added": marker,
        "feedback_sha256": hashlib.sha256(feedback).hexdigest(),
    }


def scripted_edit(player, output, arena, opponents=None, opponent_dir=None):
    # Native arena images do not universally contain Python. Stage only this
    # diagnostic source and feedback through Docker cp; models still run native.
    source = str(Path(player.environment.config.cwd) / arena["smoke_edit_path"])
    quoted = shlex.quote(source)
    checked_environment_command(
        player.environment,
        f'test -f {quoted} && test ! -L {quoted} && test "$(readlink -f {quoted})" = {quoted}',
    )
    round_number = player.game_context.round
    container = player.environment.container_id
    with tempfile.TemporaryDirectory(prefix="codeclash-smoke-") as temp:
        staging = Path(temp)
        source_copy, feedback_copy = staging / "source", staging / "feedback.json"
        execute("docker", "cp", f"{container}:{source}", str(source_copy))
        execute(
            "docker",
            "cp",
            f"{container}:/logs/rounds/{round_number - 1}/results.json",
            str(feedback_copy),
        )
        if source_copy.is_symlink() or feedback_copy.is_symlink():
            raise ValueError("Diagnostic source and feedback must be regular files")
        after, proof = edit_source(
            source_copy.read_bytes(),
            arena["smoke_edit_path"],
            player.name,
            round_number,
            feedback_copy.read_bytes(),
        )
        source_copy.write_bytes(after)
        execute("docker", "cp", str(source_copy), f"{container}:{source}")
        check_copy = staging / "edited-source"
        execute("docker", "cp", f"{container}:{source}", str(check_copy))
        if check_copy.read_bytes() != after:
            raise ValueError("Container did not retain the diagnostic source edit")
        if opponents:
            snapshots = {}
            for opponent in opponents:
                destination = staging / opponent
                execute(
                    "docker",
                    "cp",
                    f"{container}:/{opponent_dir}/{opponent}",
                    str(destination),
                    timeout=120,
                )
                inventory = adapter_module("runtime.replay").hash_round_files(
                    destination
                )
                if not inventory:
                    raise ValueError("Transparent opponent codebase is empty")
                if round_number > 1:
                    previous = json.loads(
                        (
                            output
                            / "scripted-edits"
                            / f"{opponent}-round-{round_number - 1}.json"
                        ).read_text()
                    )
                    if not any(
                        (
                            path == arena["smoke_edit_path"]
                            or path.endswith("/" + arena["smoke_edit_path"])
                        )
                        and digest == previous["sha256_after"]
                        for path, digest in inventory.items()
                    ):
                        raise ValueError(
                            "Latest opponent source is not visible in transparent mode"
                        )
                snapshots[opponent] = inventory
            proof["opponent_codebases"] = snapshots
    folder = output / "scripted-edits"
    folder.mkdir(exist_ok=True)
    atomic_json(folder / f"{player.name}-round-{round_number}.json", proof)
    return proof


def initial_summary(request, config):
    manifest = request["task_config"]
    return {
        "schema_version": 2,
        "status": "failed",
        "run_id": request["run_id"],
        "profile": manifest["profile"],
        "arena": manifest["arena"],
        "player_names": [player["name"] for player in config["players"]],
        "rounds": config["tournament"]["rounds"],
        "sims_per_round": config["game"]["sims_per_round"],
        "cleanup": [],
        "isolation": [],
        "editor_assertions": [],
        "round_execution": {},
    }


def run(request):
    contract, catalog = adapter_module("contract"), adapter_module("catalog")
    replay = adapter_module("runtime.replay")
    manifest = contract.validate_task_config(request["task_config"])
    arena = catalog.get_arena(manifest["arena"])
    if arena["name"] != manifest["arena_name"]:
        raise ValueError("Catalog and task arena identity differ")
    expected_sources = [*arena.get("external_sources", [])]
    if "repository" in arena["workspace_source"]:
        expected_sources.append(arena["workspace_source"])
    expected_pins = {
        (source["repository"], source["commit"]) for source in expected_sources
    }
    actual_pins = {
        (source.get("repository"), source.get("commit"))
        for source in manifest["arena_sources"]
    }
    if expected_pins != actual_pins:
        raise ValueError("Task arena source pins differ from the runtime catalog")
    config, overrides = build_config(request)
    upstream, output = (
        Path(request["upstream_repo"]).resolve(),
        Path(request["output_dir"]).resolve(),
    )
    if output.exists():
        raise ValueError("output_dir must not exist; attempts are never overwritten")
    if execute("git", "rev-parse", "HEAD", cwd=upstream) != contract.UPSTREAM_COMMIT:
        raise ValueError("Upstream checkout is not at the pinned revision")
    if execute(
        "git", "status", "--porcelain", "--", "codeclash", "configs", cwd=upstream
    ):
        raise ValueError("Pinned upstream code/configs contain local changes")
    image = json.loads(execute("docker", "image", "inspect", request["native_image"]))[
        0
    ]
    image_id, labels = image["Id"], image.get("Config", {}).get("Labels") or {}
    if (
        labels.get("org.codeclash.source-revision") != contract.UPSTREAM_COMMIT
        or labels.get("org.harbor.codeclash.arena") != manifest["arena"]
    ):
        raise ValueError("Image provenance does not match the task arena and source")
    output.mkdir(parents=True)
    atomic_json(output / "request.json", request)
    atomic_json(output / "task-config.json", manifest)
    configure_environment(manifest["profile"], output)
    sys.path.insert(0, str(upstream))
    owned, environments = [], []
    roles = ["arena", *["player:" + p["name"] for p in config["players"]]]
    rounds = config["tournament"]["rounds"]
    summary = initial_summary(request, config)

    def save_manifest():
        atomic_json(
            output / "containers.json",
            {"run_id": request["run_id"], "containers": owned},
        )

    def finalize():
        summary["cleanup"] = cleanup_containers(owned, image_id, request["run_id"])
        save_manifest()
        if any(
            not row["removed"] or not row["inventory_verified"]
            for row in summary["cleanup"]
        ):
            summary.update(status="failed", cleanup_failed=True)
        for environment in environments:
            if any(
                row["container_id"] == environment.container_id and row["removed"]
                for row in summary["cleanup"]
            ):
                environment.container_id = None
        atomic_json(output / "runner-summary.json", summary)

    def interrupted(signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        summary.update(status="failed", error=f"Interrupted by signal {signum}")
        finalize()
        os._exit(128 + signum)

    save_manifest()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        import codeclash
        import codeclash.arenas.arena as arena_module
        from codeclash.agents.player import Player
        from codeclash.agents.utils import GameContext
        from codeclash.constants import DIR_WORK
        from codeclash.tournaments.pvp import PvpTournament
        from codeclash.utils.environment import ClashDockerEnvironment

        if Path(codeclash.__file__).resolve() != upstream / "codeclash/__init__.py":
            raise RuntimeError(
                "Wrong codeclash package imported; use the native environment"
            )
        arena_type = getattr(
            importlib.import_module(arena["module"]), arena["class_name"]
        )
        atomic_json(output / "effective-config.json", config)
        provenance = {
            "schema_version": 2,
            "run_id": request["run_id"],
            "upstream_commit": contract.UPSTREAM_COMMIT,
            "source_dirty": False,
            "arena": manifest["arena"],
            "arena_name": arena["name"],
            "arena_sources": manifest["arena_sources"],
            "profile": manifest["profile"],
            "resolved_config_sha256": manifest["resolved_config_sha256"],
            "effective_config_sha256": contract.config_digest(config),
            "effective_native_config": deepcopy(config),
            "model_overrides": overrides,
            "image_id": image_id,
            "image_labels": labels,
            "python_version": sys.version.split()[0],
            "diagnostic_only": contract.is_diagnostic(manifest),
            "seed_policy": "native_configuration",
            "container_credentials": "none",
            "external_writes": False,
            "packaging_changes": [
                "prebuilt pinned source image",
                "Docker network disconnection",
                "git remote/ref isolation",
                "local-only origin bootstrap if absent",
                "host score replay",
                "smoke-only host-staged source comment edits and feedback checks",
                "COPYFILE_DISABLE=1 prevents macOS resource-fork archive entries",
                "no_internet=False for native mini because Docker network is disconnected",
            ],
            "limitations": [
                "Native arena filesystem/process isolation is not hardened against adversarial submitted code",
                "Base images and toolchain dependencies remain incompletely pinned",
            ],
        }
        atomic_json(output / "provenance.json", provenance)

        class TrackedEnvironment(ClashDockerEnvironment):
            def __init__(self, **kwargs):
                if len(owned) >= len(roles):
                    raise RuntimeError("Unexpected extra native container")
                role = roles[len(owned)]
                kwargs["run_args"] = [
                    *kwargs.get("run_args", []),
                    "--label",
                    f"{RUN_LABEL}={request['run_id']}",
                    "--label",
                    f"{ROLE_LABEL}={role}",
                ]
                super().__init__(**kwargs)
                record = {
                    "container_id": self.container_id,
                    "role": role,
                    "image_id": image_id,
                }
                owned.append(record)
                environments.append(self)
                save_manifest()
                # Bundled-runtime images have a git repo but no origin, while
                # upstream Player unconditionally fetches origin during init.
                remotes = checked_environment_command(self, "git remote").splitlines()
                if "origin" not in remotes:
                    checked_environment_command(
                        self, "git remote add origin /workspace"
                    )
                    record["local_origin_bootstrap"] = True
                    save_manifest()

        score_records = {}

        class RecordedTournament(PvpTournament):
            def get_agent(self, agent_config, prompts):
                if agent_config["agent"] != "scripted":
                    return super().get_agent(agent_config, prompts)
                agent_config.setdefault("commit_label", "")
                environment = self.game.get_environment(
                    f"{self.game.game_id}.{agent_config['name']}"
                )
                context = GameContext(
                    id=self.game.game_id,
                    log_env=self.game.log_env,
                    log_local=self.game.log_local,
                    name=self.game.name,
                    player_id=agent_config["name"],
                    prompts=prompts,
                    round=1,
                    rounds=self.rounds,
                    working_dir=str(DIR_WORK),
                    arena_description=self.game.description,
                )

                class ScriptedPlayer(Player):
                    def run(player):
                        summary["editor_assertions"].append(
                            scripted_edit(
                                player,
                                output,
                                arena,
                                opponents=[
                                    p["name"]
                                    for p in config["players"]
                                    if p["name"] != player.name
                                ]
                                if self.transparent
                                else None,
                                opponent_dir=importlib.import_module(
                                    "codeclash.constants"
                                ).OPPONENT_CODEBASES_DIR_NAME,
                            )
                        )

                return ScriptedPlayer(
                    agent_config, environment=environment, game_context=context
                )

            def run_competition_phase(self, round_num):
                super().run_competition_phase(round_num)
                stats = self._metadata["round_stats"][round_num]
                valid = [
                    name
                    for name, value in stats["player_stats"].items()
                    if value["valid_submit"]
                ]
                record = score_records.get(str(round_num))
                kind = "scored"
                if record is None:
                    record = replay.replay_validation_round(
                        self.game,
                        self.agents,
                        round_num,
                        stats,
                        arena_module.CodeArena.run_round,
                    )
                    kind = "validation_forfeit" if valid else "all_invalid"
                summary["round_execution"][str(round_num)] = {
                    **record,
                    "valid_players": valid,
                    "ordered_players": [a.name for a in self.agents],
                    "execution_kind": kind,
                    "files": replay.hash_round_files(self.game.log_round(round_num)),
                }
                atomic_json(output / "round-execution.json", summary["round_execution"])

        arena_module.ClashDockerEnvironment = TrackedEnvironment
        arena_type.image_name = property(lambda self: request["native_image"])
        tournament = RecordedTournament(
            config, output_dir=output, cleanup=False, keep_containers=False
        )
        provenance["initialized_native_config"] = deepcopy(config)
        provenance["initialized_config_sha256"] = contract.config_digest(config)
        atomic_json(output / "provenance.json", provenance)
        if len(owned) != len(roles):
            raise RuntimeError("Missing native player or arena container")
        for item, environment in zip(owned, environments, strict=True):
            summary["isolation"].append(
                isolate_container(environment, item["role"], image_id, arena)
            )
        atomic_json(output / "isolation.json", summary["isolation"])
        replay.install_score_replay(tournament.game, score_records)
        tournament.run()
        metadata = json.loads((output / "metadata.json").read_text())
        expected = {str(number) for number in range(rounds + 1)}
        if (
            set(metadata["round_stats"]) != expected
            or set(summary["round_execution"]) != expected
        ):
            raise RuntimeError("Native tournament did not complete all rounds")
        summary["status"] = "success"
    except BaseException as error:
        summary["error"] = f"{type(error).__name__}: {error}"
        (output / "failure.txt").write_text(traceback.format_exc())
        traceback.print_exc()
    finally:
        finalize()
    print(json.dumps({"status": summary["status"], "output_dir": str(output)}))
    return 0 if summary["status"] == "success" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    try:
        return run(read_request(args.request))
    except Exception as error:
        print(f"Preflight failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
