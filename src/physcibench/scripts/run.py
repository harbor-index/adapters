"""Run Harbor and record the configured judge model in the job's results.json."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import yaml


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_results(path: Path, record: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", type=Path,
                        default=Path("config/run_physcibench_162.yaml"))
    parser.add_argument("-y", "--yes", action="store_true")
    parser.add_argument("--job-name")
    parser.add_argument("--jobs-dir", type=Path)
    args = parser.parse_args()

    config_path = args.config.resolve()
    with config_path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        parser.error("The job configuration must be a YAML mapping.")
    judge_model = os.environ.get("JUDGE_LLM_MODEL")
    if not judge_model:
        parser.error("Export JUDGE_LLM_MODEL before starting the run.")

    # Prefer Harbor installed next to this interpreter, including under sudo.
    sibling = Path(sys.executable).with_name("harbor")
    harbor = str(sibling) if sibling.is_file() else shutil.which("harbor")
    if not harbor:
        parser.error("Harbor is not installed in this environment or on PATH.")

    job_name = args.job_name or config.get("job_name") or datetime.now().strftime(
        "%Y-%m-%d__%H-%M-%S"
    )
    if not isinstance(job_name, str) or job_name in ("", ".", "..") or Path(job_name).name != job_name:
        parser.error("The job name must be a single directory name.")
    jobs_dir = (args.jobs_dir or Path(config.get("jobs_dir", "jobs"))).resolve()
    job_dir = jobs_dir / job_name
    # Never overwrite metadata for an existing run or silently resume it.
    try:
        job_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error(f"Job directory already exists: {job_dir}; choose a new job name.")

    result_path = job_dir / "results.json"
    record = {
        "job_name": job_name,
        "config_file": str(config_path),
        "judge_model": judge_model,
        "agents": [
            {"name": agent.get("name"), "model_name": agent.get("model_name")}
            for agent in config.get("agents", [{"name": "oracle"}])
        ],
        "started_at": timestamp(),
        "finished_at": None,
        "status": "running",
        "harbor_exit_code": None,
        "harbor_result_file": "result.json",
    }
    write_results(result_path, record)
    print(f"Judge model: {judge_model}\nRun metadata: {result_path}", flush=True)

    command = [harbor, "run", "-c", str(config_path),
               "--job-name", job_name, "--jobs-dir", str(jobs_dir)]
    if args.yes:
        command.append("-y")
    exit_code = 1
    try:
        exit_code = subprocess.run(command, check=False).returncode
        record["status"] = "finished" if exit_code == 0 else "failed"
    except KeyboardInterrupt:
        exit_code = 130
        record["status"] = "interrupted"
    except OSError as exc:
        exit_code = 127
        record["status"] = "failed"
        print(f"Could not start Harbor: {exc}", file=sys.stderr)
    finally:
        record["finished_at"] = timestamp()
        record["harbor_exit_code"] = exit_code
        write_results(result_path, record)
    return exit_code if exit_code >= 0 else 128 - exit_code


if __name__ == "__main__":
    raise SystemExit(main())
