"""Convert the OpenAI public case studies into Harbor tasks."""

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from huggingface_hub import snapshot_download

DATASET_ID = "openai/genebench-pro-public-package"
DATASET_REVISION = "eb75a3c0996b3cedcc9af685bad02fd166848fa2"
TEMPLATE_DIR = Path(__file__).parent / "task-template"
DOWNLOAD_PATTERNS = [
    "LICENSE",
    "manifest.json",
    "checksums.sha256",
    "reference_grader.py",
    "problems/*/eval_config.json",
    "problems/*/data_files/*",
]
logger = logging.getLogger(__name__)


def _task_id(upstream_id: str) -> str:
    task_id = re.sub(r"[^a-z0-9]+", "-", upstream_id.lower()).strip("-")
    if not task_id:
        raise ValueError(
            f"Upstream ID has no registry-safe characters: {upstream_id!r}"
        )
    return task_id


def _checked_file(source: Path, relative: str, checksums: dict[str, str]) -> Path:
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or relative_path.drive
        or ".." in relative_path.parts
    ):
        raise ValueError(f"Unsafe upstream path: {relative}")
    path = source / relative_path
    expected = checksums[relative]
    with path.open("rb") as file:
        actual = hashlib.file_digest(file, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(f"Upstream checksum mismatch: {relative}")
    return path


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
        source = Path(
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
        checksums = {}
        for line in (source / "checksums.sha256").read_text().splitlines():
            checksum, relative = line.split(maxsplit=1)
            checksums[relative] = checksum

        manifest_path = _checked_file(source, "manifest.json", checksums)
        _checked_file(source, "reference_grader.py", checksums)
        manifest = json.loads(manifest_path.read_text())
        problems = sorted(manifest["problems"], key=lambda item: item["release_order"])
        names = set()
        for problem in problems:
            name = _task_id(problem["eval_id"])
            if name in names:
                raise ValueError(f"Duplicate registry task name: {name}")
            names.add(name)

        if self.task_ids is not None:
            requested = set(self.task_ids)
            unknown = requested - {problem["eval_id"] for problem in problems}
            if unknown:
                raise ValueError(f"Unknown task IDs: {', '.join(sorted(unknown))}")
            problems = [
                problem for problem in problems if problem["eval_id"] in requested
            ]

        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated = 0
        for problem in problems[: self.limit]:
            upstream_id = problem["eval_id"]
            if re.fullmatch(r"[A-Za-z0-9_-]+", upstream_id):
                directory = upstream_id
            else:
                directory = _task_id(upstream_id)
            destination = self.output_dir / directory
            if destination.is_symlink() or (
                destination.exists() and not destination.is_dir()
            ):
                raise ValueError(f"Task destination is a symlink or file: {destination}")
            if destination.exists() and not self.overwrite:
                logger.info("Skipping existing task: %s", upstream_id)
                continue

            with TemporaryDirectory(dir=self.output_dir) as temporary:
                staged = Path(temporary) / "task"
                self._write_task(source, problem, staged, checksums)
                if destination.exists():
                    shutil.rmtree(destination)
                staged.replace(destination)
            generated += 1
        logger.info("Generated %d tasks in %s", generated, self.output_dir.resolve())

    def _write_task(
        self, source: Path, problem: dict, destination: Path, checksums: dict[str, str]
    ) -> None:
        config_path = _checked_file(source, problem["eval_config"], checksums)
        config = json.loads(config_path.read_text())
        shutil.copytree(TEMPLATE_DIR, destination)

        input_paths = []
        for relative in config["data_files"]:
            if not relative.startswith("data_files/"):
                raise ValueError(f"Input must be under data_files/: {relative}")
            package_path = (config_path.parent / relative).relative_to(source)
            input_file = _checked_file(source, package_path.as_posix(), checksums)
            target = destination / "environment" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(input_file, target)
            input_paths.append(f"- `/workspace/{relative}`")

        upstream_id = problem["eval_id"]
        task_config = destination / "task.toml"
        task_name = f"openai/genebench-pro-{_task_id(upstream_id)}"
        task_toml = task_config.read_text()
        task_toml = task_toml.replace("{task_name}", task_name)
        task_toml = task_toml.replace("{source_revision}", self.revision)
        task_toml = task_toml.replace(
            "{upstream_id}", json.dumps(upstream_id, ensure_ascii=False)
        )
        task_config.write_text(task_toml)
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
        for license_path in (
            destination / "LICENSE",
            tests_dir / "LICENSE",
            destination / "environment/LICENSE.dataset",
        ):
            shutil.copyfile(source / "LICENSE", license_path)

        answer = {
            "answer": config["ground_truth"],
            "reasoning": (
                "Released GeneBench-Pro reference answer for oracle verification."
            ),
        }
        (destination / "solution/eval_answer.json").write_text(
            json.dumps(answer, indent=2, allow_nan=False) + "\n"
        )
        (tests_dir / "test.sh").chmod(0o755)
        tests_dir.chmod(0o700)
        (destination / "solution/solve.sh").chmod(0o755)
