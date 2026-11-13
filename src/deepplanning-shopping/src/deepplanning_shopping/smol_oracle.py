"""Run local Smol oracle trials from a shared dependency-prepared machine."""

import argparse
import json
from pathlib import Path

from .adapter import BASE_IMAGE
from .results import check_oracle


def main():
    from harbor.cli.jobs import start
    from smol import ConnectOptions, ExecOptions, Machine, MachineConfig, ResourceSpec

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--jobs-dir", type=Path, default=Path("jobs"))
    parser.add_argument("--job-name", default="deepplanning-shopping-smol-oracle")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    print(
        "Preparing local Smol machine with pinned shopping dependencies...", flush=True
    )
    parent = Machine.create(
        MachineConfig(
            image=BASE_IMAGE,
            checkpoint=True,
            resources=ResourceSpec(cpus=1, memory_mb=512, storage_gb=1, network=True),
        ),
        ConnectOptions(target="local"),
    )
    try:
        result = parent.exec(
            [
                "/usr/local/bin/python",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "numpy==2.2.6",
                "rank-bm25==0.2.2",
            ],
            ExecOptions(timeout=180),
        )
        if result.exit_code:
            raise RuntimeError(f"Dependency preparation failed: {result.stderr}")
        checkpoint = {
            BASE_IMAGE: {
                "machine": parent.id,
                "resources": {"cpus": 1, "memory_mb": 512, "storage_mb": 1024},
                "network_mode": "public",
            }
        }
        print(
            "Running Harbor oracle with local Smol agent and verifier machines...",
            flush=True,
        )
        start(
            path=args.tasks_dir.resolve(),
            agent_name="oracle",
            environment="smol",
            environment_kwargs=[
                "target=local",
                "checkpoints=" + json.dumps(checkpoint),
            ],
            n_concurrent_trials=args.concurrency,
            jobs_dir=args.jobs_dir.resolve(),
            job_name=args.job_name,
            yes=True,
        )
        count = sum((p / "task.toml").is_file() for p in args.tasks_dir.iterdir())
        receipt = check_oracle(args.jobs_dir / args.job_name, count)
        print(
            f"Verified {receipt['trials']} perfect isolated oracle trials.", flush=True
        )
    finally:
        parent.delete()
        print("Prepared Smol machine cleaned up.", flush=True)


if __name__ == "__main__":
    main()
