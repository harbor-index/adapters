"""Pinned, checksum-verified data and stable single-turn task selection."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from .protocol import CONDITIONS, SAMPLE_FIELDS

UPSTREAM_COMMIT = "8f3175f66fdff89ded3ebdf0ee2055fa47c955db"
DATASET_REVISION = "38b412da565073483c0a68ce9e04eb15859beedc"
DATASET_ID = "skyai798/DeceptionBench"
DATA_HASHES = {
    "economy": "1e0786966b09bc8801097e20ceecb33960aaeb253d2a94a67ec1eec677bb5d88",
    "education": "1619c24f315bd76de37ecfa190d6a8da5c4a41bc06fdae2738ad136a1a3bba62",
    "entertainment": "4f788de99ae9346ce6214b1dd8fdb28de1f81f8b897566f66ad05a7e18b6f2f4",
    "healthcare": "2d80b989cd26c53795a877d282b3afd4220c2baf7ad2bcd19f6eba7446759b23",
    "social": "4924b22d258081cafd35cf66f0fd345aeef1fa9e66742ac33def465ecc21e6e8",
}
DOMAINS = tuple(DATA_HASHES)
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "deceptionbench"


@dataclass(frozen=True)
class SampleTask:
    task_id: str
    domain: str
    condition: str
    sample: dict[str, str]


def _verified_bytes(path: Path, domain: str) -> bytes:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DATA_HASHES[domain]:
        raise ValueError(f"Dataset checksum mismatch: {path}")
    return raw


def _data_path(domain: str, data_dir: Path | None, cache_dir: Path) -> Path:
    if data_dir is not None:
        return data_dir / f"{domain}.json"
    directory = cache_dir / DATASET_REVISION
    path = directory / f"{domain}.json"
    if path.exists():
        return path
    url = f"https://huggingface.co/datasets/{DATASET_ID}/resolve/{DATASET_REVISION}/{domain}.json"
    with urlopen(url, timeout=60) as response:
        raw = response.read()
    if hashlib.sha256(raw).hexdigest() != DATA_HASHES[domain]:
        raise ValueError(f"Downloaded dataset checksum mismatch: {domain}")
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{domain}-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def load_tasks(
    data_dir: Path | None, cache_dir: Path = DEFAULT_CACHE_DIR
) -> list[SampleTask]:
    """Load all five fixed files; never silently accept stale or modified data."""
    tasks = []
    for domain in DOMAINS:
        records = json.loads(
            _verified_bytes(_data_path(domain, data_dir, cache_dir), domain)
        )
        if not isinstance(records, list) or len(records) != 30:
            raise ValueError(f"Expected 30 scenarios in {domain}")
        dimensions: Counter[str] = Counter()
        for index, sample in enumerate(records):
            if not isinstance(sample, dict) or any(
                not isinstance(sample.get(field), str) or not sample[field].strip()
                for field in (*SAMPLE_FIELDS, *CONDITIONS)
            ):
                raise ValueError(f"Invalid scenario {domain}:{index}")
            dimensions[sample["dimension"]] += 1
            for condition in CONDITIONS:
                task_id = f"deceptionbench-{domain}-{index:03d}-{condition.lower()}"
                tasks.append(SampleTask(task_id, domain, condition, sample))
        if sorted(dimensions.values()) != [10, 10, 10]:
            raise ValueError(f"Expected three ten-scenario dimensions in {domain}")
    return tasks


def select_tasks(
    tasks: list[SampleTask],
    *,
    limit: int | None = None,
    task_ids: list[str] | None = None,
    domains: list[str] | None = None,
    conditions: list[str] | None = None,
    split: str = "full",
) -> list[SampleTask]:
    """Filter in canonical order, then limit; the diagnostic split is fixed.

    ``parity`` selects the first pinned scenario per domain/dimension, with all
    six conditions. This is a proposed 90-task diagnostic set, not measured
    parity evidence or an upstream split.
    """
    if limit is not None and limit < 1:
        raise ValueError("--limit must be positive")
    if split not in ("full", "parity"):
        raise ValueError(f"Unknown split: {split}")
    for label, selected, valid in (
        ("domain", domains, set(DOMAINS)),
        ("condition", conditions, set(CONDITIONS)),
        ("task ID", task_ids, {task.task_id for task in tasks}),
    ):
        unknown = set(selected or ()) - valid
        if unknown:
            raise ValueError(f"Unknown {label}: {', '.join(sorted(unknown))}")
    chosen = tasks
    if split == "parity":
        first_scenarios: dict[tuple[str, str], str] = {}
        chosen = []
        for task in tasks:
            key = (task.domain, task.sample["dimension"])
            scenario = task.task_id.rsplit("-l", 1)[0]
            first_scenarios.setdefault(key, scenario)
            if scenario == first_scenarios[key]:
                chosen.append(task)
    chosen = [
        task
        for task in chosen
        if (not task_ids or task.task_id in task_ids)
        and (not domains or task.domain in domains)
        and (not conditions or task.condition in conditions)
    ]
    if task_ids and set(task_ids) - {task.task_id for task in chosen}:
        raise ValueError(
            "Requested task IDs conflict with the split/domain/condition filters"
        )
    chosen = chosen[:limit]
    if not chosen:
        raise ValueError("Selection contains no tasks")
    return chosen
