"""Map one upstream website project to one independently runnable Harbor task."""

from __future__ import annotations

import ast
import json
import re
import shutil
import tempfile
from pathlib import Path

from .pins import DATASET_REVISION, LEVELS, PILOT_IDS, RUNTIME_IMAGE, UPSTREAM_REVISION
from .sources import PACKAGE_DIR, catalog, check_upstream, sha256


def slug(row: dict) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", row["task_name"].lower()).strip("-")
    return f"vision2web-{row['level']}-{name}"


def copy_tree(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Refusing symlink in input tree: {path}")
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "._*"),
    )


def load_prompts(upstream: Path) -> dict[str, str]:
    # Read the pinned literals without importing or executing source code.
    values = {}
    for node in ast.parse((upstream / "vision2web/inference/prompts.py").read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = ast.literal_eval(node.value)
    return {level: values[f"{level.upper()}_PROMPT"] for level in LEVELS}


class Vision2WebAdapter:
    def __init__(
        self,
        output_dir: Path,
        dataset_dir: Path,
        upstream_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "full",
        reference_dir: Path | None = None,
        runtime_image: str = RUNTIME_IMAGE,
        memory_mb: int = 8192,
    ):
        self.output_dir = output_dir.resolve()
        self.dataset_dir = dataset_dir.resolve()
        self.upstream_dir = upstream_dir.resolve()
        self.limit, self.overwrite, self.task_ids = limit, overwrite, task_ids
        self.split, self.runtime_image, self.memory_mb = split, runtime_image, memory_mb
        self.reference_dir = reference_dir.resolve() if reference_dir else None

    def selected(self) -> list[dict]:
        rows = catalog()
        if len({slug(r) for r in rows}) != len(rows):
            raise ValueError("Sanitized task names collide")
        if self.split == "pilot":
            rows = [r for r in rows if r["id"] in PILOT_IDS]
        elif self.split in LEVELS:
            rows = [r for r in rows if r["level"] == self.split]
        if self.task_ids:
            aliases = {
                alias: r["id"] for r in rows for alias in [r["id"], slug(r), "zai-org/" + slug(r)]
            }
            unknown = sorted(set(self.task_ids) - aliases.keys())
            if unknown:
                raise ValueError(f"Unknown task IDs for split {self.split}: {unknown}")
            chosen = {aliases[name] for name in self.task_ids}
            rows = [r for r in rows if r["id"] in chosen]
        if self.limit is not None:
            if self.limit < 1:
                raise ValueError("--limit must be positive")
            rows = rows[: self.limit]
        return rows

    def _validate_project(self, row: dict) -> Path:
        project = self.dataset_dir / row["level"] / row["task_name"]
        if not (project / "workflow.json").is_file():
            raise FileNotFoundError(f"Missing complete task inputs: {project}; use --download")
        workflows = json.loads((project / "workflow.json").read_text())
        if not isinstance(workflows, list) or len(workflows) != row["workflow_steps"]:
            raise ValueError(f"Workflow count differs from the pinned catalog: {row['id']}")
        cases = sum(
            bool(case.get("validations")) for wf in workflows for case in wf.get("content", [])
        )
        if cases != row["num_test_cases"]:
            raise ValueError(f"Functional denominator differs from the catalog: {row['id']}")
        if {p.name for p in (project / "prototypes").glob("*.jpg")} != set(row["prototypes"]):
            raise ValueError(f"Prototype inventory differs from the catalog: {row['id']}")
        requirement = {"frontend": "prompt.txt", "website": "prd.md"}.get(row["level"])
        if requirement and not (project / requirement).is_file():
            raise FileNotFoundError(project / requirement)
        if row["resources_count"] and not (project / "resources").is_dir():
            raise FileNotFoundError(project / "resources")
        return project

    def run(self) -> list[Path]:
        check_upstream(self.upstream_dir)
        prompts = load_prompts(self.upstream_dir)
        projects = [(r, self._validate_project(r)) for r in self.selected()]
        # Validate all selected inputs before writing anything; never silently skip tasks.
        for row, _ in projects:
            target = self.output_dir / slug(row)
            if target.exists() and not self.overwrite:
                raise FileExistsError(f"{target}; use --overwrite")
            if target.is_symlink():
                raise ValueError(f"Refusing symlink output: {target}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for row, project in projects:
            target = self.output_dir / slug(row)
            with tempfile.TemporaryDirectory(prefix=".vision2web-", dir=self.output_dir) as tmp:
                staged = Path(tmp) / "task"
                copy_tree(PACKAGE_DIR / "task-template", staged)
                (staged / "instruction.md").write_text(prompts[row["level"]])
                substitutions = {
                    "{{TASK_NAME}}": "zai-org/" + slug(row),
                    "{{LEVEL}}": row["level"],
                    "{{MEMORY_MB}}": str(self.memory_mb),
                    "{{RUNTIME_IMAGE}}": self.runtime_image,
                }
                for path in [
                    staged / "task.toml",
                    staged / "environment/Dockerfile",
                    staged / "tests/Dockerfile",
                ]:
                    content = path.read_text()
                    for key, value in substitutions.items():
                        content = content.replace(key, value)
                    content = content.replace("memory_mb = 8192", f"memory_mb = {self.memory_mb}")
                    path.write_text(content)
                shutil.copy2(PACKAGE_DIR / "metrics.py", staged / "tests/metrics.py")
                for name in ["prototypes", "resources", "prompt.txt", "prd.md"]:
                    source, dest = project / name, staged / "environment/input" / name
                    if source.is_dir():
                        copy_tree(source, dest)
                    elif source.is_file():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, dest)
                trusted = staged / "tests/data" / row["level"] / row["task_name"]
                trusted.mkdir(parents=True)
                shutil.copy2(project / "workflow.json", trusted / "workflow.json")
                copy_tree(project / "prototypes", trusted / "prototypes")
                copy_tree(self.upstream_dir / "vision2web", staged / "tests/upstream/vision2web")
                (staged / "tests/task.json").write_text(json.dumps(row, indent=2) + "\n")
                provenance = {
                    "task": row["id"],
                    "dataset_revision": DATASET_REVISION,
                    "upstream_revision": UPSTREAM_REVISION,
                    "source_files": {
                        str(p.relative_to(project)): sha256(p)
                        for p in sorted(project.rglob("*"))
                        if p.is_file()
                    },
                    "instruction_sha256": sha256(staged / "instruction.md"),
                    "reference_status": "not supplied",
                }
                if self.reference_dir:
                    reference = self.reference_dir / row["level"] / row["task_name"]
                    if (reference / "start.sh").is_file():
                        copy_tree(reference, staged / "solution/reference")
                        provenance["reference_status"] = "candidate supplied; not certified"
                        provenance["reference_files"] = {
                            str(p.relative_to(reference)): sha256(p)
                            for p in sorted(reference.rglob("*"))
                            if p.is_file()
                        }
                (staged / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
                if target.exists():
                    shutil.rmtree(target)
                staged.rename(target)
            written.append(target)
        return written
