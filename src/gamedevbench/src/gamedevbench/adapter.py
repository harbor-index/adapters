"""Generate independent Harbor tasks from the pinned waynchi benchmark."""
from __future__ import annotations

import hashlib
import concurrent.futures
import importlib.util
import json
import re
import shutil
import stat
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

REVISION = "54589a477d6c1a15255707a861db6b01126797aa"
TEMPLATE = Path(__file__).parent / "task-template"
PACKAGE_MANIFEST = Path(__file__).parent / "upstream_manifest.json"
ORACLE_OVERLAYS = Path(__file__).parent / "oracle-overlays"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract(archive_path: Path, target: Path, prefix: str) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        names, total = set(), 0
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts or not member.filename.startswith(prefix):
                raise ValueError("Unexpected ZIP path: " + member.filename)
            if member.filename in names:
                raise ValueError("Duplicate ZIP path")
            names.add(member.filename)
            if stat.S_IFMT(member.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("Special ZIP member")
            total += member.file_size
            if total > 8 * 1024**3:
                raise ValueError("Unpacked archive exceeds limit")
            destination = target.joinpath(*path.parts)
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(member))
    project = target / prefix
    if not (project / "project.godot").is_file():
        raise ValueError("Missing Godot project")
    return project


def copy_solver_view(source: Path, destination: Path) -> None:
    """Mirror GodotBenchmarkRunner._create_sandbox_environment's file policy."""
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        name = item.name.lower()
        if item.is_symlink():
            raise ValueError("Unexpected source symlink")
        if item.is_dir():
            if not name.startswith(".") and name != ".backup":
                copy_solver_view(item, destination / item.name)
        elif item.is_file():
            if name.startswith(("test", ".")) or name == "task_config.json" or name.endswith((".log", ".md")):
                continue
            shutil.copy2(item, destination / item.name)


def copy_reference_view(source: Path, destination: Path) -> None:
    """Keep GT data such as test_cube.tres; omit actual graders and caches."""
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        name = item.name.lower()
        if item.is_symlink():
            raise ValueError("Unexpected reference symlink")
        if item.is_dir():
            if not name.startswith(".") and name != ".backup":
                copy_reference_view(item, destination / item.name)
        elif item.is_file():
            if (name in {"test.gd", "test.gd.uid", "test.tscn", "task_config.json"}
                    or name.startswith(".") or name.endswith((".log", ".md"))):
                continue
            shutil.copy2(item, destination / item.name)


class GameDevBenchAdapter:
    def __init__(self, output_dir: Path, source_dir: Path, limit: int | None = None,
                 overwrite: bool = False, task_ids: list[str] | None = None,
                 runtime_image: str | None = None, use_mcp: bool = False,
                 use_runtime_video: bool = False, workers: int = 4,
                 oracle_profile: str = "formal-verifier-v1", **kwargs):
        self.output_dir, self.source_dir = Path(output_dir).resolve(), Path(source_dir).resolve()
        self.limit, self.overwrite, self.task_ids = limit, overwrite, task_ids
        self.runtime_image = runtime_image
        self.use_mcp, self.use_runtime_video = use_mcp, use_runtime_video
        self.workers = max(1, workers)
        if oracle_profile not in ("original", "formal-verifier-v1"):
            raise ValueError("Unknown oracle compatibility profile")
        self.oracle_profile = oracle_profile
        self.oracle_changes = {}
        if oracle_profile != "original":
            overlay = json.loads((ORACLE_OVERLAYS / oracle_profile / "manifest.json").read_text())
            if overlay["upstream_revision"] != REVISION:
                raise ValueError("Oracle overlay revision mismatch")
            self.oracle_changes = {row["task_id"]: row for row in overlay["tasks"]}
        manifest_path = PACKAGE_MANIFEST if PACKAGE_MANIFEST.is_file() else self.source_dir / "upstream_manifest.json"
        self.manifest = json.loads(manifest_path.read_text())
        if self.manifest["revision"] != REVISION:
            raise ValueError("Unexpected upstream revision")
        self.packages = {x["path"]: x for x in self.manifest["packages"]}
        for name, expected in self.manifest["runtime_files"].items():
            if digest(self.source_dir / name) != expected:
                raise ValueError("Upstream source checksum mismatch: " + name)
        spec = importlib.util.spec_from_file_location("official_gdb_prompts", self.source_dir / "gamedevbench/src/utils/prompts.py")
        self.prompts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.prompts)

    def run(self) -> dict:
        available = sorted(Path(p).stem for p in self.packages if p.startswith("tasks/"))
        selected = sorted(set(self.task_ids)) if self.task_ids else available
        if not selected or set(selected) - set(available):
            raise ValueError("Unknown or empty task selection")
        if self.limit is not None:
            if self.limit < 1:
                raise ValueError("--limit must be positive")
            selected = selected[:self.limit]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with concurrent.futures.ThreadPoolExecutor(self.workers) as pool:
            rows = list(pool.map(self.generate_task, selected))
        names = [tomllib.loads((self.output_dir / row["directory"] / "task.toml").read_text())["task"]["name"]
                 for row in rows]
        if len(set(names)) != len(rows) or any(not name.startswith("gamedevbench/") for name in names):
            raise ValueError("Task names must be stable, unique, and namespaced")
        description = (f"{len(rows)} GameDevBench Godot tasks from waynchi/gamedevbench@{REVISION}. "
                       "Original prompts and formal tests; declared oracle-only reference adaptations. "
                       "Adapted by xts from the work of Wayne Chi and colleagues. On the fixed 30-task parity sample, Codex 0.171.0 with GPT-6.1 Sol averaged 76.67% pass@1 on both runners across three runs on Docker.")
        (self.output_dir / "dataset.toml").write_text(
            'schema_version = "1.0"\n\n[dataset]\nname = "gamedevbench/gamedevbench"\n'
            + 'description = ' + json.dumps(description) + '\n'
            + 'keywords = ["godot", "game-development"]\n'
            + 'authors = [{name = "Wayne Chi"}, {name = "xts", email = "tianshixu@stu.pku.edu.cn"}]\n')
        report = {"upstream_revision": REVISION, "count": len(rows), "tasks": rows,
                  "verification_source": "tasks/", "oracle_source": "tasks_gt/",
                  "oracle_file_policy": "all-reference-data-except-graders-and-caches-v1",
                  "use_mcp": self.use_mcp, "use_runtime_video": self.use_runtime_video,
                  "oracle_profile": self.oracle_profile}
        package = Path(__file__).parent
        report["generator_files"] = {
            path.relative_to(package).as_posix(): digest(path)
            for path in sorted(package.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        }
        (self.output_dir / "generation_manifest.json").write_text(json.dumps(report, indent=2) + "\n")
        return report

    def _runtime(self, destination: Path) -> None:
        for relative in self.manifest["runtime_files"]:
            if not (relative.startswith(("gamedevbench/", ".agents/")) or relative in ("pyproject.toml", "uv.lock", "LICENSE", "opencode.json", "opencode.mcp.json")):
                continue
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.source_dir / relative, path)

    def _adapt_oracle(self, task: str, project: Path) -> dict | None:
        change = self.oracle_changes.get(task)
        if change is None:
            return None
        for row in change["files"]:
            relative = PurePosixPath(row["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Invalid oracle overlay path")
            original = project.joinpath(*relative.parts)
            replacement = ORACLE_OVERLAYS / self.oracle_profile / task / relative
            if digest(original) != row["original_sha256"] or digest(replacement) != row["replacement_sha256"]:
                raise ValueError("Oracle overlay checksum mismatch")
            shutil.copy2(replacement, original)
        return change

    def generate_task(self, task: str) -> dict:
        if not re.fullmatch(r"task_\d{4}", task):
            raise ValueError("Invalid upstream task ID")
        name = "gamedevbench-" + task.replace("_", "-")
        destination = self.output_dir / name
        if destination.exists():
            if not self.overwrite:
                raise FileExistsError(str(destination) + "; pass --overwrite to regenerate")
            old = json.loads((destination / "source_manifest.json").read_text())
            if old.get("task_id") != task or old.get("upstream_revision") != REVISION:
                raise ValueError("Existing output does not belong to this adapter")
            shutil.rmtree(destination)
        with tempfile.TemporaryDirectory(prefix="gdb-adapter-") as tmp:
            projects = {}
            for folder in ("tasks", "tasks_gt"):
                relative = folder + "/" + task + ".zip"
                archive = self.source_dir / relative
                if digest(archive) != self.packages[relative]["sha256"]:
                    raise ValueError("Task archive checksum mismatch: " + relative)
                projects[folder] = extract(archive, Path(tmp), folder + "/" + task + "/")
            original, gt = projects["tasks"], projects["tasks_gt"]
            config = json.loads((original / "task_config.json").read_text())
            if not config.get("instruction"):
                raise ValueError("Missing task instruction")
            shutil.copytree(TEMPLATE, destination)
            environment, solution, tests = [destination / x for x in ("environment", "solution", "tests")]
            public_project = environment / ('project-' + task)
            private_payload = tests / ('payload-' + task)
            private_payload.mkdir()
            copy_solver_view(original, public_project)
            copy_reference_view(gt, solution / "project")
            oracle_change = self._adapt_oracle(task, solution / "project")
            for project in (public_project, solution / "project"):
                (project / "task_config.json").write_text(json.dumps({"instruction": config["instruction"]}, indent=2) + "\n")
            verifier_hashes = {}
            for relative in ("scripts/test.gd", "scripts/test.gd.uid", "scenes/test.tscn"):
                if (original / relative).is_file():
                    target = private_payload / "official" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original / relative, target)
                    verifier_hashes[relative] = digest(target)
            shutil.copy2(original / "task_config.json", private_payload / "original_task_config.json")
            metadata = {"task_id": task, "upstream_revision": REVISION,
                        "starter_zip_sha256": self.packages[f"tasks/{task}.zip"]["sha256"],
                        "gt_zip_sha256": self.packages[f"tasks_gt/{task}.zip"]["sha256"],
                        "verifier_hashes": verifier_hashes,
                        "original_requires_display": bool(config.get("requires_display")),
                        "requires_display": bool(config.get("requires_display")) or task in {"task_0030", "task_0059"},
                        "compatibility_profile": "linux-runtime-compat-v2" if task == "task_0030" else "linux-runtime-compat-v1",
                        "agent_project_relative_path": public_project.relative_to(destination).as_posix(),
                        "private_payload_relative_path": private_payload.relative_to(destination).as_posix()}
            if oracle_change:
                metadata.update(oracle_profile=self.oracle_profile, oracle_change=oracle_change)
            for path in (private_payload / "binding.json", destination / "source_manifest.json"):
                path.write_text(json.dumps(metadata, indent=2) + "\n")
            instruction = self.prompts.create_task_prompt({"instruction": config["instruction"]},
                                                         use_mcp=self.use_mcp,
                                                         use_runtime_video=self.use_runtime_video)
            (destination / "instruction.md").write_text(instruction)
            config_text = (destination / "task.toml").read_text().replace("{task_name}", name)
            if not self.runtime_image:
                # Cold apt/toolchain downloads are outside solver/verifier budgets.
                config_text = config_text.replace("build_timeout_sec = 1200.0", "build_timeout_sec = 3600.0")
            (destination / "task.toml").write_text(config_text.replace("{task_id}", task).replace("{revision}", REVISION))
            runtime_dockerfile = (TEMPLATE / "environment/Dockerfile").read_text()
            for directory, final in ((environment, f"COPY {public_project.name}/ /workspace/\n"),
                                     (tests, f"COPY verify.py test.sh /tests/\nCOPY {private_payload.name}/ /tests/payload/\n")):
                if self.runtime_image:
                    base = "FROM " + self.runtime_image + "\n"
                else:
                    self._runtime(directory / "runtime")
                    base = runtime_dockerfile
                role = 'agent' if directory == environment else 'verifier'
                label = f'LABEL harbor.gamedevbench.task="{task}" harbor.gamedevbench.role="{role}"\n'
                (directory / "Dockerfile").write_text(base + "\n" + label + final + "WORKDIR /workspace\n")
            for script in (solution / "solve.sh", tests / "test.sh"):
                script.chmod(0o755)
        return {"task_id": task, "directory": name, "source": metadata,
                "instruction_sha256": digest(destination / "instruction.md")}
