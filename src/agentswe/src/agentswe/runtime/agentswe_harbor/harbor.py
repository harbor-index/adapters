from __future__ import annotations

import argparse
import json
import queue
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import settings, trial

# Upstream's adapter.py runs every per-case job with `$AGENTSWE_HARBOR_ROOT/bin/harbor run --config <job>`
# (execute_harbor); inside a task this command runs the job's trials as local processes.
VERSION = "agentswe-harbor local runner (per-case jobs as processes)"
WORKER_SOCKET = settings.WORKER_DIR / "worker.sock"


def run_on_worker(task_dir: Path, trial_dir: Path) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(WORKER_SOCKET))
        client.sendall(json.dumps({"task_dir": str(task_dir), "trial_dir": str(trial_dir)}).encode() + b"\n")
        if not client.makefile("rb").readline():
            raise RuntimeError(f"worker ended without finishing {trial_dir.name}")


def run(config_path: Path) -> int:
    """Run a Harbor job config's trials, at most n_concurrent_trials at a time: one here and, in the dev
    evaluator, one on its worker (upstream runs two per-case trials concurrently, each in its own container)."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    job_dir = Path(config["jobs_dir"]) / config["job_name"]
    job_dir.mkdir(parents=True)
    started_at = datetime.now(timezone.utc).isoformat()
    slots: queue.Queue = queue.Queue()
    slots.put(trial.run_trial)
    if WORKER_SOCKET.exists():
        slots.put(run_on_worker)

    def run_task(task_dir: Path) -> None:
        slot = slots.get()
        try:
            slot(task_dir, job_dir / f"{task_dir.name}__{uuid.uuid4().hex[:7]}")
        finally:
            slots.put(slot)

    with ThreadPoolExecutor(max_workers=min(int(config["n_concurrent_trials"]), slots.qsize())) as pool:
        list(pool.map(run_task, [Path(entry["path"]) for entry in config["tasks"]]))
    results = [json.loads(path.read_text(encoding="utf-8")) for path in job_dir.glob("*/result.json")]
    stats = {"n_trials": len(results), "n_errored_trials": sum(1 for result in results if result["exception_info"])}
    (job_dir / "result.json").write_text(json.dumps({
        "job_name": config["job_name"], "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(), "stats": stats,
    }, indent=2) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="harbor")
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run").add_argument("--config", type=Path, required=True)
    return run(parser.parse_args().config)


if __name__ == "__main__":
    raise SystemExit(main())
