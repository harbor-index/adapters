"""Serializable task contract shared by generation, host runtime and verifier."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

UPSTREAM_COMMIT = "f0694c64ecf6abfca2bc867bad2de9333fef5be8"
ARENA_NAMES = {
    "ants": "Ants",
    "battlecode23": "BattleCode23",
    "battlecode24": "BattleCode24",
    "battlecode25": "BattleCode25",
    "battlesnake": "BattleSnake",
    "bomberland": "Bomberland",
    "bridge": "Bridge",
    "chess": "Chess",
    "corewar": "CoreWar",
    "cyborg": "CybORG",
    "figgie": "Figgie",
    "gomoku": "Gomoku",
    "halite": "Halite",
    "halite2": "Halite2",
    "halite3": "Halite3",
    "huskybench": "HuskyBench",
    "lightcycles": "LightCycles",
    "paintvolley": "PaintVolley",
    "robocode": "RoboCode",
    "robotrumble": "RobotRumble",
    "scml": "SCML",
}


def config_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def player_names(config: dict) -> list[str]:
    return [player["name"] for player in config["players"]]


def is_diagnostic(manifest: dict) -> bool:
    """Dummy agents and native Mini test models do not establish model evaluation."""
    test_models = {
        "deterministic",
        "minisweagent.models.test_models.DeterministicModel",
        "minisweagent.models.test_models.DeterministicToolcallModel",
        "minisweagent.models.test_models.DeterministicResponseAPIToolcallModel",
    }
    return (
        manifest["profile"] == "smoke"
        or manifest["config_origin"] == "upstream_diagnostic"
        or not any(
            player["agent"] == "mini"
            and player.get("config", {}).get("model", {}).get("model_class")
            not in test_models
            for player in manifest["resolved_native_config"]["players"]
        )
    )


def validate_native_config(config: Any, arena: str) -> dict:
    import re

    require(isinstance(config, dict), "Native configuration must be an object")
    require(arena in ARENA_NAMES, "Unsupported arena")
    require(
        not ({"player", "ladder", "ladder_rules"} & config.keys()),
        "Single-player and ladder configurations require separate native backends; this adapter runs PvP",
    )
    game, tournament, players = (
        config.get("game"),
        config.get("tournament"),
        config.get("players"),
    )
    require(
        isinstance(game, dict) and game.get("name") == ARENA_NAMES[arena],
        "Native arena mismatch",
    )
    require(isinstance(tournament, dict), "Missing native tournament configuration")
    for obj, key in ((game, "sims_per_round"), (tournament, "rounds")):
        require(
            type(obj.get(key)) is int and obj[key] > 0,
            f"{key} must be a positive integer",
        )
    require(
        type(tournament.get("transparent", False)) is bool,
        "transparent must be boolean",
    )
    require(
        isinstance(players, list) and len(players) >= 2,
        "PvP requires at least two players",
    )
    names = []
    for player in players:
        require(isinstance(player, dict), "Player must be an object")
        name = player.get("name")
        require(
            isinstance(name, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", name) is not None,
            "Player names must be shell-safe identifiers",
        )
        require(player.get("agent") in {"dummy", "mini"}, "Unknown native player agent")
        require(
            not player.get("push") and not player.get("force_push"),
            "Remote player pushes are not supported",
        )
        require(
            not player.get("branch_init"),
            "branch_init requires an explicitly pinned opponent snapshot; not supported yet",
        )
        require(not player.get("branch"), "Player branch overrides are not supported")
        names.append(name)
    require(len(set(names)) == len(names), "Player names must be unique")
    if arena == "bridge":
        require(len(players) == 4, "Bridge requires four players")
    if arena == "figgie":
        require(len(players) in {4, 5}, "Figgie requires four or five players")
    if arena == "bomberland":
        sims = game.get("args", {}).get("sims_per_round", game["sims_per_round"])
        require(
            type(sims) is int and sims > 0 and sims % 2 == 0,
            "Bomberland requires an even simulation budget",
        )
    if arena == "robocode":
        require(
            game["sims_per_round"] >= 10 and game["sims_per_round"] % 10 == 0,
            "RoboCode simulations must be a positive multiple of its native batch size (10)",
        )
    require(isinstance(config.get("prompts"), dict), "Missing native prompts")
    # Reject non-JSON values and NaN before crossing process/container boundaries.
    config_digest(config)
    return config


def validate_task_config(value: Any) -> dict:
    import re

    require(isinstance(value, dict), "Task configuration must be an object")
    require(
        type(value.get("schema_version")) is int and value["schema_version"] == 2,
        "Unsupported task schema",
    )
    require(value.get("adapter") == "codeclash", "Wrong adapter")
    require(value.get("mode") == "pvp", "Unsupported CodeClash workflow mode")
    require(
        value.get("profile") in {"smoke", "benchmark"}, "Unsupported CodeClash profile"
    )
    require(
        value.get("upstream_commit") == UPSTREAM_COMMIT, "Unexpected upstream revision"
    )
    arena = value.get("arena")
    require(
        arena in ARENA_NAMES and value.get("arena_name") == ARENA_NAMES[arena],
        "Unexpected arena identity",
    )
    config = validate_native_config(value.get("resolved_native_config"), arena)
    require(
        value.get("resolved_config_sha256") == config_digest(config),
        "Native configuration hash mismatch",
    )
    require(value.get("counting_unit") == "arena", "Task counting unit must be arena")
    require(
        value.get("config_origin")
        in {"published_pvp", "upstream_diagnostic", "user_supplied"},
        "Unknown configuration origin",
    )
    require(
        isinstance(value.get("source_config_path"), str)
        and bool(value["source_config_path"]),
        "Missing source configuration path",
    )
    require(
        re.fullmatch(r"[0-9a-f]{64}", value.get("source_config_sha256", ""))
        is not None,
        "Invalid source configuration digest",
    )
    require(
        isinstance(value.get("arena_sources"), list) and bool(value["arena_sources"]),
        "Missing arena source provenance",
    )
    for source in value["arena_sources"]:
        require(isinstance(source, dict), "Invalid arena provenance record")
        if "repository" in source:
            require(
                isinstance(source["repository"], str)
                and source["repository"].startswith("https://github.com/"),
                "Expected public arena source URL",
            )
            require(
                re.fullmatch(r"[0-9a-f]{40}", source.get("commit", "")) is not None,
                "Arena source must be commit-pinned",
            )
    return value


def finite_number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)
