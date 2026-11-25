"""Generate Harbor tasks from the public DARE-Bench evaluation release.

One task per (entry, variant): 162 entries x {v1, v2} = 324 tasks. The agent sees only the
variant's `needed_files`; held-out labels, the clean training split used to simulate
instruction-following labels, and the scorer live in tests/.
"""

import ast
import csv
import json
import random
import re
import shutil
from pathlib import Path
from string import Template

TEMPLATE_DIR = Path(__file__).parent / "task-template"
PARITY_SEED = 20261119
PARITY_PER_CATEGORY = 5

FAMILY = {
    ("classification", "v1"): "IF",
    ("classification", "v2"): "MM",
    ("regression", "v1"): "IF",
    ("regression", "v2"): "MM",
    ("time_series_analysis", "v1"): "XF",
    ("time_series_analysis", "v2"): "CF",
}
METRIC = {
    ("classification", "v1"): "exact_match",
    ("classification", "v2"): "macro_f1",
    ("regression", "v1"): "allclose",
    ("regression", "v2"): "clipped_r2",
    ("time_series_analysis", "v1"): "clipped_r2",
    ("time_series_analysis", "v2"): "clipped_r2",
}


def _reference_template(upstream: Path) -> str:
    """Read REFERENCE_SOLUTION_TEMPLATE from upstream without importing it (it imports sklearn)."""
    tree = ast.parse((upstream / "scripts/reference_solution.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "REFERENCE_SOLUTION_TEMPLATE" for t in node.targets
        ):
            return node.value.value
    raise ValueError("REFERENCE_SOLUTION_TEMPLATE not found in upstream reference_solution.py")


def render_reference_code(template: str, question: dict) -> str:
    """Same substitution as upstream generate_reference_solution_code()."""
    targets = question["target"]
    if isinstance(targets, str):
        targets = [targets]
    return Template(template).substitute(
        feature_cols=question["selected_features"],
        model_type=question["model_type"],
        column_type_inference=question["column_type_inference"],
        target_cols=targets,
        imputer_type=question["imputer_type"],
        problem_type=question["problem_type"],
        random_state=question["random_state"],
        save_file_type=question["save_file_type"],
    )


def task_id_for(file_path: str, variant: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", file_path.lower()).strip("-")
    return f"{slug}-{variant}"


class DAREBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        source_dir: Path | None = None,
        split: str = "full",
        variant: str = "all",
    ):
        if source_dir is None:
            raise ValueError("--source-dir (a checkout of Snowflake-Labs/dare-bench) is required")
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = set(task_ids) if task_ids else None
        self.source_dir = Path(source_dir)
        self.split = split
        self.variant = variant
        self.eval_root = self.source_dir / "data/eval"
        self.databases = self.eval_root / "databases"
        self.reference_template = _reference_template(self.source_dir)
        with open(self.source_dir / "LICENSE/kaggle_license_test.csv", newline="") as f:
            self.licenses = {row["folder_name"]: row["license_name"] for row in csv.DictReader(f)}

    def all_tasks(self) -> list[dict]:
        questions = json.loads((self.eval_root / "question_list.json").read_text())
        tasks = []
        for q in questions:
            for v in ("v1", "v2"):
                tasks.append({"entry": q, "variant": v, "task_id": task_id_for(q["file_path"], v)})
        ids = [t["task_id"] for t in tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task id collision after sanitizing file_path")
        return sorted(tasks, key=lambda t: t["task_id"])

    def parity_ids(self, tasks: list[dict]) -> set[str]:
        """Fixed sample: PARITY_PER_CATEGORY tasks from each of the six task families."""
        rng = random.Random(PARITY_SEED)
        chosen = set()
        for key in sorted(FAMILY):
            pool = sorted(t["task_id"] for t in tasks if (t["entry"]["task"], t["variant"]) == key)
            chosen.update(rng.sample(pool, PARITY_PER_CATEGORY))
        return chosen

    def selected_tasks(self) -> list[dict]:
        tasks = self.all_tasks()
        if self.split == "parity":
            keep = self.parity_ids(tasks)
            tasks = [t for t in tasks if t["task_id"] in keep]
        if self.variant != "all":
            tasks = [t for t in tasks if t["variant"] == self.variant]
        if self.task_ids is not None:
            unknown = self.task_ids - {t["task_id"] for t in tasks}
            if unknown:
                raise ValueError(f"unknown task ids: {sorted(unknown)}")
            tasks = [t for t in tasks if t["task_id"] in self.task_ids]
        if self.limit is not None:
            tasks = tasks[: self.limit]
        return tasks

    def run(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        tasks = self.selected_tasks()
        for t in tasks:
            self.generate_task(t)
        print(f"Generated {len(tasks)} tasks in {self.output_dir}")

    def generate_task(self, task: dict) -> None:
        entry, variant, task_id = task["entry"], task["variant"], task["task_id"]
        kind, file_path = entry["task"], entry["file_path"]
        source = self.databases / file_path / "source"
        verify = self.databases / file_path / "verify"
        question = json.loads((verify / "all_metadata.json").read_text())["question"]
        targets = question["target"] if isinstance(question["target"], list) else [question["target"]]

        out = self.output_dir / task_id
        if out.exists():
            if not self.overwrite:
                print(f"Skipping {task_id} (exists; use --overwrite)")
                return
            if out.resolve().parent != self.output_dir.resolve():
                raise ValueError(f"refusing to delete {out}: not directly under the output dir")
            shutil.rmtree(out)
        (out / "environment/workspace").mkdir(parents=True)
        (out / "tests/labels").mkdir(parents=True)
        (out / "solution").mkdir(parents=True)

        # Agent-visible inputs: exactly the variant's needed_files (upstream adds metadata.txt).
        inputs = list(entry[f"needed_files_{variant}"])
        if "metadata.txt" not in inputs:
            inputs.append("metadata.txt")
        for name in inputs:
            if Path(name).name != name:
                raise ValueError(f"{task_id}: unsafe input name {name!r}")
            shutil.copy(source / name, out / "environment/workspace" / name)
        shutil.copy(TEMPLATE_DIR / "environment/Dockerfile", out / "environment/Dockerfile")
        shutil.copy(TEMPLATE_DIR / "environment/requirements.txt", out / "environment/requirements.txt")

        file_list = ", ".join(f"`{n}`" for n in inputs)
        instruction = (TEMPLATE_DIR / "instruction.md").read_text().format(
            file_list=file_list, question=entry[f"question_{variant}"]
        )
        (out / "instruction.md").write_text(instruction)

        key = (kind, variant)
        (out / "task.toml").write_text(
            (TEMPLATE_DIR / "task.toml").read_text().format(
                task_id=task_id,
                task_type=kind,
                family=FAMILY[key],
                file_path=file_path,
                variant=variant,
                metric=METRIC[key],
                license=self.licenses.get(file_path, "unknown"),
            )
        )

        # Verifier side.
        spec = {"task_id": task_id, "file_path": file_path, "variant": variant, "task": kind, "metric": METRIC[key]}
        if FAMILY[key] == "IF":
            ext = question["save_file_type"]
            ref_dir = out / "tests/reference"
            ref_dir.mkdir()
            reference_inputs = [f"train_v1_no_err.{ext}", f"val_v1.{ext}"]
            for name in reference_inputs:
                shutil.copy(source / name, ref_dir / name)
            (ref_dir / "reference_solution.py").write_text(render_reference_code(self.reference_template, question))
            spec.update(label="reference", reference_inputs=reference_inputs)
        else:
            label = {"v1": "ground_truth_v1.csv", "v2": "ground_truth_v2.csv"}[variant] if kind == "time_series_analysis" else "ground_truth.csv"
            shutil.copy(verify / label, out / "tests/labels" / label)
            spec.update(label=label)
        (out / "tests/spec.json").write_text(json.dumps(spec, indent=1))
        # Only the fields evaluate_prediction reads from all_metadata.json.
        (out / "tests/metadata.json").write_text(
            json.dumps({"question": {"target": targets, "problem_type": kind}}, indent=1)
        )
        for name in ("test.sh", "verify.py", "dare_evaluation.py"):
            shutil.copy(TEMPLATE_DIR / "tests" / name, out / "tests" / name)

        # Solution side: parameters the instruction states, in machine-readable form.
        if FAMILY[key] == "IF":
            features = question["selected_features"]
            types = question["column_type_inference"]
            solver_spec = {
                "problem_type": kind,
                "model_type": question["model_type"],
                "random_state": question["random_state"],
                "target": targets,
                "features": features,
                "file_type": question["save_file_type"],
                "expected_ranges": question["expected_ranges"],
                "imputer": question["imputer_type"],
                "numeric_features": [c for c in features if types[c].lower() == "numerical"],
                "categorical_features": [c for c in features if types[c].lower() == "categorical"],
            }
            solver = "if_solver.py"
        else:
            files = entry[f"needed_files_{variant}"]
            solver_spec = {
                "problem_type": kind,
                "target": targets,
                "train_file": next(f for f in files if f.startswith("train")),
                "test_file": next(f for f in files if f.startswith("val")),
                "time_column": question.get("time_column"),
                "time_format": question.get("time_format"),
                "output_ids": "all_test_columns" if key == ("time_series_analysis", "v2") else "row_id",
            }
            solver = "mm_solver.py"
        (out / "solution/spec.json").write_text(json.dumps(solver_spec, indent=1))
        shutil.copy(TEMPLATE_DIR / "solution" / solver, out / "solution" / solver)
        (out / "solution/solve.sh").write_text((TEMPLATE_DIR / "solution/solve.sh").read_text().replace("{solver}", solver))
        for script in (out / "solution/solve.sh", out / "tests/test.sh"):
            script.chmod(0o755)
