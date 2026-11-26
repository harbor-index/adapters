"""Generate Harbor tasks from the pinned DCP-Bench-Open release."""
from __future__ import annotations

import ast
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

PILOT = ("abbots_puzzle", "sudoku", "aircraft_landing", "fixed_charge")
UPSTREAM_COMMIT = "3f7e121301e0ea4565e20d8ee0660e799686c194"
HARBOR_COMMIT = "96c86af3bc6d7e92f21fc771d21273be4714c8e9"
TEMPLATE = Path(__file__).parent / "task-template"
PACKAGE = TEMPLATE.parent


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def public_input_notes(source):
    """The tagged declaration block contains only the public embedded input."""
    lines = source.splitlines()
    markers = [line.strip().lower() for line in lines]
    if "# data" not in markers:
        return "This problem has no per-instance input fields.\n"
    start = markers.index("# data") + 1
    stop = markers.index("# end of data", start)
    declarations = "\n".join(lines[start:stop]).strip()
    if not declarations:
        return "This problem has no per-instance input fields.\n"
    if not all(isinstance(node, ast.Assign) for node in ast.parse(declarations).body):
        raise ValueError("Expected only public input assignments in the v0.3.0 data block")
    return ("The release annotates the embedded example below. Read values from the "
            "supplied instance when building the model.\n\n```python\n" + declarations + "\n```\n")


def prepare_verifier(archive, archive_hash, image_id, docker_binary_image):
    """Build the shared runtime once; generated tasks contain only small files."""
    actual = subprocess.check_output(["docker", "image", "inspect", "dcp-eval/cpmpy_python:v1",
                                      "--format", "{{.Id}}"], text=True).strip()
    if actual != image_id:
        raise ValueError(f"Solver image on the Docker host differs from requested identity: {actual}")
    recipe = PACKAGE / "runtime.Dockerfile"
    identity = hashlib.sha256((archive_hash + image_id + docker_binary_image + sha256(recipe)).encode())
    tag = "dcp-bench-open/verifier:" + identity.hexdigest()[:24]
    with tempfile.TemporaryDirectory() as directory:
        context = Path(directory)
        try:
            os.link(archive, context / "solver-image.tar")
        except OSError:
            shutil.copyfile(archive, context / "solver-image.tar")
        shutil.copyfile(recipe, context / "Dockerfile")
        subprocess.run(["docker", "build", "--build-arg", f"DOCKER_BINARY_IMAGE={docker_binary_image}",
                        "--tag", tag, str(context)], check=True)
    return tag


def install_tasks(stage, output, names, overwrite):
    """Publish a fully generated batch, preserving existing tasks on failure."""
    output.mkdir(parents=True, exist_ok=True)
    previous = stage / ".previous"
    previous.mkdir()
    descriptor = os.open(output, os.O_RDONLY)
    installed = []
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        for name in names:
            target = output / name
            if target.exists() and not overwrite:
                raise FileExistsError(f"Refusing to overwrite {target}; pass --overwrite")
        for name in names:
            target = output / name
            if target.exists():
                target.rename(previous / name)
            (stage / name).rename(target)
            installed.append(name)
    except OSError:
        for name in installed:
            (output / name).rename(stage / name)
        for backup in previous.iterdir():
            backup.rename(output / backup.name)
        raise
    finally:
        os.close(descriptor)


def oracle_source(source, reference):
    tree, data, cut, output = reference.parse_source(source)
    imports = [n for n in tree.body[:cut] if isinstance(n, (ast.Import, ast.ImportFrom))]
    body = [n for n in tree.body[:cut] if n not in data and n not in imports]
    assigns = [ast.Assign(targets=[ast.Name(id=key, ctx=ast.Store())], value=ast.Subscript(
        value=ast.Name(id="instance", ctx=ast.Load()), slice=ast.Constant(key), ctx=ast.Load()))
        for key in reference.embedded_instance(source)]
    outputs = reference.OutputExpressions().visit(copy.deepcopy(output))
    ret = ast.Return(value=ast.Tuple(elts=[ast.Name(id="model", ctx=ast.Load()), outputs], ctx=ast.Load()))
    function = ast.FunctionDef(name="build", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="instance")],
        kwonlyargs=[], kw_defaults=[], defaults=[]), body=assigns + body + [ret], decorator_list=[])
    module = ast.Module(body=imports + [function], type_ignores=[])
    result = "# Oracle derived from pinned upstream reference AST.\n" + ast.unparse(ast.fix_missing_locations(module)) + "\n"
    compile(result, "oracle.py", "exec")
    return result


class DCPBenchOpenAdapter:
    def __init__(self, output_dir: Path, *, source_dir: Path, solver_image_archive: Path | None = None,
                 solver_image_id: str, limit: int | None = None, overwrite: bool = False,
                 task_ids: list[str] | None = None, control: str = "correct",
                 controls_dir: Path | None = None, docker_binary_image: str = "docker:28.2.2-dind",
                 verifier_image: str | None = None, solver_archive_sha256: str | None = None,
                 split: str | None = None):
        self.output_dir = output_dir.resolve()
        self.source_dir = source_dir.resolve()
        self.archive = solver_image_archive.resolve() if solver_image_archive else None
        self.archive_sha256 = solver_archive_sha256
        self.image_id = solver_image_id
        self.limit, self.overwrite, self.task_ids = limit, overwrite, task_ids
        self.control, self.controls_dir = control, controls_dir
        self.docker_binary_image = docker_binary_image
        self.verifier_image = verifier_image
        self.split = split or ("pilot" if controls_dir or control != "correct" else "full")

    def run(self):
        if self.archive is None and not (self.verifier_image and self.archive_sha256):
            raise ValueError("Supply a solver archive or a prepared verifier image and solver archive SHA-256")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_id):
            raise ValueError("--solver-image-id must be the built native image's SHA-256 identity")
        pins = json.loads((PACKAGE / "source-pins.json").read_text())
        released = sorted(Path(key).parent.name for key in pins if key.endswith(".cpmpy.py"))
        task_names = [name.replace("_", "-") for name in released]
        if len(released) != 164 or len(set(task_names)) != len(released):
            raise ValueError("Invalid inventory for release v0.3.0")
        eligible = list(PILOT) if self.split == "pilot" else released
        ids = self.task_ids or eligible
        ids = [p.replace("-", "_").removeprefix("dcp_bench_open/") for p in ids]
        if len(set(ids)) != len(ids) or not set(ids).issubset(eligible):
            raise ValueError(f"Expected unique task IDs from the {self.split} split")
        if self.limit is not None:
            if self.limit < 1:
                raise ValueError("--limit must be positive")
            ids = ids[:self.limit]
        for relative, expected in pins.items():
            path = self.source_dir / relative
            if not path.is_file() or sha256(path) != expected:
                raise ValueError(f"Source differs from pinned v0.3.0: {relative}")
        # Import only the verified, trusted upstream AST loader; no submitted code.
        sys.path.insert(0, str(self.source_dir))
        from evaluation import reference
        archive_hash = sha256(self.archive) if self.archive is not None else self.archive_sha256
        if not re.fullmatch(r"[0-9a-f]{64}", archive_hash) or (self.archive_sha256 and self.archive_sha256 != archive_hash):
            raise ValueError("Invalid or mismatched solver archive SHA-256")
        if self.verifier_image is None:
            self.verifier_image = prepare_verifier(self.archive, archive_hash, self.image_id, self.docker_binary_image)
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9./:@_-]*", self.verifier_image):
            raise ValueError("Invalid verifier image reference")
        self.output_dir.parent.mkdir(parents=True, exist_ok=True)
        names = [name.replace("_", "-") for name in ids]
        with tempfile.TemporaryDirectory(dir=self.output_dir.parent) as staging:
            stage = Path(staging)
            evaluator_files = [Path(path) for path in pins if path.startswith("evaluation/")]
            total = sum(self._generate(stage, problem, reference, archive_hash, evaluator_files) for problem in ids)
            if set(ids) == set(released) and total != 634:
                raise ValueError(f"Expected 634 distinct instances in v0.3.0, generated {total}")
            install_tasks(stage, self.output_dir, names, self.overwrite)
        print(f"Generated {len(names)} tasks covering {total} distinct instances in {self.output_dir}")

    def _generate(self, stage, problem, reference, archive_hash, evaluator_files):
        target = stage / problem.replace("_", "-")
        shutil.copytree(TEMPLATE, target)
        source_path = self.source_dir / "dataset" / problem / f"{problem}.cpmpy.py"
        source = source_path.read_text(encoding="utf-8-sig")
        tree, _, _, output = reference.parse_source(source)
        example = reference.embedded_instance(source)
        rows = json.loads(source_path.with_name(f"{problem}.json").read_text(encoding="utf-8-sig"))
        available = reference.available_instances(source, rows)
        instance_ids = [i for i, _ in available]
        def declared_data(row):
            return reference.canonical({name: row[name] for name in example})
        canonical_ids = {declared_data(row): key for key, row in available}
        if self.control == "suboptimal" and problem == "aircraft_landing":
            instance_ids = ["example"]
        model = oracle_source(source, reference)
        if self.control != "correct" and self.controls_dir is None:
            raise ValueError("--controls-dir is required for negative controls")
        if self.controls_dir is not None:
            suffix = "suboptimal_example" if problem == "aircraft_landing" and self.control == "suboptimal" else self.control
            model = (self.controls_dir / f"{problem}_{suffix}.py").read_text()
        (target / "solution" / "model.py").write_text(model)
        write_json(target / "environment" / "example.json", example)
        (target / "environment" / "INPUT.md").write_text(public_input_notes(source))
        skill = self.source_dir / "solvers" / "cpmpy_python" / "skills" / "cpmpy-python" / "SKILL.md"
        shutil.copy2(skill, target / "environment" / "CPMPY_SKILL.md")
        reviewed = PACKAGE / "public-briefs" / f"{problem}.md"
        brief = reviewed.read_text().strip() if reviewed.exists() else ast.get_docstring(tree, clean=False).strip()
        outputs = [key.value for key in output.keys]
        instruction = (TEMPLATE / "instruction.md").read_text()
        instruction = instruction.replace("{brief}", brief).replace("{outputs}", ", ".join(outputs))
        instruction = instruction.replace("{input_fields}", ", ".join(example) or "(none)")
        (target / "instruction.md").write_text(instruction)
        config = (TEMPLATE / "task.toml").read_text().replace("{task_id}", problem.replace("_", "-"))
        # The outer Harbor watchdog must allow the native per-instance budgets to run.
        config = config.replace("timeout_sec = 1500.0  # scaled by the generator for the selected instances",
                                f"timeout_sec = {max(1500, 120 + 400 * len(instance_ids))}.0")
        (target / "task.toml").write_text(config)
        native = target / "tests" / "native"
        (native / "evaluation").mkdir(parents=True)
        for relative in evaluator_files:
            shutil.copy2(self.source_dir / relative, native / relative)
        destination = native / "dataset" / problem
        destination.mkdir(parents=True)
        for extension in ("cpmpy.py", "json"):
            shutil.copy2(source_path.with_name(f"{problem}.{extension}"), destination)
        integration = native / "solvers" / "cpmpy_python"
        integration.mkdir(parents=True)
        shutil.copy2(self.source_dir / "solvers" / "cpmpy_python" / "metadata.yaml", integration)
        shutil.copy2(self.source_dir / "LICENSE", native / "LICENSE")
        shutil.copy2(self.source_dir / "SOURCES.md", native / "SOURCES.md")
        dockerfile = (TEMPLATE / "tests" / "Dockerfile").read_text().replace("{verifier_image}", self.verifier_image)
        (target / "tests" / "Dockerfile").write_text(dockerfile)
        manifest = {
            "problem": problem, "control": self.control, "instance_ids": instance_ids,
            "source_row_instance_ids": [canonical_ids[declared_data(row)] for row in rows],
            "reviewed_brief": reviewed.is_file(), "verifier_image": self.verifier_image,
            "upstream_commit": UPSTREAM_COMMIT, "harbor_commit": HARBOR_COMMIT,
            "source_sha256": sha256(source_path), "model_sha256": hashlib.sha256(model.encode()).hexdigest(),
            "solver_image_id": self.image_id,
            "solver_archive_sha256": archive_hash, "solution_limit": 1,
            "execution_timeout": 60, "reference_timeout": 60, "memory_mb": 2048, "cpus": 1,
        }
        write_json(target / "tests" / "case.json", manifest)
        if self.split == "pilot":
            write_json(target / "pilot-manifest.json", manifest)
        return len(instance_ids)
