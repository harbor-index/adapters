"""Convert the pinned OpenAI public case studies into Harbor tasks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
from tempfile import TemporaryDirectory

from huggingface_hub import snapshot_download


DATASET_ID = "openai/genebench-pro-public-package"
DATASET_REVISION = "eb75a3c0996b3cedcc9af685bad02fd166848fa2"
TEMPLATE_DIR = Path(__file__).parent / "task-template"


def _source_path(source: Path, relative: str) -> Path:
    """Accept package-relative POSIX paths, including native HF cache symlinks."""
    path = PurePosixPath(relative)
    if (
        path.is_absolute() or ".." in path.parts or str(path) != relative
        or "\\" in relative or re.match(r"^[A-Za-z]:", relative)
    ):
        raise ValueError(f"Unsafe upstream path: {relative}")
    return source.joinpath(*path.parts)


def _task_slug(upstream_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", upstream_id.lower()).strip("-")
    if not slug:
        raise ValueError(f"Upstream ID has no registry-safe characters: {upstream_id!r}")
    return slug


def _verify_hash(source: Path, relative: str, expected: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError(f"Invalid upstream SHA-256: {relative}")
    actual = hashlib.sha256(_source_path(source, relative).read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"Upstream checksum mismatch: {relative}")


class GeneBenchProAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        cache_dir: Path | None = None,
        offline: bool = False,
        revision: str = DATASET_REVISION,
    ) -> None:
        if limit is not None and limit < 1:
            raise ValueError("--limit must be at least 1")
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("--revision must be a full lowercase Hugging Face commit SHA")
        self.output_dir = output_dir
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.cache_dir = cache_dir
        self.offline = offline
        self.revision = revision

    def run(self) -> None:
        source = Path(
            snapshot_download(
                repo_id=DATASET_ID,
                repo_type="dataset",
                revision=self.revision,
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
        problems = self._verify_checksums(source)
        problems = sorted(problems, key=lambda item: item["release_order"])
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
            upstream_id = problem["eval_id"]
            directory = upstream_id if re.fullmatch(r"[A-Za-z0-9_-]+", upstream_id) else _task_slug(upstream_id)
            destination = self.output_dir / directory
            if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
                raise ValueError(f"Task destination must be a directory, not a symlink or file: {destination}")
            if destination.exists():
                if not self.overwrite:
                    print(f"Skipping existing task: {upstream_id}")
                    continue
            with TemporaryDirectory(prefix=".genebench-pro-", dir=self.output_dir) as temporary:
                staged = Path(temporary) / "task"
                previous = Path(temporary) / "previous"
                self._write_task(source, problem, staged)
                if destination.exists():
                    destination.replace(previous)
                try:
                    staged.replace(destination)
                except BaseException:
                    if previous.exists():
                        previous.replace(destination)
                    raise
            generated += 1
        print(f"Generated {generated} GeneBench-Pro tasks in {self.output_dir.resolve()}")

    @staticmethod
    def _verify_checksums(source: Path) -> list[dict]:
        expected_hashes = {}
        for line in (source / "checksums.sha256").read_text().splitlines():
            expected, relative = line.split(maxsplit=1)
            if relative in expected_hashes:
                raise ValueError(f"Duplicate upstream checksum: {relative}")
            expected_hashes[relative] = expected
        for relative in ("manifest.json", "reference_grader.py", "reference_definitions.md"):
            if relative not in expected_hashes:
                raise ValueError(f"Missing upstream checksum: {relative}")
            _verify_hash(source, relative, expected_hashes[relative])
        manifest = json.loads((source / "manifest.json").read_text())
        names = set()
        for problem in manifest["problems"]:
            name = _task_slug(problem["eval_id"])
            if name in names:
                raise ValueError(f"Duplicate registry task name: {name}")
            names.add(name)
            required = {problem["eval_config"], *problem["data_files"]}
            hashes = {}
            for entry in problem["files"]:
                if entry["path"] in hashes:
                    raise ValueError(f"Duplicate upstream file: {entry['path']}")
                hashes[entry["path"]] = entry["sha256"]
            missing = required - hashes.keys()
            if missing:
                raise ValueError(f"Missing upstream checksums: {', '.join(sorted(missing))}")
            for relative in required:
                _verify_hash(source, relative, hashes[relative])
            config_path = _source_path(source, problem["eval_config"])
            config = json.loads(config_path.read_text())
            if config["id"] != problem["eval_id"] or not isinstance(config["task"], str):
                raise ValueError(f"Inconsistent upstream configuration: {problem['eval_id']}")
            inputs = []
            for relative in config["data_files"]:
                local = _source_path(Path(), relative)
                if local.parts[0] != "data_files" or len(local.parts) < 2:
                    raise ValueError(f"Input must be under data_files/: {relative}")
                inputs.append((config_path.parent / local).relative_to(source).as_posix())
            if set(inputs) != set(problem["data_files"]) or len(inputs) != len(set(inputs)):
                raise ValueError(f"Config and manifest inputs differ: {problem['eval_id']}")
        return manifest["problems"]

    def _write_task(self, source: Path, problem: dict, destination: Path) -> None:
        config_path = source / problem["eval_config"]
        config = json.loads(config_path.read_text())
        upstream_id = problem["eval_id"]
        shutil.copytree(TEMPLATE_DIR, destination)

        task_name = f"openai/genebench-pro-{_task_slug(upstream_id)}"
        task_toml = (destination / "task.toml").read_text()
        task_toml = task_toml.replace("{task_name}", task_name)
        task_toml = task_toml.replace("{source_revision}", self.revision)
        task_toml = task_toml.replace("{upstream_id}", json.dumps(upstream_id, ensure_ascii=False))
        (destination / "task.toml").write_text(task_toml)

        input_paths = []
        for relative in config["data_files"]:
            target = destination / "environment" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(config_path.parent / relative, target)
            input_paths.append(f"- `/workspace/{relative}`")
        instruction = (destination / "instruction.md").read_text()
        instruction = instruction.format(task=config["task"], input_files="\n".join(input_paths))
        (destination / "instruction.md").write_text(instruction)

        # Harbor uploads tests only after the agent has finished.
        shutil.copyfile(config_path, destination / "tests" / "eval_config.json")
        shutil.copyfile(source / "reference_grader.py", destination / "tests" / "reference_grader.py")
        shutil.copyfile(source / "LICENSE", destination / "LICENSE")
        shutil.copyfile(source / "LICENSE", destination / "tests" / "LICENSE")
        shutil.copyfile(source / "LICENSE", destination / "environment" / "LICENSE.dataset")
        answer = {
            "answer": config["ground_truth"],
            "reasoning": "Released GeneBench-Pro reference answer for oracle verification.",
        }
        (destination / "solution" / "eval_answer.json").write_text(
            json.dumps(answer, indent=2, allow_nan=False) + "\n"
        )
        (destination / "tests" / "test.sh").chmod(0o755)
        (destination / "tests").chmod(0o700)
        (destination / "solution" / "solve.sh").chmod(0o755)
