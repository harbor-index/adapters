"""
AIRS-Bench -> Harbor adapter.

AIRS-Bench (Meta FAIR, arXiv:2602.06855) has 20 open-ended ML research tasks. Each
task is a <problem, dataset, metric> triplet: the agent gets train data plus test
inputs and must write ``submission.csv``; the submission is scored on held-out test
labels of a *public* HuggingFace dataset and compared with a literature SOTA value.

Harbor mapping (one Harbor task per AIRS-Bench task):

* ``environment/`` : Dockerfile builds the agent-visible ``/workspace/data`` by running
  the original ``prepare.py`` at image build time (raw data is deleted afterwards).
* ``tests/``       : ``test.sh`` re-downloads the raw dataset, runs the original
  ``evaluate_prepare.py`` + ``evaluate.py`` and turns the metric into a reward with the
  paper's normalized score (clipped to [0, 1]).
* ``solution/``    : oracle submits the gold test labels, which must give reward 1.0.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

AIRS_REPO_URL = "https://github.com/facebookresearch/airs-bench.git"
TEMPLATE_DIR = Path(__file__).parent / "task-template"
SHARED_DIR = Path(__file__).parent / "shared"
ORG = "facebookresearch"

# Files from the original task that live in the verifier / oracle only.
AUX_FILES = ("utils.py", "custom_labels.py", "testing_util.py")


# Memory-only patches to upstream evaluate.py (metric math untouched). The Kaggle web
# traffic test set has 145k long series; upstream materialises two full columns as python
# lists (>7 GB). A lazy batched view gives identical values with a small footprint.
_LAZY_CLS = '''

class _Lazy:
    """Read an Arrow column in batches instead of building one huge python list."""

    def __init__(self, ds, col):
        self.ds, self.col = ds, col

    def __len__(self):
        return len(self.ds)

    def __iter__(self):
        for batch in self.ds.select_columns([self.col]).iter(batch_size=500):
            yield from batch[self.col]

'''
EVALUATE_PATCHES = {
    "time-series-forecasting-kaggle-web-traffic-mase": [
        ("\n\ndef load_test_set():", _LAZY_CLS + "\ndef load_test_set():"),
        ('return dataset["label_target"]', 'return _Lazy(dataset, "label_target")'),
        ('train_targets = test_ds["target"]', 'train_targets = _Lazy(test_ds, "target")'),
    ],
}


def slugify(name: str) -> str:
    """CamelCase task directory name -> stable kebab-case task id."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "-", name)
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def normalized_score_params(meta: dict) -> dict:
    """Values the verifier needs to compute the paper's normalized score."""
    li = meta["logging_info"]
    sota = li["sota"][0]["sota_score"]
    return {
        "lower_is_better": bool(meta.get("metric_lower_is_better", False)),
        "worst": float(li["estimated_worst_score"]),
        "optimal": float(li["optimal_score"]),
        "sota": float(sota),
    }


def _phi(s: float, opt: float) -> float:
    d = abs(s - opt)
    return math.inf if d == 0 else -math.log10(d)


class AIRSBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        airs_repo: Path | None = None,
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = set(task_ids) if task_ids else None
        self.airs_repo = Path(airs_repo) if airs_repo else None

    # ------------------------------------------------------------------ source
    def _ensure_repo(self) -> Path:
        if self.airs_repo and self.airs_repo.exists():
            return self.airs_repo
        dest = Path(tempfile.gettempdir()) / "airs-bench"
        if not dest.exists():
            subprocess.run(
                ["git", "clone", "--depth", "1", AIRS_REPO_URL, str(dest)], check=True
            )
        return dest

    def _tasks(self, repo: Path) -> list[Path]:
        dirs = sorted(p for p in (repo / "airsbench/tasks/rad").iterdir() if p.is_dir())
        if self.task_ids:
            dirs = [d for d in dirs if d.name in self.task_ids or slugify(d.name) in self.task_ids]
        return dirs[: self.limit] if self.limit else dirs

    # ---------------------------------------------------------------- rendering
    @staticmethod
    def _render(text: str, values: dict) -> str:
        for k, v in values.items():
            text = text.replace("{{" + k + "}}", str(v))
        return text

    @staticmethod
    def _submission_header(desc: str) -> str | None:
        m = re.search(r"following header:\s*```\s*(.*?)\s*```", desc, re.S)
        return m.group(1).strip() if m else None

    def _generate_task(self, src: Path, out: Path) -> None:
        meta = yaml.safe_load((src / "metadata.yaml").read_text())
        li = meta["logging_info"]
        task_id = slugify(src.name)
        desc = (src / "project_description.md").read_text()
        params = normalized_score_params(meta)
        header = self._submission_header(desc) or li.get("scoring_column", "label")

        values = {
            "task_id": task_id,
            "task_name": src.name,
            "dataset": li["dataset"],
            "config": li["config"],
            "category": li["category"],
            "metric": li["metric"],
            "problem_statement": desc,
        }

        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(TEMPLATE_DIR, out)
        for f in out.rglob("*"):
            if f.is_file() and f.suffix in {".toml", ".md", ".sh", ".py", ""} | {".json"}:
                try:
                    f.write_text(self._render(f.read_text(), values))
                except UnicodeDecodeError:
                    pass

        # instruction.md = original description + Harbor-specific I/O contract
        (out / "instruction.md").write_text(
            self._render((TEMPLATE_DIR / "instruction.md").read_text(), values)
        )

        # Files shared by env build / verifier / oracle
        (out / "task_params.json").unlink(missing_ok=True)
        import json

        score_cfg = {**params, "metric": li["metric"], "header": header, "task_id": task_id}
        for sub in ("tests", "solution"):
            (out / sub / "score_config.json").write_text(json.dumps(score_cfg, indent=2))

        # environment build context: only prepare.py (+ helpers) — deleted after build
        build = out / "environment" / "build"
        build.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "prepare.py", build / "prepare.py")
        (build / "dataset.json").write_text(
            json.dumps({"dataset": li["dataset"], "config": li["config"]})
        )
        shutil.copy2(SHARED_DIR / "download_data.py", build / "download_data.py")
        for aux in AUX_FILES:
            if (src / aux).exists():
                shutil.copy2(src / aux, build / aux)

        # verifier + oracle: evaluate_prepare.py, evaluate.py (+ aux)
        for sub in ("tests", "solution"):
            for fname in ("evaluate_prepare.py", "evaluate.py", *AUX_FILES):
                if (src / fname).exists():
                    shutil.copy2(src / fname, out / sub / fname)
            if task_id in EVALUATE_PATCHES and (out / sub / "evaluate.py").exists():
                ev = out / sub / "evaluate.py"
                text = ev.read_text()
                for old, new in EVALUATE_PATCHES[task_id]:
                    assert old in text, f"patch target missing in {task_id}: {old}"
                    text = text.replace(old, new)
                ev.write_text(text)
            if (src / "testing_util.py").exists():  # APPS: pyext shim (see shared/pyext.py)
                shutil.copy2(SHARED_DIR / "pyext.py", out / sub / "pyext.py")
            for shared in ("download_data.py", "score.py" if sub == "tests" else "gold.py"):
                shutil.copy2(SHARED_DIR / shared, out / sub / shared)
            (out / sub / "dataset.json").write_text(
                json.dumps({"dataset": li["dataset"], "config": li["config"]})
            )

        for sh in list((out / "tests").glob("*.sh")) + list((out / "solution").glob("*.sh")):
            sh.chmod(0o755)

    # --------------------------------------------------------------------- run
    def run(self) -> None:
        repo = self._ensure_repo()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for src in self._tasks(repo):
            out = self.output_dir / slugify(src.name)
            if out.exists() and not self.overwrite:
                print(f"skip {out.name} (exists; use --overwrite)")
                continue
            self._generate_task(src, out)
            n += 1
            print(f"generated {out.name}")
        print(f"Done: {n} tasks -> {self.output_dir}")
