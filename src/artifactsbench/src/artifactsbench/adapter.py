import hashlib
import json
import shutil
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

DATASET_REV = "40e5597429a127963ebd03130851e898fb8ba982"
DATASET_URL = f"https://huggingface.co/datasets/tencent/ArtifactsBenchmark/resolve/{DATASET_REV}/artifacts_bench.json"
DATASET_SHA = "3bd4740421f2be075191a64173d3bca8bfb68897f3d58afccc96b733e04b56db"
TEMPLATE = Path(__file__).with_name("task-template")


@dataclass
class ArtifactsBenchAdapter:
    output_dir: Path
    dataset: Path | None = None
    task_ids: list[int] | None = None
    limit: int | None = None
    overwrite: bool = False
    reference_dir: Path | None = None
    split: str = "full"

    def _records(self):
        if self.dataset:
            data = self.dataset.read_bytes()
        else:
            with urlopen(DATASET_URL, timeout=90) as response:
                data = response.read()
        if hashlib.sha256(data).hexdigest() != DATASET_SHA:
            raise ValueError("Dataset SHA-256 differs from the pinned Tencent release")
        records = [json.loads(line) for line in data.splitlines() if line.strip()]
        if self.split == "parity":
            # Sample by stable ID before selecting a model or observing scores.
            def sample_key(row):
                return hashlib.sha256(f"artifactsbench-parity-v1/{row['index']}".encode()).digest()

            sample = sorted(records, key=sample_key)[:60]
            records = sorted(sample, key=lambda row: row["index"])
        if self.task_ids is not None:
            indices = set(self.task_ids)
            missing = indices.difference(row["index"] for row in records)
            if missing:
                raise ValueError(f"Task IDs outside the selected split: {sorted(missing)}")
            records = [row for row in records if row["index"] in indices]
        if self.limit is not None and self.limit < 1:
            raise ValueError("--limit must be positive")
        if self.reference_dir is not None and not self.reference_dir.is_dir():
            raise ValueError(f"Reference directory does not exist: {self.reference_dir}")
        return records[:self.limit]

    def _write_task(self, path, row):
        number = f"{row['index']:04d}"
        shutil.copytree(TEMPLATE, path, ignore=shutil.ignore_patterns("solution", "__pycache__", "*.pyc"))
        config = path / "task.toml"
        config.write_text(
            config.read_text().replace("__ID__", number)
            .replace("__INDEX__", str(row["index"]))
            .replace("__DIFFICULTY__", json.dumps(row["difficulty"]))
            .replace("__CLASS__", json.dumps(row["class"])), encoding="utf-8",
        )
        instruction = path / "instruction.md"
        instruction.write_text(instruction.read_text().replace("__QUESTION__", row["question"]), encoding="utf-8")
        (path / "tests" / "record.json").write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        candidate = self.reference_dir / f"{number}.md" if self.reference_dir else None
        if candidate and candidate.is_file():
            solution = path / "solution"
            shutil.copytree(TEMPLATE / "solution", solution)
            shutil.copyfile(candidate, solution / "answer.md")

    def run(self):
        records = self._records()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # One writer at a time. Stage replacements on the same filesystem.
        lock = self.output_dir / ".artifactsbench-generating"
        lock.mkdir()
        try:
            for row in records:
                slug = f"artifactsbench-{row['index']:04d}"
                target = self.output_dir / slug
                if target.is_symlink():
                    raise ValueError(f"Refusing symlink {target}")
                if target.exists():
                    if not self.overwrite:
                        raise ValueError(f"{target} exists. Use --overwrite to regenerate")
                    config = tomllib.loads((target / "task.toml").read_text())
                    if config.get("task", {}).get("name") != f"tencent/{slug}":
                        raise ValueError(f"Unrelated task directory: {target}")
                with tempfile.TemporaryDirectory(dir=lock) as temp:
                    new = Path(temp) / "new"
                    previous = Path(temp) / "previous"
                    self._write_task(new, row)
                    if target.exists():
                        target.rename(previous)
                    try:
                        new.rename(target)
                    except OSError:
                        if previous.exists():
                            previous.rename(target)
                        raise
        finally:
            lock.rmdir()
        return len(records)
