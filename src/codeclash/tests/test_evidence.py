from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from codeclash.adapter import CodeClashAdapter
from codeclash.contract import config_digest, player_names
from codeclash.evidence import check_native_stats, read_round, verify_evidence


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def make_evidence(root, arena="bridge", transparent=False):
    task = CodeClashAdapter(root / "tasks", task_ids=[arena], profile="smoke").run()[0]
    manifest = json.loads((task / "environment/task-config.json").read_text())
    native = manifest["resolved_native_config"]
    if transparent:
        native["tournament"]["transparent"] = True
        manifest["resolved_config_sha256"] = config_digest(native)
    names = player_names(native)
    effective = deepcopy(native)
    for player in effective["players"]:
        player["agent"] = "scripted"
        player.pop("config", None)
    initialized = deepcopy(effective)
    for player in initialized["players"]:
        player.setdefault("commit_label", "")
    evidence = root / "evidence"
    roles = ["arena", *(f"player:{name}" for name in names)]
    owned = [
        {"role": role, "container_id": str(i), "image_id": "fixture"}
        for i, role in enumerate(roles)
    ]
    summary = {
        "status": "success",
        "run_id": "fixture",
        "arena": arena,
        "profile": "smoke",
        "rounds": 2,
        "sims_per_round": native["game"]["sims_per_round"],
        "round_execution": {},
        "cleanup": [{**c, "removed": True, "inventory_verified": True} for c in owned],
        "isolation": [
            {**c, "networks": [], "git_remotes": [], "remote_refs": []} for c in owned
        ],
        "editor_assertions": [],
    }
    provenance = {
        "arena": arena,
        "profile": "smoke",
        "run_id": "fixture",
        "upstream_commit": manifest["upstream_commit"],
        "source_dirty": False,
        "resolved_config_sha256": manifest["resolved_config_sha256"],
        "effective_native_config": effective,
        "effective_config_sha256": config_digest(effective),
        "initialized_native_config": initialized,
        "initialized_config_sha256": config_digest(initialized),
        "model_overrides": {},
    }
    metadata = {
        "config": initialized,
        "name": "PvpTournament",
        "game": {"name": manifest["arena_name"]},
        "agents": [
            {
                "name": name,
                "config": {"push": False},
                "round_tags": {str(i): f"tag{i}" for i in range(3)},
            }
            for name in names
        ],
        "round_stats": {},
    }
    for number in range(3):
        scores = {name: -2 if i % 2 else 3 for i, name in enumerate(names)}
        stats = {
            "round_num": number,
            "winner": "north/south" if len(names) == 4 else names[0],
            "scores": scores,
            "player_stats": {
                name: {
                    "score": scores[name],
                    "valid_submit": True,
                    "invalid_reason": "",
                }
                for name in names
            },
        }
        write_json(evidence / "rounds" / str(number) / "results.json", stats)
        write_json(
            evidence / "rounds" / str(number) / "nested" / "native.log",
            {"fixture": number},
        )
        _, files, result_hash = read_round(evidence, number)
        summary["round_execution"][str(number)] = {
            "execution_kind": "scored",
            "ordered_players": names,
            "scorer_players": names,
            "valid_players": names,
            "native_replay_verified": True,
            "files": files,
        }
        metadata["round_stats"][str(number)] = stats
        if number < 2:
            for name in names:
                summary["editor_assertions"].append(
                    {
                        "player": name,
                        "round": number + 1,
                        "previous_round": number,
                        "edited_path": "main.py",
                        "sha256_before": str(number),
                        "sha256_after": str(number + 1),
                        "previous_marker_verified": True,
                        "marker_added": True,
                        "feedback_sha256": result_hash,
                    }
                )
        if number:
            for name in names:
                write_json(
                    evidence / "players" / name / f"changes_r{number}.json",
                    {"round": number},
                )
    if transparent:
        for proof in summary["editor_assertions"]:
            proof["opponent_codebases"] = {
                name: {"main.py": proof["sha256_before"]}
                for name in names
                if name != proof["player"]
            }
    for filename, value in (
        ("runner-summary.json", summary),
        ("provenance.json", provenance),
        ("metadata.json", metadata),
        ("containers.json", {"run_id": "fixture", "containers": owned}),
    ):
        write_json(evidence / filename, value)
    return evidence, manifest


class EvidenceTests(unittest.TestCase):
    def test_team_score_and_negative_native_scores_are_not_reinterpreted(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence, manifest = make_evidence(Path(tmp))
            report = verify_evidence(evidence, manifest)
            self.assertFalse(report["native_tertiary_available"])
            self.assertEqual(len(report["player_names"]), 4)
            self.assertIn(-2, report["rewards"].values())
            self.assertTrue(
                all(key.startswith("diagnostic_") for key in report["rewards"])
            )
            self.assertFalse(
                any("round_0" in key or "tertiary" in key for key in report["rewards"])
            )
            self.assertFalse(report["oracle_pass"] or report["parity_claim"])

    def test_transparent_snapshots_must_show_every_prior_opponent(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence, manifest = make_evidence(Path(tmp), transparent=True)
            self.assertTrue(verify_evidence(evidence, manifest)["transparent"])
            change_json(
                evidence,
                "runner-summary.json",
                lambda d: d["editor_assertions"][0]["opponent_codebases"].pop("south"),
            )
            with self.assertRaisesRegex(ValueError, "opponent inventory"):
                verify_evidence(evidence, manifest)
        with tempfile.TemporaryDirectory() as tmp:
            evidence, manifest = make_evidence(Path(tmp), transparent=True)
            change_json(
                evidence,
                "runner-summary.json",
                lambda d: d["editor_assertions"][0]["opponent_codebases"][
                    "south"
                ].update({"main.py": "stale"}),
            )
            with self.assertRaisesRegex(ValueError, "prior opponent code"):
                verify_evidence(evidence, manifest)

    def test_two_player_tertiary_is_separate_from_native_raw_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence, manifest = make_evidence(Path(tmp), "battlesnake")
            report = verify_evidence(evidence, manifest)
            name = report["player_names"][0]
            self.assertEqual(report["rewards"][f"diagnostic_{name}_mean_tertiary"], 1)
            self.assertEqual(
                report["rewards"][f"diagnostic_{name}_mean_native_score"], 3
            )

    def test_incomplete_or_altered_evidence_fails_closed(self):
        mutations = {
            "missing log": lambda root: (root / "rounds/1/nested/native.log").unlink(),
            "modified log": lambda root: (
                root / "rounds/1/nested/native.log"
            ).write_text("changed"),
            "added log": lambda root: (root / "rounds/1/extra").write_text("extra"),
            "round absent": lambda root: (root / "rounds/2/results.json").unlink(),
            "cleanup": lambda root: change_json(
                root,
                "runner-summary.json",
                lambda d: d["cleanup"][0].update(removed=False),
            ),
            "network": lambda root: change_json(
                root,
                "runner-summary.json",
                lambda d: d["isolation"][0].update(networks=["bridge"]),
            ),
            "feedback": lambda root: change_json(
                root,
                "runner-summary.json",
                lambda d: d["editor_assertions"][0].update(feedback_sha256="bad"),
            ),
            "persistence": lambda root: change_json(
                root,
                "runner-summary.json",
                lambda d: d["editor_assertions"][-1].update(sha256_before="lost"),
            ),
            "config": lambda root: change_json(
                root,
                "provenance.json",
                lambda d: d.update(effective_config_sha256="bad"),
            ),
            "scorer": lambda root: change_json(
                root,
                "runner-summary.json",
                lambda d: d["round_execution"]["1"].update(
                    native_replay_verified=False
                ),
            ),
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                evidence, manifest = make_evidence(Path(tmp))
                mutation(evidence)
                with self.assertRaises(ValueError):
                    verify_evidence(evidence, manifest)

    def test_tar_paths_and_links_rejected_without_extraction(self):
        for path, kind in (
            ("../../escape", tarfile.REGTYPE),
            ("0/link", tarfile.SYMTYPE),
        ):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "rounds").mkdir()
                with tarfile.open(root / "rounds/round_0.tar.gz", "w:gz") as archive:
                    for name, data, type_ in (
                        ("0/results.json", b'{"round_num":0}', tarfile.REGTYPE),
                        (path, b"bad", kind),
                    ):
                        member = tarfile.TarInfo(name)
                        member.type = type_
                        member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))
                with self.assertRaises(ValueError):
                    read_round(root, 0)
                self.assertFalse((root / "escape").exists())

    def test_archive_preserves_nested_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rounds").mkdir()
            with tarfile.open(root / "rounds/round_0.tar.gz", "w:gz") as archive:
                for name, data in (
                    ("0/results.json", b'{"round_num":0}'),
                    ("0/replay/nested.log", b"native"),
                ):
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            stats, files, result_hash = read_round(root, 0)
            self.assertEqual(stats, {"round_num": 0})
            self.assertEqual(
                files["replay/nested.log"], hashlib.sha256(b"native").hexdigest()
            )
            self.assertEqual(result_hash, files["results.json"])

    def test_native_all_invalid_and_single_valid_forfeit(self):
        names = ["a", "b", "c", "d"]
        for valid in ([], ["c"]):
            scores = {
                name: (3 if name in valid else 0) if valid else 0.75 for name in names
            }
            stats = {
                "scores": scores,
                "winner": valid[0] if valid else "Tie",
                "player_stats": {
                    name: {
                        "valid_submit": name in valid,
                        "score": scores[name],
                        "invalid_reason": "" if name in valid else "missing source",
                    }
                    for name in names
                },
            }
            execution = {
                "native_replay_verified": True,
                "valid_players": valid,
                "ordered_players": names,
                "execution_kind": "validation_forfeit" if valid else "all_invalid",
            }
            check_native_stats(stats, names, execution, 3)
            stats["scores"]["a"] += 1
            with self.assertRaises(ValueError):
                check_native_stats(stats, names, execution, 3)


def change_json(root, filename, mutate):
    path = root / filename
    data = json.loads(path.read_text())
    mutate(data)
    write_json(path, data)
