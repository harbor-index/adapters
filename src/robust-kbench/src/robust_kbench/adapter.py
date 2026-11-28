"""
Harbor adapter implementation for Robust-KBench.
Generates self-contained Harbor task directories for CUDA kernel robustness evaluations.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import List, Optional

from .utils import RAW_DATA_DIR, get_available_tasks

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "task-template"


def _copy_lf(src: Path, dst: Path) -> None:
    """Copy a text script ensuring Unix LF line endings for Linux Docker containers."""
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    dst.write_bytes(text.encode("utf-8"))


class RobustKBenchAdapter:
    """Adapter for generating Harbor tasks from Sakana AI's Robust-KBench benchmark."""

    def __init__(
        self,
        output_dir: Path,
        limit: Optional[int] = None,
        overwrite: bool = False,
        task_ids: Optional[List[str]] = None,
        include_h100_only: bool = False,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = set(task_ids) if task_ids else None
        self.include_h100_only = include_h100_only

    def run(self) -> List[Path]:
        """Generate all task directories under self.output_dir."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        available_tasks = get_available_tasks(include_h100_only=self.include_h100_only)

        if self.task_ids:
            available_tasks = [t for t in available_tasks if t["task_id"] in self.task_ids]

        if self.limit:
            available_tasks = available_tasks[: self.limit]

        logger.info(f"Generating {len(available_tasks)} Robust-KBench tasks to {self.output_dir}...")
        generated_dirs = []

        for task_info in available_tasks:
            task_dir = self._generate_task(task_info)
            if task_dir:
                generated_dirs.append(task_dir)

        logger.info(f"Successfully generated {len(generated_dirs)} tasks.")
        return generated_dirs

    def _generate_task(self, task_info: dict) -> Optional[Path]:
        task_id = task_info["task_id"]
        task_name = task_info["task_name"]
        pass_type = task_info["pass_type"]

        # Output folder for this specific task
        task_dir = self.output_dir / f"robust-kbench__{task_id}"

        if task_dir.exists():
            if self.overwrite:
                shutil.rmtree(task_dir)
            else:
                logger.info(f"Skipping existing task directory: {task_dir}")
                return task_dir

        task_dir.mkdir(parents=True, exist_ok=True)

        # 1. Read template files
        task_toml_template = (TEMPLATE_DIR / "task.toml").read_text(encoding="utf-8")
        instruction_template = (TEMPLATE_DIR / "instruction.md").read_text(encoding="utf-8")

        # 2. Render templates
        rendered_toml = (
            task_toml_template.replace("{{task_id}}", task_id)
            .replace("{{task_name}}", task_name)
            .replace("{{pass_type}}", pass_type)
        )
        (task_dir / "task.toml").write_text(rendered_toml, encoding="utf-8")

        rendered_instruction = (
            instruction_template.replace("{{task_name}}", task_name)
            .replace("{{pass_type}}", pass_type)
        )
        (task_dir / "instruction.md").write_text(rendered_instruction, encoding="utf-8")

        # 3. Copy environment directory
        env_dir = task_dir / "environment"
        env_dir.mkdir(parents=True, exist_ok=True)
        _copy_lf(TEMPLATE_DIR / "environment" / "Dockerfile", env_dir / "Dockerfile")

        # Copy func.py and config.json into environment so Docker builds them into /app
        shutil.copy2(task_info["func_path"], env_dir / "func.py")
        shutil.copy2(task_info["config_path"], env_dir / "config.json")

        with open(env_dir / "Dockerfile", "a", encoding="utf-8", newline="\n") as df:
            df.write("\nCOPY func.py /app/func.py\n")
            df.write("COPY config.json /app/config.json\n")

        # 4. Copy tests directory (including upstream robust_kbench package + task_files)
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        _copy_lf(TEMPLATE_DIR / "tests" / "test.sh", tests_dir / "test.sh")
        _copy_lf(TEMPLATE_DIR / "tests" / "eval_verifier.py", tests_dir / "eval_verifier.py")

        shutil.copytree(
            RAW_DATA_DIR / "robust_kbench",
            tests_dir / "robust_kbench",
            dirs_exist_ok=True,
        )

        task_files_dir = tests_dir / "task_files"
        task_files_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(task_info["func_path"], task_files_dir / f"func_{pass_type}.py")
        shutil.copy2(task_info["config_path"], task_files_dir / f"config_{pass_type}.json")

        (tests_dir / "meta.json").write_text(
            json.dumps({"task_name": task_name, "pass_type": pass_type}, indent=2),
            encoding="utf-8",
        )

        # 5. Copy solution directory
        sol_dir = task_dir / "solution"
        sol_dir.mkdir(parents=True, exist_ok=True)
        _copy_lf(TEMPLATE_DIR / "solution" / "solve.sh", sol_dir / "solve.sh")
        _copy_lf(task_info["golden_kernel_path"], sol_dir / "reference.cu")

        return task_dir
