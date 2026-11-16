#!/usr/bin/env python3
"""Local native-validator controls. No model, benchmark reward or task edits."""

import argparse
import concurrent.futures
import copy
import hashlib
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import time
import traceback
from types import SimpleNamespace
import uuid

PIN = "f0694c64ecf6abfca2bc867bad2de9333fef5be8"
LABEL = "org.harbor.codeclash.reference-run"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def call(args, timeout=60, check=True):
    r = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
    )
    if check and r.returncode:
        raise RuntimeError(
            f"{args[:3]} exited {r.returncode}: {r.stderr.decode(errors='replace')[-3000:]}"
        )
    return r


def source_hashes():
    values = {
        "catalog.json": digest(CATALOG.read_bytes()),
        "reference-case-plan.json": digest(PLAN.read_bytes()),
        "run_native_references.py": digest(Path(__file__).read_bytes()),
    }
    values.update(
        {
            str(p.relative_to(UPSTREAM)): digest(p.read_bytes())
            for p in sorted((UPSTREAM / "codeclash").rglob("*.py"))
        }
    )
    return values


def worker(arena_id):
    out = OUT / arena_id / "attempt-1"
    out.mkdir(parents=True, exist_ok=False)
    # Disable host dotenv/provider state BEFORE importing native or mini packages.
    for key in list(os.environ):
        if any(
            s in key.upper()
            for s in (
                "API_KEY",
                "TOKEN",
                "SECRET",
                "CREDENTIAL",
                "PRIVATE_KEY",
                "ACCESS_KEY",
            )
        ):
            os.environ.pop(key, None)
    os.environ.update(
        PYTHON_DOTENV_DISABLED="1",
        MSWEA_GLOBAL_CONFIG_DIR=str(out / "empty-global-config"),
        MSWEA_SILENT_STARTUP="1",
        MPLCONFIGDIR=str(out / "mpl"),
        COPYFILE_DISABLE="1",
    )
    cat = json.loads(CATALOG.read_text())
    arena = next(a for a in cat["arenas"] if a["id"] == arena_id)
    plans = json.loads(PLAN.read_text())
    plan = next(p for p in plans if p["arena"] == arena_id)
    config = copy.deepcopy(arena["native_config"])
    run_id = "ref-" + uuid.uuid4().hex
    owned = []
    environments = []
    report = {
        "schema_version": 1,
        "arena": arena_id,
        "run_id": run_id,
        "attempt": 1,
        "status": "running",
        "started_at": time.time(),
        "upstream_commit": PIN,
        "config_origin": arena["config_origin"],
        "native_config": config,
        "reference_source": arena["workspace_source"],
        "checks": [],
        "cleanup": [],
        "model_api_calls": 0,
        "competitive_oracle_pass": False,
        "parity_claim": False,
        "scope": "Native validator positive/negative/restoration and native forfeit branch references",
        "expected_forfeit_tournament_simulations": 0,
        "simulation_scope": "Forfeit controls skip tournament execute_round; native validate_code may run real engine probes, which are not counted as tournament simulations.",
        "packaging": "Native arena constructors, validators and run_round are invoked with CodeArena.get_environment replaced by an offline credential-free Docker wrapper; lightweight player records supply only name/environment.",
    }

    def checkpoint():
        save(out / "report.json", report)
        save(out / "containers.json", {"run_id": run_id, "containers": owned})

    def check(name, expected, observed, passed):
        report["checks"].append(
            {
                "name": name,
                "expected": expected,
                "observed": observed,
                "passed": bool(passed),
            }
        )
        checkpoint()
        if not passed:
            raise AssertionError(name)

    try:
        assert (
            call(["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"])
            .stdout.decode()
            .strip()
            == PIN
        )
        assert not call(
            [
                "git",
                "-C",
                str(UPSTREAM),
                "status",
                "--porcelain",
                "--",
                "codeclash",
                "configs",
            ]
        ).stdout.strip()
        tag = f"codeclash-adapter/{arena_id}:{PIN[:12]}"
        image = json.loads(call(["docker", "image", "inspect", tag]).stdout)[0]
        image_id = image["Id"]
        assert image["Config"]["Labels"]["org.codeclash.source-revision"] == PIN
        assert image["Config"]["Labels"]["org.harbor.codeclash.arena"] == arena_id
        report["image"] = {
            "tag": tag,
            "id": image_id,
            "architecture": image["Architecture"],
            "os": image["Os"],
        }
        sys.path.insert(0, str(UPSTREAM))
        import codeclash
        from codeclash.arenas.arena import CodeArena

        assert Path(codeclash.__file__).resolve() == UPSTREAM / "codeclash/__init__.py"
        cls = getattr(importlib.import_module(arena["module"]), arena["class_name"])
        report["native_implementation"] = {
            "validator": f"{cls.validate_code.__module__}.{cls.validate_code.__qualname__}",
            "validator_source_sha256": digest(
                inspect.getsource(cls.validate_code).encode()
            ),
            "validator_file_sha256": digest(
                Path(inspect.getfile(cls.validate_code)).read_bytes()
            ),
            "run_round_source_sha256": digest(
                inspect.getsource(CodeArena.run_round).encode()
            ),
        }

        class Environment:
            def __init__(self, role):
                self.role = role
                self.config = SimpleNamespace(cwd="/workspace")
                name = f"{run_id[:20]}-{arena_id}-{len(owned)}"
                r = call(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        name,
                        "--network",
                        "none",
                        "--cpus",
                        "2",
                        "--memory",
                        "4g",
                        "--pids-limit",
                        "512",
                        "--security-opt",
                        "no-new-privileges",
                        "--label",
                        f"{LABEL}={run_id}",
                        "--label",
                        f"org.harbor.codeclash.reference-arena={arena_id}",
                        "--workdir",
                        "/workspace",
                        "--entrypoint",
                        "/bin/sh",
                        image_id,
                        "-c",
                        "sleep 1200",
                    ]
                )
                self.container_id = r.stdout.decode().strip()
                owned.append(
                    {
                        "container_id": self.container_id,
                        "role": role,
                        "image_id": image_id,
                    }
                )
                environments.append(self)
                checkpoint()
                info = json.loads(
                    call(["docker", "container", "inspect", self.container_id]).stdout
                )[0]
                assert (
                    info["Image"] == image_id
                    and info["HostConfig"]["NetworkMode"] == "none"
                )
                assert not info["Mounts"]
                for item in info["Config"].get("Env", []):
                    key, _, val = item.partition("=")
                    assert not (
                        val
                        and any(
                            s in key.upper()
                            for s in (
                                "API_KEY",
                                "TOKEN",
                                "SECRET",
                                "CREDENTIAL",
                                "PRIVATE_KEY",
                                "ACCESS_KEY",
                            )
                        )
                    ), key
                head = self.execute("git rev-parse HEAD")["output"].strip()
                tree = self.execute("git rev-parse 'HEAD^{tree}'")["output"].strip()
                source = arena["workspace_source"]
                assert (
                    head == source["commit"]
                    if source["kind"] == "repository"
                    else tree == source["tree"]
                )
                owned[-1].update(
                    workspace_head=head,
                    workspace_tree=tree,
                    network="none",
                    mounts=[],
                    credentials="none",
                )
                checkpoint()

            def execute(self, action, cwd="", *, timeout=None):
                command = action if isinstance(action, str) else action["command"]
                started = time.time()
                record = {
                    "role": self.role,
                    "command": command,
                    "cwd": str(cwd or self.config.cwd),
                    "timeout": min(timeout or 120, 180),
                    "started_at": started,
                }
                try:
                    r = call(
                        [
                            "docker",
                            "exec",
                            "-w",
                            record["cwd"],
                            self.container_id,
                            "/bin/bash",
                            "-c",
                            command,
                        ],
                        timeout=record["timeout"],
                        check=False,
                    )
                    result = {
                        "returncode": r.returncode,
                        "output": (r.stdout + r.stderr).decode(errors="replace"),
                    }
                    record.update(result)
                    return result
                except subprocess.TimeoutExpired as e:
                    record.update(
                        exception="TimeoutExpired",
                        output=(e.stdout or b"").decode(errors="replace"),
                    )
                    raise
                finally:
                    record["elapsed_seconds"] = time.time() - started
                    with (out / "commands.jsonl").open("a") as stream:
                        stream.write(json.dumps(record) + "\n")

        CodeArena.get_environment = lambda self, branch_name=None: Environment(
            "arena" if branch_name is None else str(branch_name)
        )
        game = cls(
            config,
            tournament_id=run_id,
            local_output_dir=out / "native",
            keep_containers=True,
        )
        players = [
            SimpleNamespace(
                name=p["name"], environment=Environment("player:" + p["name"])
            )
            for p in config["players"]
        ]
        primary = players[0]
        entries = list(
            dict.fromkeys(
                [arena["submission"].rstrip("/"), *plan["negative_remove_paths"]]
            )
        )
        # Keep only existing non-overlapping paths in the pristine reference archive.
        entries = [
            p
            for p in entries
            if not any(p.startswith(q + "/") for q in entries if p != q)
        ]
        existing = [
            p
            for p in entries
            if primary.environment.execute(f"test -e {shlex.quote(p)}")["returncode"]
            == 0
        ]
        assert existing
        command = "tar -cf - -- " + shlex.join(existing)
        archive = call(
            [
                "docker",
                "exec",
                "-w",
                "/workspace",
                primary.environment.container_id,
                "/bin/bash",
                "-c",
                command,
            ],
            timeout=120,
        ).stdout
        (out / "pristine-submission.tar").write_bytes(archive)
        members = []
        with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
            for member in tf:
                if member.isfile():
                    members.append(
                        {
                            "path": member.name,
                            "size": member.size,
                            "sha256": digest(tf.extractfile(member).read()),
                        }
                    )
                elif member.issym():
                    members.append({"path": member.name, "symlink": member.linkname})
        report["reference_bytes"] = {
            "archive": "pristine-submission.tar",
            "sha256": digest(archive),
            "members": members,
        }
        save(out / "reference-bytes.json", report["reference_bytes"])
        # Backup original paths outside the workspace before compiler/runtime checks.
        backup = "/reference-backup/pristine"
        for p in existing:
            parent = str(Path(backup) / Path(p).parent)
            r = primary.environment.execute(
                f"mkdir -p {shlex.quote(parent)} && cp -a -- {shlex.quote(p)} {shlex.quote(str(Path(backup) / p))}"
            )
            assert r["returncode"] == 0
        valid, reason = game.validate_code(primary)
        check(
            "pristine_native_validator",
            {"valid": True},
            {"valid": valid, "reason": reason},
            valid is True,
        )
        for p in plan["negative_remove_paths"]:
            assert (
                primary.environment.execute("rm -rf -- " + shlex.quote(p))["returncode"]
                == 0
            )
        valid, reason = game.validate_code(primary)
        check(
            "missing_entrypoint_native_validator",
            {"valid": False, "nonempty_reason": True},
            {"valid": valid, "reason": reason},
            valid is False and bool(reason),
        )
        for p in existing:
            assert (
                primary.environment.execute("rm -rf -- " + shlex.quote(p))["returncode"]
                == 0
            )
            assert (
                primary.environment.execute(
                    f"mkdir -p {shlex.quote(str(Path(p).parent))} && cp -a -- {shlex.quote(str(Path(backup) / p))} {shlex.quote(p)}"
                )["returncode"]
                == 0
            )
        restoration = []
        for member in members:
            if "sha256" in member:
                b = call(
                    [
                        "docker",
                        "exec",
                        "-w",
                        "/workspace",
                        primary.environment.container_id,
                        "cat",
                        "--",
                        member["path"],
                    ]
                ).stdout
                restoration.append(
                    {
                        "path": member["path"],
                        "sha256": digest(b),
                        "matches": digest(b) == member["sha256"],
                    }
                )
            else:
                r = primary.environment.execute(
                    "readlink -- " + shlex.quote(member["path"])
                )
                restoration.append(
                    {
                        "path": member["path"],
                        "symlink": r["output"].strip(),
                        "matches": r["output"].strip() == member["symlink"],
                    }
                )
        check(
            "restored_reference_bytes",
            {"all_match": True},
            restoration,
            all(r["matches"] for r in restoration),
        )
        valid, reason = game.validate_code(primary)
        check(
            "restored_native_validator",
            {"valid": True},
            {"valid": valid, "reason": reason},
            valid is True,
        )
        for player in players[1:]:
            for p in plan["negative_remove_paths"]:
                assert (
                    player.environment.execute("rm -rf -- " + shlex.quote(p))[
                        "returncode"
                    ]
                    == 0
                )
        ordered = list(players)
        one = game.run_round(ordered, 1, copy_logs=False).to_dict()
        expected = {
            p.name: (config["game"]["sims_per_round"] if p is primary else 0)
            for p in players
        }
        passed = one["winner"] == primary.name and one["scores"] == expected
        passed &= all(
            s["valid_submit"] == (name == primary.name)
            and (s["valid_submit"] or bool(s["invalid_reason"]))
            for name, s in one["player_stats"].items()
        )
        check(
            "native_one_valid_forfeit",
            {"winner": primary.name, "scores": expected, "tournament_simulations": 0},
            one,
            passed,
        )
        for p in plan["negative_remove_paths"]:
            assert (
                primary.environment.execute("rm -rf -- " + shlex.quote(p))["returncode"]
                == 0
            )
        none = game.run_round(list(players), 2, copy_logs=False).to_dict()
        expected = {
            p.name: config["game"]["sims_per_round"] / len(players) for p in players
        }
        passed = none["winner"] == "Tie" and none["scores"] == expected
        passed &= all(
            not s["valid_submit"] and bool(s["invalid_reason"])
            for s in none["player_stats"].values()
        )
        check(
            "native_all_invalid_tie",
            {"winner": "Tie", "scores": expected, "tournament_simulations": 0},
            none,
            passed,
        )
        report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        (out / "failure.txt").write_text(traceback.format_exc())
    finally:
        for record in owned:
            cid = record["container_id"]
            row = {"container_id": cid, "role": record["role"], "removed": False}
            try:
                found = call(["docker", "container", "inspect", cid], check=False)
                if found.returncode == 0:
                    info = json.loads(found.stdout)[0]
                    assert info["Config"]["Labels"][LABEL] == run_id
                    assert info["Image"] == record["image_id"]
                    call(["docker", "rm", "-f", cid], timeout=60)
                row["removed"] = (
                    call(
                        ["docker", "container", "inspect", cid], check=False
                    ).returncode
                    != 0
                )
            except BaseException as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            report["cleanup"].append(row)
        report["cleanup_verified"] = all(x["removed"] for x in report["cleanup"])
        if not report["cleanup_verified"]:
            report["status"] = "failed"
        report["finished_at"] = time.time()
        report["elapsed_seconds"] = report["finished_at"] - report["started_at"]
        checkpoint()
    print(
        json.dumps(
            {
                "arena": arena_id,
                "status": report["status"],
                "checks": len(report["checks"]),
                "elapsed_seconds": round(report["elapsed_seconds"], 1),
            }
        ),
        flush=True,
    )
    return 0 if report["status"] == "passed" else 1


def main():
    global UPSTREAM, CATALOG, PLAN, OUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    parser.add_argument("--upstream-repo", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    UPSTREAM, CATALOG, PLAN, OUT = (
        p.resolve()
        for p in (args.upstream_repo, args.catalog, args.plan, args.output_dir)
    )
    if args.worker:
        return worker(args.worker)
    cat = json.loads(CATALOG.read_text())
    if not cat["arenas"]:
        raise ValueError("Reference catalog must contain at least one arena")
    existing = [
        a["id"] for a in cat["arenas"] if (OUT / a["id"] / "attempt-1").exists()
    ]
    if existing:
        raise ValueError(
            f"Refusing existing attempt directories: {existing}; use a fresh output directory"
        )
    OUT.mkdir(parents=True, exist_ok=True)
    hashes = source_hashes()
    save(OUT / "source-hashes-before.json", hashes)

    def launch(arena):
        aid = arena["id"]
        with (OUT / f"{aid}.log").open("w") as log:
            try:
                r = subprocess.run(
                    [
                        sys.executable,
                        __file__,
                        "--worker",
                        aid,
                        "--upstream-repo",
                        str(UPSTREAM),
                        "--catalog",
                        str(CATALOG),
                        "--plan",
                        str(PLAN),
                        "--output-dir",
                        str(OUT),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=900,
                )
                result = {"arena": aid, "process_exit": r.returncode}
            except subprocess.TimeoutExpired:
                result = {
                    "arena": aid,
                    "process_exit": None,
                    "error": "worker timeout 900s",
                }
                manifest_path = OUT / aid / "attempt-1/containers.json"
                if manifest_path.exists():
                    manifest = json.loads(manifest_path.read_text())
                    cleanup = []
                    for row in manifest["containers"]:
                        cid = row["container_id"]
                        found = call(
                            ["docker", "container", "inspect", cid], check=False
                        )
                        if found.returncode == 0:
                            info = json.loads(found.stdout)[0]
                            assert info["Config"]["Labels"][LABEL] == manifest["run_id"]
                            assert info["Image"] == row["image_id"]
                            call(["docker", "rm", "-f", cid])
                        cleanup.append(
                            {
                                "container_id": cid,
                                "removed": call(
                                    ["docker", "container", "inspect", cid], check=False
                                ).returncode
                                != 0,
                            }
                        )
                    result["timeout_cleanup"] = cleanup
                    path = OUT / aid / "attempt-1/report.json"
                    report = (
                        json.loads(path.read_text())
                        if path.exists()
                        else {"arena": aid}
                    )
                    report.update(
                        status="failed",
                        error="worker timeout 900s",
                        cleanup=cleanup,
                        cleanup_verified=all(x["removed"] for x in cleanup),
                    )
                    save(path, report)
        print(json.dumps(result), flush=True)
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        processes = list(pool.map(launch, cat["arenas"]))
    reports = []
    for a in cat["arenas"]:
        p = OUT / a["id"] / "attempt-1/report.json"
        reports.append(
            json.loads(p.read_text())
            if p.exists()
            else {"arena": a["id"], "status": "missing-report"}
        )
    after = source_hashes()
    save(OUT / "source-hashes-after.json", after)
    result = {
        "schema_version": 1,
        "scope": "Native reference controls, not competitive oracle or model parity",
        "arena_count": len(reports),
        "passed_arenas": sum(x["status"] == "passed" for x in reports),
        "checks": sum(len(x.get("checks", [])) for x in reports),
        "passed_checks": sum(c["passed"] for x in reports for c in x.get("checks", [])),
        "source_files_unchanged": hashes == after,
        "model_api_calls": 0,
        "competitive_oracle_pass": False,
        "parity_claim": False,
        "processes": processes,
        "arenas": reports,
    }
    result["status"] = (
        "passed"
        if result["passed_arenas"] == len(reports)
        and hashes == after
        and all(p["process_exit"] == 0 for p in processes)
        else "failed"
    )
    save(OUT / "summary.json", result)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("arenas", "processes")}
        ),
        flush=True,
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
