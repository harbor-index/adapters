"""Replay pinned native scoring without creating or executing any environment."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
import random
from types import MethodType


# These are the only execution-derived fields read by the pinned public scorers.
# All remaining scorer inputs are the native config, ordered players and logs.
SCORER_STATE_FIELDS = ("_round_result", "_failed_to_start_player", "sim_ext")


def primitive(value):
    if is_dataclass(value):
        return primitive(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [primitive(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported scorer state: {type(value).__name__}")


def hash_round_files(directory: Path) -> dict[str, str]:
    """Hash every regular file, recursively; never follow artifact symlinks."""
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("Native round directory is missing or a symlink")
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlink in native round evidence: {path.name}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"Non-regular native round evidence: {path.name}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        result[path.relative_to(directory).as_posix()] = digest.hexdigest()
    return result


class NoEnvironment:
    def __getattr__(self, name):
        raise RuntimeError(f"Native score replay attempted environment access: {name}")


def install_score_replay(arena, records: dict) -> None:
    """Wrap the original bound scorer and compare its full native RoundStats."""
    original = arena.get_results

    def checked(_self, agents, round_num, stats, *args, **kwargs):
        before = deepcopy(stats)
        state = {
            name: deepcopy(getattr(arena, name))
            for name in SCORER_STATE_FIELDS
            if hasattr(arena, name)
        }
        files_before = hash_round_files(arena.log_round(round_num))
        original(agents, round_num, stats, *args, **kwargs)
        after_state = {name: deepcopy(getattr(arena, name)) for name in state}
        environment = arena.environment
        replayed = deepcopy(before)
        try:
            for name, value in state.items():
                setattr(arena, name, deepcopy(value))
            arena.environment = NoEnvironment()
            original(agents, round_num, replayed, *args, **kwargs)
        finally:
            arena.environment = environment
            for name, value in after_state.items():
                setattr(arena, name, value)
        if replayed.to_dict() != stats.to_dict():
            raise RuntimeError(f"Native scoring replay diverged in round {round_num}")
        if hash_round_files(arena.log_round(round_num)) != files_before:
            raise RuntimeError("Native scorer modified its input artifacts")
        record = {
            "scorer_players": [agent.name for agent in agents],
            "scorer_state": primitive(state),
            "pre_score_stats": primitive(before.to_dict()),
            "native_replay_verified": True,
        }
        # Verify that recorded state is portable JSON rather than an object repr.
        json.dumps(record, allow_nan=False)
        records[str(round_num)] = record

    arena.get_results = MethodType(checked, arena)


def replay_validation_round(arena, agents, round_num, expected, native_run_round):
    """Replay upstream's no-contest/one-valid-player branch without containers."""
    validity = expected["player_stats"]
    valid = [name for name, value in validity.items() if value["valid_submit"]]
    if len(valid) > 1:
        raise RuntimeError("Missing get_results replay for a scored round")
    original_validation = arena.validate_code
    environment = arena.environment
    rng = random.getstate()
    try:
        arena.validate_code = lambda agent: (
            validity[agent.name]["valid_submit"],
            validity[agent.name]["invalid_reason"],
        )
        arena.environment = NoEnvironment()
        actual = native_run_round(arena, list(agents), round_num, copy_logs=False)
    finally:
        arena.validate_code = original_validation
        arena.environment = environment
        random.setstate(rng)
    if actual.to_dict() != expected:
        raise RuntimeError(f"Native validation replay diverged in round {round_num}")
    return {
        "scorer_players": [],
        "scorer_state": {},
        "pre_score_stats": None,
        "native_replay_verified": True,
    }
