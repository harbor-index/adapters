"""Convert the pinned OpenAI public case studies into Harbor tasks."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from huggingface_hub import snapshot_download


DATASET_ID = "openai/genebench-pro-public-package"
DATASET_REVISION = "eb75a3c0996b3cedcc9af685bad02fd166848fa2"
TEMPLATE_DIR = Path(__file__).parent / "task-template"
DOWNLOAD_PATTERNS = [
    "LICENSE",
    "README.md",
    "manifest.json",
    "checksums.sha256",
    "reference_definitions.md",
    "reference_grader.py",
    "problems/*/eval_config.json",
    "problems/*/data_files/*",
]
logger = logging.getLogger(__name__)


def _relative_path(relative: str) -> PurePosixPath:
    """Reject paths that could escape the package or task directory."""
    path = PurePosixPath(relative)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != relative
        or "\\" in relative
        or re.match(r"^[A-Za-z]:", relative)
    ):
        raise ValueError(f"Unsafe upstream path: {relative}")
    return path


def _sanitize_task_id(upstream_id: str) -> str:
    task_id = re.sub(r"[^a-z0-9]+", "-", upstream_id.lower()).strip("-")
    if not task_id:
        raise ValueError(
            f"Upstream ID has no registry-safe characters: {upstream_id!r}"
        )
    return task_id


def _verify_hash(path: Path, expected: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError(f"Invalid upstream SHA-256: {path}")
    with path.open("rb") as file:
        actual = hashlib.file_digest(file, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(f"Upstream checksum mismatch: {path}")


class GeneBenchProAdapter:
    """Generate tasks from the OpenAI public package without changing its grader."""

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
            raise ValueError(
                "--revision must be a full lowercase Hugging Face commit SHA"
            )
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.cache_dir = cache_dir
        self.offline = offline
        self.revision = revision

    def run(self) -> None:
        source = self._download_source()
        problems = self._load_benchmark_data(source)
        selected = self._select_problems(problems)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated = 0
        for problem in selected:
            if self._generate_task(source, problem):
                generated += 1
        logger.info("Generated %d tasks in %s", generated, self.output_dir.resolve())

    def _download_source(self) -> Path:
        return Path(
            snapshot_download(
                repo_id=DATASET_ID,
                repo_type="dataset",
                revision=self.revision,
                cache_dir=self.cache_dir,
                local_files_only=self.offline,
                token=False,
                allow_patterns=DOWNLOAD_PATTERNS,
            )
        )

    def _select_problems(self, problems: list[dict]) -> list[dict]:
        if self.task_ids is not None:
            requested_ids = set(self.task_ids)
            known_ids = {problem["eval_id"] for problem in problems}
            unknown = requested_ids - known_ids
            if unknown:
                raise ValueError(f"Unknown task IDs: {', '.join(sorted(unknown))}")
            problems = [
                problem for problem in problems if problem["eval_id"] in requested_ids
            ]
        return problems[: self.limit]

    @staticmethod
    def _verify_package_files(source: Path) -> None:
        checksums = {}
        for line in (source / "checksums.sha256").read_text().splitlines():
            expected, relative = line.split(maxsplit=1)
            if relative in checksums:
                raise ValueError(f"Duplicate upstream checksum: {relative}")
            checksums[relative] = expected

        required = ("manifest.json", "reference_grader.py", "reference_definitions.md")
        for relative in required:
            if relative not in checksums:
                raise ValueError(f"Missing upstream checksum: {relative}")
            _verify_hash(source / relative, checksums[relative])

    def _load_benchmark_data(self, source: Path) -> list[dict]:
        self._verify_package_files(source)
        manifest = json.loads((source / "manifest.json").read_text())
        problems = sorted(manifest["problems"], key=lambda item: item["release_order"])
        task_names = set()
        for problem in problems:
            name = _sanitize_task_id(problem["eval_id"])
            if name in task_names:
                raise ValueError(f"Duplicate registry task name: {name}")
            task_names.add(name)
            self._validate_problem(source, problem)
        return problems

    @staticmethod
    def _validate_problem(source: Path, problem: dict) -> None:
        checksums = {}
        for entry in problem["files"]:
            relative = entry["path"]
            if relative in checksums:
                raise ValueError(f"Duplicate upstream file: {relative}")
            checksums[relative] = entry["sha256"]

        required = {problem["eval_config"], *problem["data_files"]}
        missing = required - checksums.keys()
        if missing:
            missing_files = ", ".join(sorted(missing))
            raise ValueError(f"Missing upstream checksums: {missing_files}")
        for relative in required:
            _verify_hash(source / _relative_path(relative), checksums[relative])

        config_path = source / _relative_path(problem["eval_config"])
        config = json.loads(config_path.read_text())
        if config["id"] != problem["eval_id"] or not isinstance(config["task"], str):
            raise ValueError(
                f"Inconsistent upstream configuration: {problem['eval_id']}"
            )

        configured_inputs = set()
        for relative in config["data_files"]:
            data_path = _relative_path(relative)
            if len(data_path.parts) < 2 or data_path.parts[0] != "data_files":
                raise ValueError(f"Input must be under data_files/: {relative}")
            package_path = (config_path.parent / data_path).relative_to(source)
            configured_inputs.add(package_path.as_posix())
        if (
            configured_inputs != set(problem["data_files"])
            or len(configured_inputs) != len(config["data_files"])
        ):
            raise ValueError(f"Config and manifest inputs differ: {problem['eval_id']}")

    def _generate_task(self, source: Path, problem: dict) -> bool:
        upstream_id = problem["eval_id"]
        if re.fullmatch(r"[A-Za-z0-9_-]+", upstream_id):
            directory_name = upstream_id
        else:
            directory_name = _sanitize_task_id(upstream_id)
        destination = self.output_dir / directory_name

        if destination.is_symlink() or (
            destination.exists() and not destination.is_dir()
        ):
            raise ValueError(f"Task destination is a symlink or file: {destination}")
        if destination.exists() and not self.overwrite:
            logger.info("Skipping existing task: %s", upstream_id)
            return False

        with TemporaryDirectory(dir=self.output_dir) as temporary:
            staged = Path(temporary) / "task"
            previous = Path(temporary) / "previous"
            self._prepare_task(source, problem, staged)
            try:
                if destination.exists():
                    destination.replace(previous)
                staged.replace(destination)
            except BaseException:
                # Restore existing output on errors and interruptions.
                if previous.exists():
                    previous.replace(destination)
                raise
        return True

    def _prepare_task(self, source: Path, problem: dict, destination: Path) -> None:
        config_path = source / problem["eval_config"]
        config = json.loads(config_path.read_text())
        upstream_id = problem["eval_id"]
        shutil.copytree(TEMPLATE_DIR, destination)

        task_name = f"openai/genebench-pro-{_sanitize_task_id(upstream_id)}"
        task_config = destination / "task.toml"
        task_toml = task_config.read_text().replace("{task_name}", task_name)
        task_toml = task_toml.replace("{source_revision}", self.revision)
        # Insert source text last so literal template fields remain intact.
        task_toml = task_toml.replace(
            "{upstream_id}", json.dumps(upstream_id, ensure_ascii=False)
        )
        task_config.write_text(task_toml)

        input_paths = []
        for relative in config["data_files"]:
            target = destination / "environment" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(config_path.parent / relative, target)
            input_paths.append(f"- `/workspace/{relative}`")
        instruction_path = destination / "instruction.md"
        instruction = instruction_path.read_text().format(
            task=config["task"], input_files="\n".join(input_paths)
        )
        instruction_path.write_text(instruction)

        tests_dir = destination / "tests"
        shutil.copyfile(config_path, tests_dir / "eval_config.json")
        shutil.copyfile(
            source / "reference_grader.py", tests_dir / "reference_grader.py"
        )
        shutil.copyfile(source / "LICENSE", destination / "LICENSE")
        shutil.copyfile(source / "LICENSE", tests_dir / "LICENSE")
        shutil.copyfile(
            source / "LICENSE", destination / "environment" / "LICENSE.dataset"
        )
        answer = {
            "answer": config["ground_truth"],
            "reasoning": (
                "Released GeneBench-Pro reference answer for oracle verification."
            ),
        }
        (destination / "solution" / "eval_answer.json").write_text(
            json.dumps(answer, indent=2, allow_nan=False) + "\n"
        )
        (tests_dir / "test.sh").chmod(0o755)
        tests_dir.chmod(0o700)
        (destination / "solution" / "solve.sh").chmod(0o755)
