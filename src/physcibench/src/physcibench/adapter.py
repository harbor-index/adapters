"""Generate Harbor tasks from a local PhySciBench checkout and data snapshot."""

import json
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

TEMPLATE = Path(__file__).with_name("task-template")
EXCLUDED_TYPE = "code-generation"
SUPPORTED_TYPES = {
    "multimodal-qa",
    "long-context-qa",
    "structured-information-extraction",
    "scientific-reasoning",
    "experimental-design",
}


def add_json_output_instruction(path: Path) -> None:
    """Append the JSON contract to the upstream long-form judge prompt."""
    instruction = '''  # Required JSON Output
  Return exactly one valid JSON object matching the required response schema.
  Use this structure (the values below are illustrative):
  {{"evaluations": [{{"criterion": "criterion1", "criterion_name": "Name from the rubric", "reasoning": "Explanation of the assigned score", "score": 1}}]}}
  Include each rubric criterion exactly once, using its original identifier and name.
  Each score must be the integer 0 or 1. Escape quotes and newlines inside JSON strings.
  Return JSON only. Do not include Markdown tables, code fences, or text outside the JSON object.

'''
    text = path.read_text(encoding="utf-8")
    if "# Required JSON Output" in text:
        return
    pattern = r"(?ms)^(PhySciBench_LongForm: \|\n.*?)(?=^[^\s#]|\Z)"
    text, count = re.subn(pattern, lambda match: match[0].rstrip() + "\n\n" + instruction, text)
    if count != 1:
        raise ValueError("Expected one PhySciBench_LongForm YAML prompt")
    path.write_text(text, encoding="utf-8")


class PhySciBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        upstream_dir: Path,
        data_dir: Path,
        *,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.upstream_dir = upstream_dir
        self.data_dir = data_dir
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids

    def run(self) -> None:
        records = json.loads(
            (self.data_dir / "physcibench.json").read_text(encoding="utf-8")
        )
        if not isinstance(records, list):
            raise ValueError("physcibench.json must contain a list of records")
        eligible = {}
        known_ids = set()
        for record in records:
            task_id = record["id"]
            if not re.fullmatch(r"physci-[0-9]+", task_id) or task_id in known_ids:
                raise ValueError(f"Invalid or duplicate task ID: {task_id!r}")
            known_ids.add(task_id)
            task_type = record["type"]
            if task_type == EXCLUDED_TYPE:
                continue
            if task_type not in SUPPORTED_TYPES:
                raise ValueError(f"{task_id}: unsupported task type {task_type!r}")
            eligible[task_id] = record

        requested = set(self.task_ids or [])
        unsupported = requested - eligible.keys()
        if unsupported:
            raise ValueError(f"Unknown or excluded task IDs: {', '.join(sorted(unsupported))}")
        selected = [record for task_id, record in eligible.items() if not requested or task_id in requested]
        selected = selected[:self.limit]

        for name in ("eval/score.py", "requirements.txt", "LICENSE"):
            if not (self.upstream_dir / name).is_file():
                raise FileNotFoundError(f"Missing upstream file: {self.upstream_dir / name}")
        for record in selected:
            self._attachments(record)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated = 0
        skipped = 0
        for record in selected:
            target = self.output_dir / record["id"]
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ValueError(f"Expected a task directory at {target}")
            if target.exists() and not self.overwrite:
                skipped += 1
                continue
            with TemporaryDirectory(dir=self.output_dir, prefix=".physcibench-") as temp:
                staged = Path(temp) / "task"
                self._write_task(staged, record)
                if target.exists():
                    target.rename(Path(temp) / "old")
                    try:
                        staged.rename(target)
                    except OSError:
                        (Path(temp) / "old").rename(target)
                        raise
                else:
                    staged.rename(target)
            generated += 1
        print(f"Generated {generated} tasks in {self.output_dir} ({skipped} existing tasks skipped).")

    def _attachments(self, record: dict) -> list[tuple[Path, Path]]:
        root = (self.data_dir / "files").resolve()
        attachments = []
        for filename in record.get("files") or []:
            relative = Path(filename)
            source = (root / relative).resolve()
            if relative.is_absolute() or ".." in relative.parts or not source.is_relative_to(root):
                raise ValueError(f"{record['id']}: invalid attachment path {filename!r}")
            if not source.is_file():
                raise FileNotFoundError(f"{record['id']}: missing attachment {source}")
            attachments.append((source, relative))
        return attachments

    def _write_task(self, target: Path, record: dict) -> None:
        shutil.copytree(TEMPLATE, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for source, relative in self._attachments(record):
            destination = target / "environment" / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

        env = target / "environment"
        (env / "files").mkdir(exist_ok=True)
        (env / "files" / ".gitkeep").touch()
        shutil.copyfile(self.upstream_dir / "requirements.txt", env / "requirements.txt")

        task = target / "task.toml"
        task.write_text(
            task.read_text(encoding="utf-8")
            .replace("{task_id}", record["id"])
            .replace("{task_type}", record["type"]),
            encoding="utf-8",
        )
        instruction = target / "instruction.md"
        attachments = "\n".join(
            f"- `/workspace/files/{filename}`" for filename in (record.get("files") or [])
        )
        instruction.write_text(
            instruction.read_text(encoding="utf-8").format(
                question=record["question"],
                attachments=f"\nSupporting files:\n{attachments}\n" if attachments else "",
            ),
            encoding="utf-8",
        )

        tests = target / "tests"
        shutil.copytree(
            self.upstream_dir / "eval", tests / "eval",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "predict.py"),
        )
        add_json_output_instruction(tests / "eval" / "judge_templates.yaml")
        shutil.copyfile(self.upstream_dir / "LICENSE", tests / "UPSTREAM-LICENSE")
        (tests / "sample.json").write_text(
            json.dumps([record], ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (target / "solution" / "reference.txt").write_text(record["answer"], encoding="utf-8")
