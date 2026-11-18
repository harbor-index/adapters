"""Deploy a generated task's candidate and capture initial nodes without scoring.

This uses the generated separate-verifier context. It writes capture.json, never
reward.txt, and does not run a coding model, GUI judge, or visual judge.
"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    task = args.task.resolve()
    if not (task / "solution/reference/start.sh").is_file():
        parser.error("Generate this task with an explicit --reference-dir first")
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    name = "v2w-draft-" + task.name
    image = name + ":capture"
    env = dict(os.environ)
    helper = Path("/Applications/Docker.app/Contents/Resources/bin")
    if helper.is_dir():
        env["PATH"] = str(helper) + os.pathsep + env["PATH"]

    def command(argv, check=True, timeout=1800):
        p = subprocess.run(
            argv, check=False, env=env, capture_output=True, text=True, timeout=timeout
        )
        with (out / "commands.log").open("a") as f:
            f.write(
                json.dumps(
                    {"args": argv, "code": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
                )
                + "\n"
            )
        if check and p.returncode:
            raise RuntimeError(p.stderr[-1500:] + p.stdout[-1500:])
        return p

    command(["docker", "build", "-t", image, str(task / "tests")])
    cid = command(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--cpus",
            "2",
            "--memory",
            "2g",
            "--shm-size",
            "256m",
            image,
            "sleep",
            "infinity",
        ]
    ).stdout.strip()
    try:
        command(["docker", "cp", str(task / "environment/input") + "/.", cid + ":/workspace"])
        command(["docker", "cp", str(task / "solution/reference") + "/.", cid + ":/workspace"])
        started = time.monotonic()
        run = command(
            ["docker", "exec", cid, "python", "/tests/verify.py", "--capture-only"], check=False
        )
        command(["docker", "cp", cid + ":/logs/verifier/.", str(out)], check=False)
        if (out / "capture.json").exists():
            result = json.loads((out / "capture.json").read_text())
            result["wall_seconds"] = round(time.monotonic() - started, 2)
            result["container_image"] = image
            (out / "capture.json").write_text(json.dumps(result, indent=2) + "\n")
        # A screenshot diagnostic must never masquerade as oracle reward 1.
        command(["docker", "exec", cid, "test", "!", "-e", "/logs/verifier/reward.txt"])
        if run.returncode:
            raise RuntimeError("Capture-only failed; inspect the saved deploy and command logs")
        print((out / "capture.json").read_text())
    finally:
        command(["docker", "rm", "-f", cid], check=False)


if __name__ == "__main__":
    main()
