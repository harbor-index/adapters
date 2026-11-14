"""Generate independent Harbor tasks from pinned DeceptionBench scenarios."""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import tomllib
from pathlib import Path

from .dataset import (
    DATA_HASHES,
    DATASET_ID,
    DATASET_REVISION,
    DEFAULT_CACHE_DIR,
    UPSTREAM_COMMIT,
    SampleTask,
    load_tasks,
    select_tasks,
)
from .protocol import RESPONSE_SUFFIX, build_generation_prompt

PACKAGE_DIR = Path(__file__).resolve().parent


class DeceptionBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        *,
        data_dir: Path | None = None,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        limit: int | None = None,
        task_ids: list[str] | None = None,
        domains: list[str] | None = None,
        conditions: list[str] | None = None,
        split: str = "full",
        overwrite: bool = False,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        self.selection = {
            "limit": limit,
            "task_ids": task_ids,
            "domains": domains,
            "conditions": conditions,
            "split": split,
        }
        self.overwrite = overwrite

    @staticmethod
    def _task_name(task: SampleTask) -> str:
        return f"aries-iai/{task.task_id}"

    def _check_destination(self, destination: Path, task: SampleTask) -> None:
        if destination.is_symlink():
            raise ValueError(f"Refusing symlink destination: {destination}")
        if not destination.exists():
            return
        if not self.overwrite:
            raise FileExistsError(
                f"Task already exists (use --overwrite): {destination}"
            )
        try:
            config = tomllib.loads((destination / "task.toml").read_text())
            matches = config["task"]["name"] == self._task_name(task)
        except (OSError, ValueError, KeyError, TypeError):
            matches = False
        if not matches:
            raise ValueError(
                f"Refusing to overwrite an unrelated directory: {destination}"
            )

    def _render(self, destination: Path, task: SampleTask) -> None:
        shutil.copytree(PACKAGE_DIR / "task-template", destination, dirs_exist_ok=True)
        config = (destination / "task.toml").read_text()
        config = config.replace("{task_name}", self._task_name(task))
        config = config.replace("{domain}", task.domain).replace(
            "{condition}", task.condition
        )
        (destination / "task.toml").write_text(config, encoding="utf-8")
        instruction = (
            (destination / "instruction.md")
            .read_text()
            .replace(
                "{instruction}", build_generation_prompt(task.sample[task.condition])
            )
        )
        (destination / "instruction.md").write_text(instruction, encoding="utf-8")
        (destination / "environment" / "prompt.txt").write_text(
            task.sample[task.condition] + RESPONSE_SUFFIX, encoding="utf-8"
        )
        provenance = {
            "upstream_commit": UPSTREAM_COMMIT,
            "dataset": DATASET_ID,
            "dataset_revision": DATASET_REVISION,
            "dataset_file": f"{task.domain}.json",
            "dataset_sha256": DATA_HASHES[task.domain],
        }
        metadata = {
            "task_id": task.task_id,
            "domain": task.domain,
            "condition": task.condition,
            "sample": task.sample,
            "provenance": provenance,
        }
        (destination / "tests" / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        shutil.copyfile(
            PACKAGE_DIR / "protocol.py", destination / "tests" / "protocol.py"
        )
        shutil.copyfile(
            PACKAGE_DIR / "verifier.py", destination / "tests" / "verify.py"
        )
        # The original judge accepts matching groundtruth in its non-JSON rule.
        # Its real-judge pass rate must still be measured before publishing.
        oracle = base64.b64encode(task.sample["groundtruth"].encode("utf-8")).decode(
            "ascii"
        )
        solve = (
            (destination / "solution" / "solve.sh")
            .read_text()
            .replace("{oracle_base64}", oracle)
        )
        (destination / "solution" / "solve.sh").write_text(solve, encoding="utf-8")
        for script in (
            destination / "solution" / "solve.sh",
            destination / "tests" / "test.sh",
        ):
            script.chmod(0o755)

    def run(self) -> list[Path]:
        tasks = select_tasks(
            load_tasks(self.data_dir, self.cache_dir), **self.selection
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for task in tasks:
            self._check_destination(self.output_dir / task.task_id, task)
        generated = []
        for task in tasks:
            destination = self.output_dir / task.task_id
            with tempfile.TemporaryDirectory(
                prefix=".deceptionbench-", dir=self.output_dir
            ) as temporary:
                staged = Path(temporary) / task.task_id
                self._render(staged, task)
                if destination.exists():
                    backup = Path(temporary) / "previous"
                    destination.rename(backup)
                    try:
                        staged.rename(destination)
                    except BaseException:
                        backup.rename(destination)
                        raise
                else:
                    staged.rename(destination)
            generated.append(destination)
        return generated
