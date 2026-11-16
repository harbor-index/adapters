"""Verify native PvP evidence transport and metrics; not adversarial arena isolation."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile

try:
    from .contract import (
        config_digest,
        finite_number,
        is_diagnostic,
        player_names,
        require,
        validate_task_config,
    )
except ImportError:  # The separate verifier runs this file outside the package.
    from contract import (
        config_digest,
        finite_number,
        is_diagnostic,
        player_names,
        require,
        validate_task_config,
    )


def read_json(path):
    return json.loads(path.read_text())


def read_round(root: Path, number: int):
    """Stream logs without extracting tar paths or assuming an arena log format."""
    files, results, results_hash = {}, None, None

    def consume(relative, stream):
        nonlocal results, results_hash
        require(relative not in files, "Duplicate round evidence")
        digest = hashlib.sha256()
        if relative == "results.json":
            data = stream.read()
            digest.update(data)
            results = json.loads(data)
            results_hash = digest.hexdigest()
        else:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        files[relative] = digest.hexdigest()

    direct = root / "rounds" / str(number)
    if direct.is_dir():
        for path in sorted(direct.rglob("*")):
            require(not path.is_symlink(), "Symlink in round evidence")
            if path.is_file():
                with path.open("rb") as stream:
                    consume(path.relative_to(direct).as_posix(), stream)
    else:
        with tarfile.open(
            root / "rounds" / f"round_{number}.tar.gz", "r|gz"
        ) as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                require(
                    not path.is_absolute()
                    and ".." not in path.parts
                    and path.parts
                    and path.parts[0] == str(number),
                    "Unsafe round archive path",
                )
                if member.isdir():
                    continue
                require(
                    member.isfile() and len(path.parts) >= 2, "Non-file round evidence"
                )
                consume(
                    PurePosixPath(*path.parts[1:]).as_posix(),
                    archive.extractfile(member),
                )
    require(results is not None, f"Missing results for round {number}")
    return results, files, results_hash


def check_native_stats(stats, names, execution, sims):
    players = stats["player_stats"]
    require(set(players) == set(names), "Native player inventory mismatch")
    require(
        all(
            type(item.get("valid_submit")) is bool and finite_number(item.get("score"))
            for item in players.values()
        ),
        "Malformed native player stats",
    )
    scores = stats["scores"]
    require(
        isinstance(scores, dict)
        and set(names) <= set(scores)
        and all(finite_number(v) for v in scores.values()),
        "Malformed native scores",
    )
    require(
        all(scores[name] == players[name]["score"] for name in names),
        "Native player score mismatch",
    )
    require(
        isinstance(stats.get("winner"), str) and bool(stats["winner"]),
        "Missing native winner",
    )
    valid = {name for name in names if players[name]["valid_submit"]}
    require(
        set(execution["valid_players"]) == valid, "Native validation status mismatch"
    )
    require(
        len(execution["ordered_players"]) == len(names)
        and set(execution["ordered_players"]) == set(names),
        "Native player order is incomplete",
    )
    for name in set(names) - valid:
        require(
            bool(players[name]["invalid_reason"]),
            "Missing native invalid-submission reason",
        )
    require(
        execution["native_replay_verified"] is True, "Native scorer replay not verified"
    )
    kind = execution["execution_kind"]
    if kind == "scored":
        require(
            execution["native_replay_verified"] is True,
            "Native scorer replay not verified",
        )
        scorer_players = execution["scorer_players"]
        require(
            len(scorer_players) >= 2
            and len(set(scorer_players)) == len(scorer_players)
            and set(scorer_players) <= set(names),
            "Invalid native scorer players",
        )
        # Some native scorers invalidate submissions after initial validation.
        # Do not equate scorer_players with the final valid_players set.
    elif kind == "validation_forfeit":
        require(len(valid) == 1, "Invalid validation forfeit")
        winner = next(iter(valid))
        require(
            stats["winner"] == winner
            and all(scores[n] == (sims if n == winner else 0) for n in names),
            "Native forfeit score mismatch",
        )
    elif kind == "all_invalid":
        require(
            not valid
            and stats["winner"] == "Tie"
            and all(scores[n] == sims / len(names) for n in names),
            "Native all-invalid tie mismatch",
        )
    else:
        raise ValueError("Unknown native round execution kind")


def verify_evidence(root: Path, manifest: dict):
    validate_task_config(manifest)
    config, profile = manifest["resolved_native_config"], manifest["profile"]
    names = player_names(config)
    rounds, sims = config["tournament"]["rounds"], config["game"]["sims_per_round"]
    summary, provenance, metadata = [
        read_json(root / name)
        for name in ("runner-summary.json", "provenance.json", "metadata.json")
    ]
    require(summary["status"] == "success", "Native runner incomplete")
    require(
        summary["arena"] == provenance["arena"] == manifest["arena"],
        "Runtime arena mismatch",
    )
    require(
        summary["profile"] == provenance["profile"] == profile,
        "Runtime profile mismatch",
    )
    require(
        provenance["upstream_commit"] == manifest["upstream_commit"]
        and provenance["source_dirty"] is False,
        "Runtime source mismatch",
    )
    require(
        provenance["resolved_config_sha256"] == manifest["resolved_config_sha256"],
        "Runtime task identity mismatch",
    )
    require(
        provenance["effective_config_sha256"]
        == config_digest(provenance["effective_native_config"]),
        "Effective native config hash mismatch",
    )
    effective = provenance["effective_native_config"]
    expected_effective = deepcopy(config)
    overrides = provenance["model_overrides"]
    require(isinstance(overrides, dict), "Malformed model override provenance")
    require(not (profile == "smoke" and overrides), "Model override in smoke evidence")
    require(set(overrides) <= set(names), "Unknown overridden player")
    for player in expected_effective["players"]:
        if player["name"] in overrides:
            require(
                player["agent"] == "mini", "Model override converted a dummy player"
            )
            player["config"]["model"]["model_name"] = overrides[player["name"]]
        if profile == "smoke":
            player["agent"] = "scripted"
            player.pop("config", None)
        elif player["agent"] == "mini":
            player["no_internet"] = False
    require(effective == expected_effective, "Unexpected native configuration change")
    initialized = deepcopy(effective)
    for player in initialized["players"]:
        player.setdefault("commit_label", "")
    require(
        provenance["initialized_native_config"] == initialized
        and provenance["initialized_config_sha256"] == config_digest(initialized),
        "Unexpected native initialization change",
    )
    require(metadata["config"] == initialized, "Native metadata/config mismatch")
    require(
        metadata["name"] == "PvpTournament"
        and metadata["game"]["name"] == manifest["arena_name"],
        "Wrong native workflow",
    )
    require(
        (summary["rounds"], summary["sims_per_round"]) == (rounds, sims),
        "Runtime budget mismatch",
    )
    require(
        effective["tournament"] == config["tournament"]
        and effective["game"] == config["game"]
        and effective["prompts"] == config["prompts"],
        "Native game, workflow or prompts changed",
    )
    require(player_names(effective) == names, "Native player identity changed")
    require(
        len(metadata["agents"]) == len(names)
        and {a["name"] for a in metadata["agents"]} == set(names),
        "Missing native player",
    )
    expected = {str(i) for i in range(rounds + 1)}
    require(
        set(metadata["round_stats"]) == set(summary["round_execution"]) == expected,
        "Incomplete native rounds",
    )
    for agent in metadata["agents"]:
        require(not agent["config"].get("push"), "Remote pushing disallowed")
        require(set(agent["round_tags"]) == expected, "Missing native code history")
        for number in range(1, rounds + 1):
            changes = read_json(
                root / "players" / agent["name"] / f"changes_r{number}.json"
            )
            require(changes["round"] == number, "Missing edit history")
    ownership = read_json(root / "containers.json")
    require(
        ownership["run_id"] == summary["run_id"] == provenance["run_id"],
        "Run identity mismatch",
    )
    owned, expected_roles = (
        ownership["containers"],
        {"arena", *(f"player:{name}" for name in names)},
    )
    require(
        len(owned) == len(names) + 1 and {c["role"] for c in owned} == expected_roles,
        "Native container inventory mismatch",
    )
    ids = {c["container_id"] for c in owned}
    require(len(ids) == len(owned), "Duplicate container identity")
    cleanup, isolation = summary["cleanup"], summary["isolation"]
    require(
        len(cleanup) == len(ids)
        and {c["container_id"] for c in cleanup} == ids
        and all(c["removed"] and c["inventory_verified"] for c in cleanup),
        "Native cleanup unverified",
    )
    require(
        len(isolation) == len(ids)
        and {c["container_id"] for c in isolation} == ids
        and {c["role"] for c in isolation} == expected_roles,
        "Native isolation inventory mismatch",
    )
    require(
        all(
            c["networks"] == [] and c["git_remotes"] == [] and c["remote_refs"] == []
            for c in isolation
        ),
        "Native isolation unverified",
    )
    reports, rewards, feedback_hashes = [], {}, {}
    prefix = "diagnostic_" if is_diagnostic(manifest) else ""
    for number in range(rounds + 1):
        stats, files, feedback_hashes[number] = read_round(root, number)
        require(
            stats == metadata["round_stats"][str(number)]
            and stats["round_num"] == number,
            "Round metadata mismatch",
        )
        execution = summary["round_execution"][str(number)]
        require(
            files == execution["files"],
            "Round evidence missing, added or modified in transport",
        )
        check_native_stats(stats, names, execution, sims)
        reports.append(
            {
                "round": number,
                "winner": stats["winner"],
                "scores": stats["scores"],
                "evidence_files": len(files),
                "execution_kind": execution["execution_kind"],
            }
        )
        if number:
            for name in names:
                rewards[f"{prefix}{name}_round_{number}_native_score"] = stats[
                    "player_stats"
                ][name]["score"]
    for name in names:
        rewards[f"{prefix}{name}_mean_native_score"] = (
            sum(
                rewards[f"{prefix}{name}_round_{n}_native_score"]
                for n in range(1, rounds + 1)
            )
            / rounds
        )
    # Preserve the native two-player tertiary metric only where it is defined.
    tertiary = len(names) == 2 and all(
        r["winner"] in {*names, "Tie"} for r in reports[1:]
    )
    if tertiary:
        for name in names:
            values = []
            for report in reports[1:]:
                value = (
                    0.5
                    if report["winner"] == "Tie"
                    else float(report["winner"] == name)
                )
                rewards[f"{prefix}{name}_round_{report['round']}_tertiary"] = value
                values.append(value)
            rewards[f"{prefix}{name}_mean_tertiary"] = sum(values) / rounds
    if profile == "smoke":
        proofs = summary["editor_assertions"]
        require(
            len(proofs) == rounds * len(names)
            and {(p["player"], p["round"]) for p in proofs}
            == {(p, r) for p in names for r in range(1, rounds + 1)},
            "Incomplete scripted edit evidence",
        )
        by_round = {(p["player"], p["round"]): p for p in proofs}
        for proof in proofs:
            require(
                proof["previous_round"] == proof["round"] - 1
                and proof["previous_marker_verified"]
                and proof["marker_added"]
                and proof["sha256_before"] != proof["sha256_after"],
                "Persistent code edit failed",
            )
            require(
                proof["feedback_sha256"] == feedback_hashes[proof["previous_round"]],
                "Wrong native feedback delivered",
            )
            if proof["round"] > 1:
                previous = by_round[(proof["player"], proof["round"] - 1)]
                require(
                    proof["sha256_before"] == previous["sha256_after"],
                    "Code state did not persist",
                )
            if config["tournament"].get("transparent", False):
                snapshots = proof.get("opponent_codebases", {})
                require(
                    set(snapshots) == set(names) - {proof["player"]},
                    "Transparent opponent inventory is incomplete",
                )
                for opponent, files in snapshots.items():
                    require(
                        isinstance(files, dict) and bool(files),
                        "Empty transparent opponent snapshot",
                    )
                    expected_opponent = by_round[(opponent, proof["round"])]
                    source_path = expected_opponent["edited_path"]
                    require(
                        any(
                            (path == source_path or path.endswith("/" + source_path))
                            and digest == expected_opponent["sha256_before"]
                            for path, digest in files.items()
                        ),
                        "Transparent snapshot is not the prior opponent code",
                    )
    return {
        "status": "verified",
        "profile": profile,
        "arena": manifest["arena"],
        "mode": "pvp",
        "diagnostic_only": bool(prefix),
        "config_origin": manifest["config_origin"],
        "arena_count": 1,
        "player_names": names,
        "edit_rounds": rounds,
        "baseline_in_metric": False,
        "oracle_pass": False,
        "parity_claim": False,
        "native_tertiary_available": tertiary,
        "transparent": config["tournament"].get("transparent", False),
        "rounds": reports,
        "rewards": rewards,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--task-config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name in ("reward.json", "reward.txt", "verification.json"):
        (args.output_dir / name).unlink(missing_ok=True)
    result = verify_evidence(args.evidence, read_json(args.task_config))
    (args.output_dir / "verification.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    (args.output_dir / "reward.json").write_text(
        json.dumps(result["rewards"], indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "status",
                    "arena",
                    "profile",
                    "diagnostic_only",
                    "oracle_pass",
                    "parity_claim",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
