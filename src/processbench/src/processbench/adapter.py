"""
ProcessBench adapter: converts each ProcessBench item into a Harbor task where the
agent must identify the earliest erroneous paragraph of a step-by-step math solution.
"""

import json
import os
import random
import shutil
import urllib.request
from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent / "task-template"

SUBSETS = ["gsm8k", "math", "olympiadbench", "omnimath"]
HF_REPO = "Qwen/ProcessBench"
HF_REVISION = "3bdcd5371ed567559a78f559c01c13a6deee7604"

# Parity subset: per subset, sample this many items stratified by error/correct.
PARITY_PER_SUBSET = 50
PARITY_SEED = 42


def download_subsets(data_dir: Path, subsets: list[str]) -> None:
    """Download the pinned ProcessBench JSON files (honours HF_ENDPOINT for mirrors)."""
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    data_dir.mkdir(parents=True, exist_ok=True)
    for subset in subsets:
        path = data_dir / f"{subset}.json"
        if path.exists():
            continue
        url = f"{endpoint}/datasets/{HF_REPO}/resolve/{HF_REVISION}/{subset}.json"
        print(f"Downloading {url}")
        with urllib.request.urlopen(url) as response:
            path.write_bytes(response.read())


def tag_steps(steps: list[str]) -> str:
    """Same paragraph tagging as prepare_input_boxed() in the original run_eval.py."""
    tagged_response = ""
    for sdx, step in enumerate(steps):
        tagged_response += f"<paragraph_{sdx}>\n{step}\n</paragraph_{sdx}>\n\n"
    return tagged_response.strip()


def sample_parity(records: list[dict], n: int, seed: int) -> list[dict]:
    """Stratified sample of one subset, keeping its error/correct ratio."""
    rng = random.Random(seed)
    errors = sorted((r for r in records if r["label"] != -1), key=lambda r: r["id"])
    corrects = sorted((r for r in records if r["label"] == -1), key=lambda r: r["id"])
    n_err = round(n * len(errors) / len(records))
    chosen = rng.sample(errors, n_err) + rng.sample(corrects, n - n_err)
    order = {r["id"]: i for i, r in enumerate(records)}
    return sorted(chosen, key=lambda r: order[r["id"]])


class ProcessBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        data_dir: Path | None = None,
        subsets: list[str] | None = None,
        split: str = "full",
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.data_dir = Path(data_dir) if data_dir else None
        self.subsets = subsets or SUBSETS
        self.split = split

    @staticmethod
    def task_dir_name(item_id: str) -> str:
        return f"processbench-{item_id}"

    def load_records(self, data_dir: Path) -> list[dict]:
        records = []
        for subset in self.subsets:
            items = json.loads((data_dir / f"{subset}.json").read_text())
            for item in items:
                item["subset"] = subset
            if self.split == "parity":
                items = sample_parity(items, PARITY_PER_SUBSET, PARITY_SEED)
            records.extend(items)
        return records

    def select(self, records: list[dict]) -> list[dict]:
        if self.task_ids:
            wanted = set(self.task_ids)
            records = [
                r
                for r in records
                if r["id"] in wanted or self.task_dir_name(r["id"]) in wanted
            ]
        if self.limit is not None:
            records = records[: max(0, self.limit)]
        return records

    def generate_task(self, record: dict) -> bool:
        task_dir = self.output_dir / self.task_dir_name(record["id"])
        if task_dir.exists():
            if not self.overwrite:
                return False
            shutil.rmtree(task_dir)
        shutil.copytree(TEMPLATE_DIR, task_dir)

        instruction = (TEMPLATE_DIR / "instruction.md").read_text()
        instruction = instruction.format(
            problem=record["problem"], tagged_response=tag_steps(record["steps"])
        )
        (task_dir / "instruction.md").write_text(instruction)

        task_toml = (TEMPLATE_DIR / "task.toml").read_text()
        for key, value in {
            "{task_id}": record["id"],
            "{subset}": record["subset"],
            "{has_error}": "true" if record["label"] != -1 else "false",
            "{generator}": record["generator"],
        }.items():
            task_toml = task_toml.replace(key, value)
        (task_dir / "task.toml").write_text(task_toml)

        solve_sh = (TEMPLATE_DIR / "solution/solve.sh").read_text()
        solve_sh = solve_sh.replace("{label}", str(record["label"]))
        (task_dir / "solution/solve.sh").write_text(solve_sh)

        ground_truth = {
            "id": record["id"],
            "subset": record["subset"],
            "label": record["label"],
        }
        (task_dir / "tests/ground_truth.json").write_text(
            json.dumps(ground_truth) + "\n"
        )

        for script in ("solution/solve.sh", "tests/test.sh"):
            (task_dir / script).chmod(0o755)
        return True

    def run(self) -> None:
        if self.data_dir is not None:
            records = self.load_records(self.data_dir)
        else:
            cache_dir = Path.home() / ".cache" / "harbor-processbench" / HF_REVISION
            download_subsets(cache_dir, self.subsets)
            records = self.load_records(cache_dir)

        selected = self.select(records)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated = sum(self.generate_task(r) for r in selected)
        print(
            f"Generated {generated} tasks ({len(selected) - generated} skipped, "
            f"already exist) in {self.output_dir}"
        )
