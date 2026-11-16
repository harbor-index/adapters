"""Pinned native configurations for every substantive CodeClash arena.

This module is standard-library-only so the native subprocess can load it by
filename without importing the adapter's ``codeclash`` package into upstream's
equally named package. Source snapshots are data, not a claim of runtime parity.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path


_CATALOG = json.loads(Path(__file__).with_name("catalog.json").read_text())
UPSTREAM_COMMIT = _CATALOG["upstream_commit"]
ARENAS = {arena["id"]: arena for arena in _CATALOG["arenas"]}


def config_sha256(config: dict) -> str:
    """Hash a resolved JSON configuration consistently across runtimes."""
    return hashlib.sha256(
        json.dumps(
            config, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def get_arena(slug: str) -> dict:
    """Return an isolated catalog entry; accept only stable lowercase IDs."""
    if slug not in ARENAS:
        raise ValueError(f"Unknown CodeClash arena: {slug!r}")
    return deepcopy(ARENAS[slug])


def config_for(arena: str, profile: str) -> dict:
    """Return the complete resolved native config with the requested run budget.

    ``benchmark`` preserves the source exactly, including diagnostic dummy
    players where no published PvP config exists. It never upgrades a diagnostic
    source into a published benchmark. Callers must retain ``config_origin``.
    ``smoke`` changes only edit rounds, simulation count and simulation
    concurrency. The runtime supplies scripted players separately. Prompts and
    game rules are left verbatim, even when a published prompt hardcodes 15 rounds.
    """
    if profile not in {"smoke", "benchmark"}:
        raise ValueError(f"Unknown CodeClash profile: {profile!r}")
    entry = get_arena(arena)
    config = entry["native_config"]
    if profile == "smoke":
        config["tournament"]["rounds"] = 2
        config["game"]["sims_per_round"] = entry["smoke_sims"]
        config["game"]["sim_concurrency"] = 1
    return config
