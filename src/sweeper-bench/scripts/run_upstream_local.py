#!/usr/bin/env python3
"""Run the unmodified upstream SWEeper-Bench worker on local Docker.

The upstream runner (https://github.com/zlab-princeton/sweeper-bench) executes
each case inside a Modal VM: an ubuntu:24.04 image with Docker and the repo's
src/runtime/ directory, booted with runtime/install.sh, then driven by
runtime/worker.py reading /task.json. This script builds the same VM image and
runs it as a privileged Docker-in-Docker container instead of a Modal sandbox.
src/runtime/ is used as-is from the given runner checkout; only the Modal
transport is replaced. Like upstream, prediction and evaluation run in separate
fresh VMs.

Modes:
  --mode evaluate   evaluation stage only, for --phases baseline / reference
                    (used by the pilot's original-side checks)
  --mode run        prediction stage (agent writes a patch), then evaluation of
                    that patch (phase prediction-scoped); used for parity
  --mode predict    prediction stage only (the patch is kept under --out)
  --mode score      evaluation of patches saved by an earlier --mode predict

Examples:
    # pilot: unpatched app and upstream fix
    python scripts/run_upstream_local.py --runner ~/sweeper-bench --dataset-file test.jsonl \
        --cases sweeper-001 --mode evaluate --phases baseline reference \
        --reference-patch-dir patches/ --out runs/pilot
    # parity: Gemini CLI on Vertex AI (fork at harbor-parity), Codex verifier on a subscription
    GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=global GEMINI_VERTEX_ADC_FILE=<adc.json> \
    CODEX_AUTH_FILE=~/.codex/auth.json python scripts/run_upstream_local.py \
        --runner <fork checkout at harbor-parity> --dataset-file test.jsonl \
        --cases sweeper-001 --mode run --auth subscription --pred-harness gemini-cli \
        --pred-provider vertex --pred-model gemini-3.8-flash --pred-effort high --out runs/parity-r1
Credentials come from the environment, as in the upstream CLI: GHCR_TOKEN,
GHCR_USER; for the verifier either OPENAI_API_KEY (+ optional OPENAI_BASE_URL;
--auth api) or CODEX_AUTH_FILE pointing at a Codex auth.json (--auth
subscription); for the agent GEMINI_API_KEY (or --gemini-key-file to spread
cases over several keys), or GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION and
GEMINI_VERTEX_ADC_FILE for --pred-provider vertex.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VM_DOCKERFILE = """\
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \\
    docker.io docker-buildx ca-certificates python3 python3-yaml git curl iptables iproute2 \\
    && rm -rf /var/lib/apt/lists/*
COPY runtime/ /runtime/
"""


def sh(args, **kw):
    print("+", " ".join(str(a) for a in args)[:300], flush=True)
    return subprocess.run(args, **kw)


def build_vm_image(runner: Path, tag: str) -> None:
    # The tag carries the runner commit, so an existing image already matches.
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
        print(f"VM image {tag} already present", flush=True)
        return
    with tempfile.TemporaryDirectory() as tmp:
        ctx = Path(tmp)
        (ctx / "Dockerfile").write_text(VM_DOCKERFILE)
        sh(["cp", "-a", str(runner / "src" / "runtime"), str(ctx / "runtime")], check=True)
        sh(["docker", "build", "-q", "-t", tag, str(ctx)], check=True)


def payload(stage: str, row: dict, args, phases: list[str]) -> dict:
    """Same shape as Backend._exec_case() in upstream src/modal_vm.py."""
    if stage == "prediction":  # upstream src/data.py:pred_row()
        prow = {
            "id": row["case_id"],
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "task": {"scoped": str(row["task_scoped"]).strip()},
        }
    else:  # upstream src/data.py:eval_row()
        prow = {
            "id": row["case_id"],
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "reference_commit": row.get("reference_commit") or "",
            "workflows": row["workflows"],
        }
    return {
        "stage": stage,
        "auth": args.auth,
        "row": prow,
        "prediction": {
            "harness": args.pred_harness,
            "provider": args.pred_provider,
            "model": args.pred_model,
            "effort": args.pred_effort,
            "level": "scoped",
            "time_budget": "unlimited",
            "prompt_templates": "browser",
        },
        "evaluation": {
            "harness": args.eval_harness,
            "provider": "openai",
            "model": args.eval_model,
            "effort": args.eval_effort,
            "phases": phases,
            "categories": "target,preservation",
            "record": "0",
        },
    }


def vm_env(args) -> dict[str, str]:
    """Same variables as Backend.create() + host_credentials() upstream."""
    env = {
        "RUNTIME": "/runtime",
        "GHCR_REGISTRY": "ghcr.io",
        "GHCR_OWNER": "evigbyen",
        "GHCR_TAG": "alpha",
        "GHCR_USER": os.environ.get("GHCR_USER", "evigbyen"),
        "GHCR_TOKEN": os.environ["GHCR_TOKEN"],
        "PRED_EGRESS": "1",
        "EVAL_DROP_CACHES": "1",
        "EVAL_SKIP_FAILED_TASKS": "1",
    }
    if args.auth == "subscription":
        env["CODEX_AUTH_JSON"] = Path(os.environ["CODEX_AUTH_FILE"]).expanduser().read_text()
    else:
        for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
            if os.environ.get(name):
                env[name] = os.environ[name]
    # Prediction credentials present on the host are forwarded (upstream host_credentials()).
    for name in ("GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    if os.environ.get("GEMINI_VERTEX_ADC_FILE"):  # provider=vertex (parity fork)
        adc = json.loads(Path(os.environ["GEMINI_VERTEX_ADC_FILE"]).expanduser().read_text())
        env["GEMINI_VERTEX_ADC_JSON"] = json.dumps(adc, separators=(",", ":"))
    return env


def case_key(args, cid: str) -> str | None:
    """Optional per-case GEMINI_API_KEY from --gemini-key-file (one key per line, index = case order + offset)."""
    if not args.gemini_key_file:
        return None
    keys = [k.strip() for k in Path(args.gemini_key_file).read_text().splitlines() if k.strip()]
    return keys[(args.gemini_key_offset + args.cases.index(cid)) % len(keys)]


def run_vm(stage: str, row: dict, args, vm_tag: str, dest: Path, phases: list[str], incoming: dict[str, Path], timeout: int) -> int:
    """One fresh VM: boot, write /task.json and /incoming, run worker.py, collect /artifacts.tgz."""
    cid = row["case_id"]
    dest.mkdir(parents=True, exist_ok=True)
    name = f"sweeper-upstream-{stage[:4]}-{cid}-{args.run_tag}-{os.getpid()}"
    env = vm_env(args)
    key = case_key(args, cid)
    if key:
        env["GEMINI_API_KEY"] = key
    envfile = dest / ".vm.env"
    envfile.touch(mode=0o600)
    envfile.write_text("".join(f"{k}={v}\n" for k, v in env.items() if "\n" not in v))
    multiline = {k: v for k, v in env.items() if "\n" in v}
    code = 1
    try:
        sh(["docker", "rm", "-f", name], capture_output=True)
        sh(
            [
                "docker", "run", "-d", "--privileged", "--name", name,
                "--cpus", str(args.cpus), "--memory", f"{args.memory_mb}m",
                "--env-file", str(envfile),
                # Image cache only; the VM's containers and /work start empty.
                "-v", f"sweeper-upstream-docker-{cid}-{stage}-{args.run_tag}:/var/lib/docker",
                "-v", f"sweeper-upstream-ctd-{cid}-{stage}-{args.run_tag}:/var/lib/containerd",
                vm_tag, "sleep", "infinity",
            ],
            check=True,
        )
        envfile.unlink()
        # Multi-line values (the Codex auth JSON) cannot go through --env-file.
        for key, value in multiline.items():
            sh(["docker", "exec", "-i", name, "bash", "-c", f"cat > /run/{key}"], input=value.encode(), check=True)
        prefix = " ".join(f'{k}="$(cat /run/{k})"' for k in multiline)
        with (dest / "boot.log").open("wb") as log:
            sh(["docker", "exec", name, "bash", "-c", f"{prefix} bash /runtime/install.sh"], stdout=log, stderr=subprocess.STDOUT, check=True)
        # Leftover product/agent containers from a previous run of this VM volume.
        sh(["docker", "exec", name, "bash", "-c", "docker ps -aq | xargs -r docker rm -f"], capture_output=True)
        sh(["docker", "exec", name, "bash", "-lc", "rm -rf /incoming /artifacts.tgz /task.json /work && mkdir -p /incoming"], check=True)
        sh(["docker", "exec", "-i", name, "bash", "-c", "cat > /task.json"], input=json.dumps(payload(stage, row, args, phases), ensure_ascii=False).encode(), check=True)
        for fname, path in incoming.items():
            sh(["docker", "exec", "-i", name, "bash", "-c", f"cat > /incoming/{fname}"], input=path.read_bytes(), check=True)
        with (dest / "worker.stdout.log").open("wb") as out, (dest / "worker.stderr.log").open("wb") as err:
            proc = sh(
                ["timeout", str(timeout), "docker", "exec", name, "bash", "-c", f"{prefix} python3 /runtime/worker.py"],
                stdout=out, stderr=err,
            )
        code = proc.returncode
        art = dest / "artifacts.tgz"
        if sh(["docker", "cp", f"{name}:/artifacts.tgz", str(art)]).returncode == 0:
            with tarfile.open(art) as tar:
                tar.extractall(dest / "artifacts", filter="data")
    finally:
        envfile.unlink(missing_ok=True)
        if not args.keep_vm:
            sh(["docker", "rm", "-f", name], capture_output=True)
    return code


def cells(dest: Path, phase: str) -> dict:
    out = {}
    result = dest / "artifacts" / "logs" / f"{phase}-result.json"
    if result.is_file():
        data = json.loads(result.read_text())
        for cat in ("target", "preservation"):
            cell = data.get(cat) or {}
            out[cat] = {k: cell.get(k) for k in ("pass", "fail", "uncertain")}
    return out


def solved(c: dict) -> bool | None:
    if set(c) != {"target", "preservation"}:
        return None
    return all(c[k]["pass"] and not c[k]["fail"] and not c[k]["uncertain"] for k in c)


def run_case(row: dict, args, vm_tag: str) -> dict:
    cid = row["case_id"]
    root = Path(args.out) / cid
    started = time.time()
    status: dict = {"case": cid}
    if args.mode == "evaluate":
        incoming = {}
        if "reference" in args.phases:
            incoming["reference.patch"] = Path(args.reference_patch_dir) / f"{cid}.patch"
        status["worker_exit"] = run_vm("evaluation", row, args, vm_tag, root, args.phases, incoming, args.eval_timeout)
        for phase in args.phases:
            status[phase] = cells(root, phase)
    else:
        pred_dir, eval_dir = root / "prediction", root / "evaluation"
        if args.mode == "score":
            # Score a patch saved by an earlier --mode predict run.
            prev = json.loads((root / "status.json").read_text()) if (root / "status.json").is_file() else {}
            status.update({k: prev[k] for k in ("prediction_exit", "prediction_seconds") if k in prev})
        else:
            status["prediction_exit"] = run_vm("prediction", row, args, vm_tag, pred_dir, [], {}, args.pred_timeout)
            status["prediction_seconds"] = round(time.time() - started)
        patch = pred_dir / "artifacts" / "prediction-scoped.patch"
        if args.mode == "predict":
            ok = patch.is_file() and "diff --git " in patch.read_text(errors="replace")
            status["patch"] = "ok" if ok else "missing_or_empty"
            if not ok:
                status["solved"] = False
        elif patch.is_file() and "diff --git " in patch.read_text(errors="replace"):
            status["patch"] = "ok"
            status["evaluation_exit"] = run_vm(
                "evaluation", row, args, vm_tag, eval_dir, ["prediction-scoped"],
                {"prediction-scoped.patch": patch}, args.eval_timeout,
            )
            c = cells(eval_dir, "prediction-scoped")
            status["prediction-scoped"] = c
            status["solved"] = solved(c)
        else:
            # Upstream: an empty or missing patch is not evaluated (case `skipped`).
            status["patch"] = "missing_or_empty"
            status["solved"] = False
    status["seconds"] = round(time.time() - started)
    root.mkdir(parents=True, exist_ok=True)
    (root / "status.json").write_text(json.dumps(status, indent=2) + "\n")
    print(json.dumps(status), flush=True)
    return status


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--runner", type=Path, required=True, help="checkout of the upstream runner (or the parity fork)")
    p.add_argument("--dataset-file", type=Path, required=True)
    p.add_argument("--cases", nargs="+", required=True)
    p.add_argument("--mode", choices=["evaluate", "run", "predict", "score"], default="evaluate",
                   help="run = predict + score; predict = agent only (patch saved); score = verify saved patches")
    p.add_argument("--phases", nargs="+", default=["baseline", "reference"], help="--mode evaluate only")
    p.add_argument("--reference-patch-dir", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--auth", choices=["api", "subscription"], default="api")
    p.add_argument("--pred-harness", default="codex")
    p.add_argument("--pred-provider", default="openai")
    p.add_argument("--gemini-key-file", default=None, help="spread GEMINI_API_KEY over cases (one key per line)")
    p.add_argument("--gemini-key-offset", type=int, default=0)
    p.add_argument("--pred-model", default="gpt-6-astra")
    p.add_argument("--pred-effort", default="xhigh")
    p.add_argument("--eval-harness", default="codex")
    p.add_argument("--eval-model", default="gpt-5.6-luna")
    p.add_argument("--eval-effort", default="xhigh")
    p.add_argument("--cpus", type=int, default=8)
    p.add_argument("--memory-mb", type=int, default=16384)
    p.add_argument("--pred-timeout", type=int, default=28800)
    p.add_argument("--eval-timeout", type=int, default=10800)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--keep-vm", action="store_true")
    p.add_argument("--run-tag", default="r0", help="keeps VM volumes of concurrent runs apart")
    args = p.parse_args()

    rows = {json.loads(l)["case_id"]: json.loads(l) for l in args.dataset_file.read_text().splitlines() if l.strip()}
    commit = subprocess.run(["git", "-C", str(args.runner), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    print(f"runner commit {commit}", flush=True)
    vm_tag = f"sweeper-upstream-vm:{commit[:12] or 'local'}"
    build_vm_image(args.runner, vm_tag)
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda c: run_case(rows[c], args, vm_tag), args.cases))
    Path(args.out).mkdir(parents=True, exist_ok=True)
    summary = {"runner_commit": commit, "mode": args.mode, "results": results}
    if args.mode in ("run", "score"):
        done = [r for r in results if r.get("solved") is not None]
        summary["solved"] = sum(1 for r in done if r["solved"])
        summary["scored"] = len(done)
    (Path(args.out) / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
