"""Convert the pinned OpenAI public case studies into Harbor tasks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from huggingface_hub import snapshot_download


DATASET_ID = "openai/genebench-pro-public-package"
DATASET_REVISION = "eb75a3c0996b3cedcc9af685bad02fd166848fa2"
TEMPLATE_DIR = Path(__file__).parent / "task-template"


class GeneBenchProAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        cache_dir: Path | None = None,
        offline: bool = False,
    ) -> None:
        if limit is not None and limit < 1:
            raise ValueError("--limit must be at least 1")
        self.output_dir = output_dir
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.cache_dir = cache_dir
        self.offline = offline

    def run(self) -> None:
        source = Path(
            snapshot_download(
                repo_id=DATASET_ID,
                repo_type="dataset",
                revision=DATASET_REVISION,
                cache_dir=self.cache_dir,
                local_files_only=self.offline,
                token=False,
                allow_patterns=[
                    "LICENSE", "README.md", "manifest.json", "checksums.sha256",
                    "reference_definitions.md", "reference_grader.py",
                    "problems/*/eval_config.json", "problems/*/data_files/*",
                ],
            )
        )
        self._verify_checksums(source)
        manifest = json.loads((source / "manifest.json").read_text())
        problems = sorted(manifest["problems"], key=lambda item: item["release_order"])
        known_ids = {problem["eval_id"] for problem in problems}
        if self.task_ids is not None:
            unknown = set(self.task_ids) - known_ids
            if unknown:
                raise ValueError(f"Unknown task IDs: {', '.join(sorted(unknown))}")
            problems = [p for p in problems if p["eval_id"] in self.task_ids]
        if self.limit is not None:
            problems = problems[: self.limit]

        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated = 0
        for problem in problems:
            destination = self.output_dir / problem["eval_id"]
            if destination.exists():
                if not self.overwrite:
                    print(f"Skipping existing task: {problem['eval_id']}")
                    continue
                shutil.rmtree(destination)
            self._write_task(source, problem, destination)
            generated += 1
        print(f"Generated {generated} GeneBench-Pro tasks in {self.output_dir.resolve()}")

    @staticmethod
    def _verify_checksums(source: Path) -> None:
        expected_hashes = {}
        for line in (source / "checksums.sha256").read_text().splitlines():
            expected, relative = line.split(maxsplit=1)
            if relative in {"manifest.json", "reference_grader.py", "reference_definitions.md"}:
                expected_hashes[relative] = expected
        manifest = json.loads((source / "manifest.json").read_text())
        for problem in manifest["problems"]:
            required = {problem["eval_config"], *problem["data_files"]}
            for entry in problem["files"]:
                if entry["path"] in required:
                    expected_hashes[entry["path"]] = entry["sha256"]
        for relative, expected in expected_hashes.items():
            actual = hashlib.sha256((source / relative).read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"Upstream checksum mismatch: {relative}")

    @staticmethod
    def _write_task(source: Path, problem: dict, destination: Path) -> None:
        config_path = source / problem["eval_config"]
        config = json.loads(config_path.read_text())
        upstream_id = problem["eval_id"]
        shutil.copytree(TEMPLATE_DIR, destination)

        task_name = f"openai/genebench-pro-{upstream_id.replace('_', '-')}"
        task_toml = (destination / "task.toml").read_text()
        task_toml = task_toml.replace("{task_name}", task_name)
        task_toml = task_toml.replace("{upstream_id}", upstream_id)
        (destination / "task.toml").write_text(task_toml)

        input_paths = []
        for relative in config["data_files"]:
            target = destination / "environment" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(config_path.parent / relative, target)
            input_paths.append(f"- `/workspace/{relative}`")
        instruction = (destination / "instruction.md").read_text()
        instruction = instruction.replace("{task}", config["task"])
        instruction = instruction.replace("{input_files}", "\n".join(input_paths))
        (destination / "instruction.md").write_text(instruction)

        # Harbor uploads tests only after the agent has finished.
        shutil.copyfile(config_path, destination / "tests" / "eval_config.json")
        shutil.copyfile(source / "reference_grader.py", destination / "tests" / "reference_grader.py")
        shutil.copyfile(source / "LICENSE", destination / "LICENSE")
        shutil.copyfile(source / "LICENSE", destination / "tests" / "LICENSE")
        shutil.copyfile(source / "LICENSE", destination / "environment" / "LICENSE.genebench-pro")
        answer = {
            "answer": config["ground_truth"],
            "reasoning": "Released GeneBench-Pro reference answer for oracle verification.",
        }
        (destination / "solution" / "eval_answer.json").write_text(
            json.dumps(answer, indent=2, allow_nan=False) + "\n"
        )
        (destination / "tests" / "test.sh").chmod(0o755)
        (destination / "solution" / "solve.sh").chmod(0o755)
