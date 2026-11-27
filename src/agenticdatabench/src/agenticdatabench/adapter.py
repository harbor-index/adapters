"""Generate Harbor tasks from AgenticDataBench (246 public data-agent tasks).

Each task gives the agent the contents of one domain folder of the Hugging Face
dataset in /workspace and a written analysis request; the agent saves output
files (CSV, JSON, PNG, TXT, SQLite) there. Harbor copies those files into a
fresh container, where the verifier grades them against the gold files with
upstream's own metric functions (tests/metrics, vendored from the GitHub repo
at a pinned commit). The data is fetched when the image is
built, so the generated task folders stay small.
"""

from __future__ import annotations

import json
import logging
import random
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import HfApi

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "task-template"

SOURCE_REPO = "https://github.com/AgenticDataBench/AgenticDataBench.git"
SOURCE_COMMIT = "e81dda8eb311fe5746b21248d9cf1be980ab55d4"
DATA_REPO = "shawnzzzh/AgenticDataBench"
DATA_REVISION = "3b0ac3fde63fd615de92bf70c1dd93b73f92d92f"

WORK_DIR = "/workspace"

# Tasks left out of the generated dataset, with the reason. Source:
# excluded_tasks.json next to this file; see the README.
EXCLUDED_TASKS: dict[str, str] = json.loads((Path(__file__).parent / "excluded_tasks.json").read_text())

# Parity subset (`--split parity`): PARITY_SIZE of the kept tasks, spread over
# the domains in proportion to their size (at least 1 each, largest-remainder
# rounding), sampled with one random.Random(PARITY_SEED) over the domains in
# sorted order.
PARITY_SEED = 0
PARITY_SIZE = 50
SPLITS = ("full", "parity")

# The prompt upstream's Claude Code and Codex runners send
# (testbed/da_agent/agent/claude_code.py and codex.py, _build_task_prompt).
PROMPT_SUFFIX = (
    "\n\nYou are working in the directory: {work_dir}."
    " All required data files are available in this directory."
    " Complete the task and ensure all output files are saved in this directory."
)
PLOTTING_INSTRUCTIONS = """
### Plotting (REQUIRED)

If you create a matplotlib plot, you MUST call:

    from image import Plotprocess
    Plotprocess.plot_process(fig, "<image_file_name>")

Use ONLY these file names:
{names}

Rules:
- Call AFTER plotting is complete
- Call BEFORE saving the figure
- Use: fig = plt.gcf()
- Replace <image_file_name> with one from the list above

Example:
```python
from image import Plotprocess
import matplotlib.pyplot as plt

# plotting code ...

fig = plt.gcf()
Plotprocess.plot_process(fig, "{first}")
```"""

IMAGE_HELPER = """
# Chart tasks: upstream copies its plot helper (da_agent/configs/scripts/image.py)
# into the work dir; the instruction tells the agent to call it.
COPY image.py /workspace/image.py
"""

COMPARE_IMAGE_RE = re.compile(r"\s*compare_image\(\s*(?:output_file_name\s*=\s*)?['\"]([^'\"]+)['\"]")


def as_list(value) -> list:
    return value if isinstance(value, list) else [value]


@dataclass
class DataFile:
    repo_path: str
    rel_path: str
    size: int
    hash_kind: str
    hash: str


@dataclass
class AdbTask:
    config: dict

    @property
    def id(self) -> str:
        return self.config["id"]

    @property
    def domain(self) -> str:
        """Folder under datasets/ (e.g. "agriculture", or "loan_risk/loan_risk_1")."""
        return self.config["domain"]

    @property
    def outputs(self) -> list[str]:
        return as_list(self.config["output_file_name"])

    @property
    def golds(self) -> list[str]:
        return as_list(self.config["gold_file_name"])

    @property
    def image_outputs(self) -> list[str]:
        """Charts graded by compare_image: upstream's image_post_process files.

        Upstream's runners pass the agent the plot helper and the plotting
        instructions for the outputs named in a task's post_process_func. The
        current tasks/dev.jsonl no longer carries that field; the names are the
        first argument of each compare_image check, which gives the same list
        as post_process_func in the earlier dev.jsonl (682960d) for all 40 chart
        tasks.
        """
        names = []
        for func in as_list(self.config["eval_func"]):
            m = COMPARE_IMAGE_RE.match(func)
            if m:
                names.append(m.group(1))
        return names


class AgenticDataBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        source_dir: Path | None = None,
        include_excluded: bool = False,
        split: str = "full",
        **kwargs,
    ):
        if split not in SPLITS:
            raise ValueError(f"Unknown split {split!r}; expected one of {SPLITS}")
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = set(task_ids) if task_ids else None
        self.source_dir = Path(source_dir) if source_dir else None
        self.include_excluded = include_excluded
        self.split = split
        self._tmp: tempfile.TemporaryDirectory | None = None

    # ------------------------------------------------------------------ sources
    def _source(self) -> Path:
        """Local checkout of the AgenticDataBench GitHub repo at SOURCE_COMMIT."""
        if self.source_dir is not None:
            return self.source_dir
        self._tmp = tempfile.TemporaryDirectory(prefix="agenticdatabench-")
        repo = Path(self._tmp.name) / "AgenticDataBench"
        for cmd in (
            ["git", "init", "-q", str(repo)],
            ["git", "-C", str(repo), "remote", "add", "origin", SOURCE_REPO],
            ["git", "-C", str(repo), "fetch", "-q", "--depth", "1", "origin", SOURCE_COMMIT],
            ["git", "-C", str(repo), "checkout", "-q", "FETCH_HEAD"],
        ):
            subprocess.run(cmd, check=True)
        return repo

    @staticmethod
    def _data_files() -> dict[str, list[DataFile]]:
        """Files of the pinned dataset revision, grouped by domain folder."""
        by_domain: dict[str, list[DataFile]] = {}
        for entry in HfApi().list_repo_tree(
            DATA_REPO, repo_type="dataset", revision=DATA_REVISION, recursive=True, path_in_repo="datasets"
        ):
            if not hasattr(entry, "size") or entry.size is None:
                continue  # a folder
            parts = entry.path.split("/")
            # datasets/<domain>/...; loan_risk tasks use datasets/loan_risk/<task>/...
            depth = 3 if parts[1] == "loan_risk" else 2
            domain = "/".join(parts[1:depth])
            rel = "/".join(parts[depth:])
            if entry.lfs is not None:
                kind, digest = "sha256", entry.lfs.sha256
            else:
                kind, digest = "gitsha1", entry.blob_id
            by_domain.setdefault(domain, []).append(DataFile(entry.path, rel, entry.size, kind, digest))
        return by_domain

    def load_tasks(self, source: Path) -> list[AdbTask]:
        lines = (source / "testbed" / "tasks" / "dev.jsonl").read_text().splitlines()
        tasks = [AdbTask(json.loads(line)) for line in lines if line.strip()]
        if len(tasks) != 246:
            raise RuntimeError(f"Expected 246 tasks in dev.jsonl, found {len(tasks)}")
        return tasks

    @staticmethod
    def parity_ids(ids: list[str]) -> set[str]:
        """The parity subset, from the ids of the kept (non-excluded) tasks.

        A task's domain is its id without the trailing number (loan_risk_12 ->
        loan_risk), which matches the top-level folder of its data.
        """
        by_domain: dict[str, list[str]] = {}
        for i in ids:
            by_domain.setdefault(re.sub(r"_\d+$", "", i), []).append(i)
        total = sum(len(v) for v in by_domain.values())
        quota = {d: PARITY_SIZE * len(v) / total for d, v in by_domain.items()}
        counts = {d: max(1, int(q)) for d, q in quota.items()}
        # Settle the rounding: add to the domain furthest below its share, or
        # take from the one furthest above it, never below 1 (ties: the larger
        # domain gains first and loses last, then by name).
        while sum(counts.values()) < PARITY_SIZE:
            d = min(by_domain, key=lambda d: (counts[d] - quota[d], -len(by_domain[d]), d))
            counts[d] += 1
        while sum(counts.values()) > PARITY_SIZE:
            d = max((d for d in by_domain if counts[d] > 1), key=lambda d: (counts[d] - quota[d], -len(by_domain[d]), d))
            counts[d] -= 1
        rng = random.Random(PARITY_SEED)
        picked: set[str] = set()
        for d in sorted(by_domain):
            picked.update(rng.sample(sorted(by_domain[d]), counts[d]))
        return picked

    def _selected(self, tasks: list[AdbTask]) -> list[AdbTask]:
        parity = None
        if self.split == "parity":
            parity = self.parity_ids([t.id for t in tasks if t.id not in EXCLUDED_TASKS])
        out = []
        for t in tasks:
            if t.id in EXCLUDED_TASKS and not self.include_excluded:
                continue
            if parity is not None and t.id not in parity:
                continue
            if self.task_ids is not None and t.id not in self.task_ids:
                continue
            out.append(t)
        if self.limit is not None:
            out = out[: self.limit]
        return out

    # --------------------------------------------------------------- rendering
    @staticmethod
    def instruction(task: AdbTask) -> str:
        text = task.config["question"] + PROMPT_SUFFIX.format(work_dir=WORK_DIR)
        images = task.image_outputs
        if images:
            text += PLOTTING_INSTRUCTIONS.format(names=", ".join(images), first=images[0])
        return text

    @staticmethod
    def artifacts(task: AdbTask) -> list[str]:
        """The files the separate verifier gets, as paths in the agent's container.

        Every output the task asks for (the grader reads each one from the work
        dir by base name), plus the plot data next to each graded chart:
        compare_image reads <chart>.json and <chart>.npy, which
        Plotprocess.plot_process writes beside the chart.
        """
        names = [Path(o).name for o in task.outputs]
        for img in task.image_outputs:
            stem = Path(img).stem
            names += [stem + ".json", stem + ".npy"]
        return [f"{WORK_DIR}/{n}" for n in dict.fromkeys(names)]

    @staticmethod
    def resources(files: list[DataFile]) -> tuple[int, int]:
        """(memory_mb, storage_mb) for a task, from its input data.

        Upstream runs the agent container without limits; these leave room to
        load the largest input with pandas and keep the data plus outputs on disk.
        """
        largest = max(f.size for f in files) / 2**20
        total = sum(f.size for f in files) / 2**20
        if largest <= 200:
            memory = 4096
        elif largest <= 1024:
            memory = 8192
        else:
            memory = 16384
        storage = max(10240, int((2 * total + 8192) // 1024 + 1) * 1024)
        return memory, storage

    @staticmethod
    def _render(path: Path, **values: str) -> None:
        text = path.read_text()
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        path.write_text(text)

    def generate_task(self, task: AdbTask, source: Path, files: list[DataFile]) -> None:
        task_dir = self.output_dir / task.id
        if task_dir.exists():
            if not self.overwrite:
                logger.info("Skipping %s (exists; use --overwrite)", task.id)
                return
            shutil.rmtree(task_dir)
        shutil.copytree(TEMPLATE_DIR, task_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        # No newline at the end: Harbor sends instruction.md to the agent as it
        # is, and upstream's prompt doesn't end with one.
        (task_dir / "instruction.md").write_text(self.instruction(task))

        env = task_dir / "environment"
        manifest = "".join(
            f"{DATA_REVISION}\t{f.repo_path}\t{f.rel_path}\t{f.size}\t{f.hash_kind}\t{f.hash}\n"
            for f in sorted(files, key=lambda f: f.repo_path)
        )
        (env / "data_manifest.tsv").write_text(manifest)
        images = task.image_outputs
        if not images:
            (env / "image.py").unlink()
        self._render(env / "Dockerfile", domain=task.domain, image_helper=IMAGE_HELPER if images else "")

        memory, storage = self.resources(files)
        out_types = sorted({Path(o).suffix.lstrip(".").lower() for o in task.outputs})
        domain_keyword = task.domain.split("/")[0]
        self._render(
            task_dir / "task.toml",
            task_id=task.id,
            domain_keyword=domain_keyword,
            extra_keywords=", \"charts\"" if images else "",
            output_types=", ".join(f'"{t}"' for t in out_types),
            artifacts=", ".join(json.dumps(a) for a in self.artifacts(task)),
            memory_mb=str(memory),
            storage_mb=str(storage),
        )

        # Verifier: the task's eval config and its gold files.
        gold_src = source / "testbed" / "gold" / task.id
        tests = task_dir / "tests"
        config = {k: task.config[k] for k in ("id", "domain", "output_file_name", "gold_file_name", "eval_func")}
        (tests / "task_config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        shutil.copytree(gold_src, tests / "gold")

        # Oracle: the gold files under the output names (paired by position, as
        # upstream lists them), plus the plot data next to each graded chart.
        if len(task.outputs) != len(task.golds):
            raise ValueError(f"{task.id}: {len(task.outputs)} outputs but {len(task.golds)} gold files")
        out = task_dir / "solution" / "output"
        out.mkdir(parents=True)
        pairs = dict(zip(task.outputs, task.golds))
        for o, g in pairs.items():
            if Path(o).suffix != Path(g).suffix:
                raise ValueError(f"{task.id}: output {o} and gold {g} have different types")
            shutil.copy2(gold_src / Path(g).name, out / Path(o).name)
        for img in images:
            g = Path(pairs[img])
            for ext in (".json", ".npy"):
                src = gold_src / (g.stem + ext)
                if not src.exists():
                    raise FileNotFoundError(f"{task.id}: no plot data {src.name} for {img}")
                if Path(img).stem + ext in task.outputs:
                    raise ValueError(f"{task.id}: plot data for {img} clashes with a graded output")
                shutil.copy2(src, out / (Path(img).stem + ext))

        for script in ("solution/solve.sh", "tests/test.sh"):
            (task_dir / script).chmod(0o755)

    def run(self) -> None:
        source = self._source()
        try:
            selected = self._selected(self.load_tasks(source))
            data = self._data_files()
            self.output_dir.mkdir(parents=True, exist_ok=True)
            for task in selected:
                if task.domain not in data:
                    raise RuntimeError(f"{task.id}: no data for domain {task.domain}")
                self.generate_task(task, source, data[task.domain])
            logger.info("Wrote %d tasks to %s", len(selected), self.output_dir)
        finally:
            if self._tmp is not None:
                self._tmp.cleanup()
