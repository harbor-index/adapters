"""
JAXBench -> Harbor adapter.

Each JAXBench workload (``JAXBench/benchmark/<level>/<workload>/baseline.py``)
becomes one Harbor task. The agent must write ``/workspace/kernel.py`` exporting
a ``workload(*inputs)`` function that is numerically equivalent to the baseline
and, ideally, faster on a TPU v6e. The verifier runs JAXBench's own
``python -m JAXBench evaluate`` harness unchanged.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

UPSTREAM_REPO = "https://github.com/AI-Hypercomputer/accelerator-agents.git"
# Pinned for reproducibility; bump deliberately (task content changes with it).
UPSTREAM_COMMIT = "7afeb78188383e857f8fe12d98d308eb1fd30435"
DEFAULT_LEVELS = ("level1",)

TEMPLATE_DIR = Path(__file__).parent / "task-template"

# Harness files copied verbatim from upstream ``JAXBench/``.
HARNESS_FILES = (
    "__init__.py",
    "__main__.py",
    "benchmark/__init__.py",
    "harness/__init__.py",
    "harness/correctness.py",
    "harness/evaluator.py",
    "harness/loader.py",
    "harness/profiler.py",
    "harness/runner.py",
    "harness/tpu_specs.py",
)


# Workloads that cannot be solved by any agent at UPSTREAM_COMMIT; excluded from the dataset.
EXCLUDED_WORKLOADS = {
    ("level1", "11p_Megablox_GMM"): (
        "baseline workload() fails under jax.jit: max_expert_size is passed as a traced "
        "argument but used as a static shape, so `evaluate` errors before grading any kernel"
    ),
}

# Workloads whose upstream optimized.py fails the upstream harness at UPSTREAM_COMMIT.
# The task stays valid for agents; the oracle falls back to the baseline (~1.0x).
BROKEN_PALLAS_ORACLES = {
    ("level1", "1p_Flash_Attention"): "output differs from baseline (max_diff 0.0625 > 1e-2)",
    ("level1", "2p_GQA_Attention"): "output differs from baseline (max_diff 0.0625 > 1e-2)",
    ("level1", "6p_Paged_Attention"): "workload() takes 5 args but create_inputs() returns 6",
    ("level1", "7p_Ragged_Paged_Attention"): "scoped VMEM OOM on v6e (34.39M > 32M limit)",
}


# Parity subset (15 of 49): 7 priority kernels (the 3 Pallas-oracle tasks plus 1p, 12p, 15p, 16p,
# which the JAXBench and MaxKernel papers report on) and every 4th KernelBench workload from 18k.
PARITY_WORKLOADS = {
    ("level1", name)
    for name in (
        "1p_Flash_Attention",
        "3p_MLA_Attention",
        "4p_Sparse_Attention",
        "8p_GEMM",
        "12p_RMSNorm",
        "15p_RetNet_Retention",
        "16p_Mamba2_SSD",
        "18k_Conv2D_ReLU_BiasAdd",
        "22k_Conv2d_InstanceNorm_Divide",
        "26k_BMM_InstanceNorm_Sum_ResidualAdd_Multiply",
        "30k_Matmul_Scaling_ResidualAdd",
        "34k_Conv2d_Activation_BatchNorm",
        "38k_Matmul_Dropout_Softmax",
        "42k_Gemm_Max_Subtract_GELU",
        "46k_Conv2d_GroupNorm_Tanh_HardSwish_ResidualAdd_LogSumExp",
    )
}


@dataclass(frozen=True)
class Workload:
    level: str  # e.g. "level1"
    name: str  # upstream workload id, e.g. "8p_GEMM"
    path: Path  # .../JAXBench/benchmark/<level>/<name>

    @property
    def task_id(self) -> str:
        """Registry-safe, stable id, e.g. ``level1-8p-gemm``.

        The level is part of the id because upstream reuses workload names
        across levels (e.g. ``8p_GEMM`` exists in level1 and level2).
        """
        return _sanitize(f"{self.level}-{self.name}")

    @property
    def has_optimized(self) -> bool:
        return (self.path / "optimized.py").is_file()

    @property
    def uses_pallas_oracle(self) -> bool:
        return self.has_optimized and (self.level, self.name) not in BROKEN_PALLAS_ORACLES


def _sanitize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _sort_key(name: str) -> tuple[int, str]:
    """Upstream ordering: numeric prefix first (1p, 2p, ..., 18k, ...)."""
    match = re.match(r"\d+", name)
    return (int(match.group()) if match else 999, name)


class JAXBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        source_dir: Path | None = None,
        levels: list[str] | None = None,
        oracle: str = "pallas",
        split: str = "full",
        **kwargs,
    ):
        """
        Args:
            output_dir: The directory to write the generated tasks to.
            limit: The maximum number of tasks to generate.
            overwrite: Whether to overwrite existing tasks.
            task_ids: Task IDs to generate. Accepts Harbor ids
                (``level1-8p-gemm``) or upstream workload names (``8p_GEMM``).
            source_dir: Local checkout of accelerator-agents. If omitted, the
                pinned upstream commit is cloned into a temporary directory.
            levels: JAXBench levels to include (default: level1).
            oracle: "pallas" uses upstream optimized.py as the oracle where it passes the
                harness (baseline elsewhere); "baseline" uses the XLA baseline for every task.
            split: "full" (all adapted tasks) or "parity" (the PARITY_WORKLOADS subset).
        """
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.source_dir = Path(source_dir) if source_dir else None
        self.levels = list(levels) if levels else list(DEFAULT_LEVELS)
        if oracle not in ("pallas", "baseline"):
            raise ValueError(f"oracle must be 'pallas' or 'baseline', got {oracle!r}")
        self.oracle = oracle
        if split not in ("full", "parity"):
            raise ValueError(f"split must be 'full' or 'parity', got {split!r}")
        self.split = split

    # ------------------------------------------------------------------ source

    @staticmethod
    def _clone_upstream(dest: Path) -> None:
        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=dest, check=True)

        dest.mkdir(parents=True, exist_ok=True)
        git("init", "-q")
        git("remote", "add", "origin", UPSTREAM_REPO)
        git("sparse-checkout", "set", "JAXBench")
        git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", UPSTREAM_COMMIT)
        git("checkout", "-q", "FETCH_HEAD")

    def _discover(self, jaxbench_root: Path) -> list[Workload]:
        workloads: list[Workload] = []
        for level in self.levels:
            level_dir = jaxbench_root / "benchmark" / level
            if not level_dir.is_dir():
                raise FileNotFoundError(f"JAXBench level not found: {level_dir}")
            for d in sorted(level_dir.iterdir(), key=lambda p: _sort_key(p.name)):
                if not (d.is_dir() and (d / "baseline.py").is_file()):
                    continue
                if (level, d.name) in EXCLUDED_WORKLOADS:
                    print(f"exclude: {level}/{d.name} ({EXCLUDED_WORKLOADS[(level, d.name)]})")
                    continue
                workloads.append(Workload(level=level, name=d.name, path=d))

        if self.split == "parity":
            workloads = [w for w in workloads if (w.level, w.name) in PARITY_WORKLOADS]
        if self.task_ids:
            wanted = set(self.task_ids)
            workloads = [w for w in workloads if w.task_id in wanted or w.name in wanted]
            found = {w.task_id for w in workloads} | {w.name for w in workloads}
            for missing in sorted(wanted - found):
                print(f"warning: task id not found (or not in this split): {missing}")
        if self.limit is not None:
            workloads = workloads[: self.limit]
        return workloads

    # -------------------------------------------------------------- generation

    @staticmethod
    def _copy_harness(jaxbench_root: Path, workload: Workload, dest: Path, include_optimized: bool) -> None:
        """Copy the JAXBench harness plus a single workload into ``dest/JAXBench``.

        Upstream's ``JAXBench.benchmark.get_workload_dir`` resolves workloads at
        ``benchmark/<name>/`` but the files now live at
        ``benchmark/<level>/<name>/``, so ``evaluate`` cannot find any workload
        at the pinned commit. We place the workload at ``benchmark/<name>/``
        (a path-only fix; no harness code is modified).
        """
        pkg = dest / "JAXBench"
        for rel in HARNESS_FILES:
            target = pkg / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(jaxbench_root / rel, target)

        wl_dir = pkg / "benchmark" / workload.name
        wl_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(workload.path / "baseline.py", wl_dir / "baseline.py")
        if include_optimized and workload.has_optimized:
            shutil.copy2(workload.path / "optimized.py", wl_dir / "optimized.py")

    @staticmethod
    def _render(template: Path, **values: str) -> str:
        text = template.read_text()
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        return text

    def _generate_task(self, jaxbench_root: Path, workload: Workload) -> None:
        task_dir = self.output_dir / workload.task_id
        if task_dir.exists():
            if not self.overwrite:
                print(f"skip (exists): {task_dir}")
                return
            shutil.rmtree(task_dir)

        use_pallas_oracle = self.oracle == "pallas" and workload.uses_pallas_oracle
        baseline_src = (workload.path / "baseline.py").read_text()
        values = {
            "task_id": workload.task_id,
            "workload": workload.name,
            "level": workload.level,
            "has_pallas_reference": "true" if workload.has_optimized else "false",
            "oracle": "pallas" if use_pallas_oracle else "baseline",
            "baseline_source": baseline_src.rstrip("\n"),
        }

        # task.toml + instruction.md
        task_dir.mkdir(parents=True)
        (task_dir / "task.toml").write_text(self._render(TEMPLATE_DIR / "task.toml", **values))
        (task_dir / "instruction.md").write_text(self._render(TEMPLATE_DIR / "instruction.md", **values))

        # environment/: agent workspace gets the baseline + harness, never optimized.py
        env_dir = task_dir / "environment"
        shutil.copytree(TEMPLATE_DIR / "environment", env_dir)
        workspace = env_dir / "workspace"
        workspace.mkdir()
        shutil.copy2(workload.path / "baseline.py", workspace / "baseline.py")
        self._copy_harness(jaxbench_root, workload, workspace, include_optimized=False)

        # tests/: pristine harness (incl. Pallas reference for the 3-way report)
        tests_dir = task_dir / "tests"
        shutil.copytree(TEMPLATE_DIR / "tests", tests_dir)
        (tests_dir / "test.sh").write_text(self._render(TEMPLATE_DIR / "tests" / "test.sh", **values))
        self._copy_harness(jaxbench_root, workload, tests_dir, include_optimized=True)

        # solution/: upstream hand-written Pallas kernel when it passes the harness, else baseline
        solution_dir = task_dir / "solution"
        shutil.copytree(TEMPLATE_DIR / "solution", solution_dir)
        oracle_src = workload.path / ("optimized.py" if use_pallas_oracle else "baseline.py")
        shutil.copy2(oracle_src, solution_dir / "kernel.py")

        for script in (tests_dir / "test.sh", solution_dir / "solve.sh"):
            script.chmod(0o755)
        print(f"wrote: {task_dir}")

    def run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="jaxbench-src-") as tmp:
            if self.source_dir is None:
                repo = Path(tmp) / "accelerator-agents"
                self._clone_upstream(repo)
            else:
                repo = self.source_dir
            jaxbench_root = repo / "JAXBench"

            workloads = self._discover(jaxbench_root)
            if not workloads:
                raise SystemExit("No matching JAXBench workloads found.")

            self.output_dir.mkdir(parents=True, exist_ok=True)
            for workload in workloads:
                self._generate_task(jaxbench_root, workload)
            print(f"Generated {len(workloads)} task(s) in {self.output_dir}")
