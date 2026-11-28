from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

UPSTREAM_REPO = "https://github.com/VectorSpaceLab/AgentSWE.git"
UPSTREAM_COMMIT = "6643c7398b2d7dd58b32b6a3a829e564ab68a23f"
DEFAULT_PROFILE = "lite-v1.1"
TEMPLATE_DIR = Path(__file__).resolve().parent / "task-template"
RUNTIME_DIR = Path(__file__).resolve().parent / "runtime" / "agentswe_harbor"
UPSTREAM_ROOT = "/opt/agentswe-upstream"

# Creation tasks whose upstream per-case jobs use only the `main` service.
# Tasks with extra per-case services (GUI control plane, search front, LibreOffice,
# NET_ADMIN sandboxes) are documented exclusions.
SUPPORTED_TASKS = ("repository-bug-repair", "database-analytics", "scientific-pdf-translation")
TRUSTED_BROWSER_FILES = ("runtime-dependencies.manifest.json", "fonts.manifest.json", "assemble.py")
DEPS_ROOT = "/opt/agentswe-deps"

AUTHORS = ("Jiahao Wang", "Hongjin Qian", "Yuyang Hu", "Jiajun Zhang", "Zheng Liu")
EVALUATOR_APT_PACKAGES = "curl strace"


@dataclass(frozen=True)
class UpstreamTask:
    root: Path
    data: dict

    @property
    def task_id(self) -> str:
        return self.data["id"]

    @property
    def family(self) -> str:
        return self.data["family"]

    @property
    def env_name(self) -> str:
        return self.data["envs"][0]

    @property
    def env_prefix(self) -> str:
        return json.loads((self.root / "env" / "env.json").read_text())["mount"]


def ensure_upstream(upstream_dir: Path | None, cache_dir: Path) -> Path:
    """Return an AgentSWE checkout at UPSTREAM_COMMIT, cloning it once if needed."""
    if upstream_dir is not None:
        head = subprocess.run(["git", "-C", str(upstream_dir), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
        if head != UPSTREAM_COMMIT:
            raise SystemExit(f"{upstream_dir} is at {head}; the adapter is pinned to {UPSTREAM_COMMIT}")
        return upstream_dir
    checkout = cache_dir / f"AgentSWE-{UPSTREAM_COMMIT[:12]}"
    if not (checkout / ".git").is_dir():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--filter=blob:none", UPSTREAM_REPO, str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--quiet", UPSTREAM_COMMIT], check=True)
    return checkout


def copy_overlaid(task: UpstreamTask, sub: str, profile: str, destination: Path, ignore=None) -> None:
    """tasks/<family>/<id>/<sub> as the profile sees it (agentswe/profiles.py: overlay files replace base files)."""
    shutil.copytree(task.root / sub, destination, symlinks=True, ignore=ignore)
    overlay = task.root / "profiles" / profile / sub
    if overlay.is_dir():
        shutil.copytree(overlay, destination, symlinks=True, dirs_exist_ok=True)


class AgentSWEAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "full",
        profile: str = DEFAULT_PROFILE,
        upstream_dir: Path | None = None,
        cache_dir: Path | None = None,
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split
        self.profile = profile
        self.upstream_dir = upstream_dir
        self.cache_dir = cache_dir or Path.home() / ".cache" / "harbor-agentswe"

    def select_tasks(self, upstream: Path) -> list[UpstreamTask]:
        tasks = []
        for path in sorted((upstream / "tasks" / "creation").glob("*/task.json")):
            data = json.loads(path.read_text())
            if data["id"] not in SUPPORTED_TASKS:
                continue
            if self.split in ("lite", "parity") and not data.get("lite"):
                continue
            if self.task_ids and data["id"] not in self.task_ids and data["short"] not in self.task_ids:
                continue
            tasks.append(UpstreamTask(path.parent, data))
        return tasks[: self.limit] if self.limit else tasks

    def run(self) -> None:
        upstream = ensure_upstream(self.upstream_dir, self.cache_dir)
        tasks = self.select_tasks(upstream)
        if not tasks:
            raise SystemExit("no AgentSWE tasks selected")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for task in tasks:
            destination = self.output_dir / task.task_id
            if destination.exists():
                if not self.overwrite:
                    print(f"skip {task.task_id}: exists (use --overwrite)")
                    continue
                shutil.rmtree(destination)
            TaskWriter(upstream, task, self.profile, destination).write()
            print(f"wrote {destination}")


class TaskWriter:
    def __init__(self, upstream: Path, task: UpstreamTask, profile: str, destination: Path) -> None:
        self.upstream = upstream
        self.task = task
        self.profile = profile
        self.destination = destination

    def write(self) -> None:
        shutil.copytree(TEMPLATE_DIR, self.destination, ignore=shutil.ignore_patterns("__pycache__"))
        one_stop = self.import_one_stop()
        self.render("task.toml", self.toml_values(one_stop))
        # Upstream's Builder prompt, unchanged (one_stop.builder_instruction).
        self.render("instruction.md", {
            "BUILDER_INSTRUCTION": one_stop.builder_instruction(one_stop.MAX_DEV_ROUNDS),
        })
        self.write_builder_environment(one_stop)
        self.write_evaluator_context(self.destination / "environment" / "evaluator", hidden=False)
        self.write_evaluator_context(self.destination / "tests", hidden=True)
        for script in self.destination.rglob("*.sh"):
            script.chmod(0o755)

    def import_one_stop(self):
        adapter_dir = self.task.root / "adapter"
        shared = self.upstream / "runners" / "creation"
        for name in ("one_stop", "adapter"):
            sys.modules.pop(name, None)
        sys.path[:0] = [str(adapter_dir), str(shared)]
        try:
            return importlib.import_module("one_stop")
        finally:
            sys.path.remove(str(adapter_dir))
            sys.path.remove(str(shared))

    def render(self, relative: str, values: dict[str, str]) -> None:
        text = (TEMPLATE_DIR / relative).read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        leftover = re.findall(r"\{\{[A-Z_]+\}\}", text)
        if leftover:
            raise RuntimeError(f"unrendered placeholders in {relative}: {leftover}")
        (self.destination / relative).write_text(text, encoding="utf-8")

    def toml_values(self, one_stop) -> dict[str, str]:
        protocol = self.task.data["protocol"]
        hidden = len(protocol["heldout_cases"])
        # Per hidden case: Candidate job + verifier (660 s each) and Eval job (1800 s) + verifier (660 s).
        verifier_timeout = hidden * (660 + 660 + 1800 + 660)
        authors = ", ".join(f'{{ name = "{name}" }}' for name in AUTHORS)
        return {
            "TASK_ID": self.task.task_id,
            "SHORT": self.task.data["short"],
            "TITLE": self.task.data["title"],
            "FAMILY": self.task.family,
            "TIER": self.task.data.get("tier", ""),
            "LITE": "true" if self.task.data.get("lite") else "false",
            "AUTHORS": authors,
            "COMMIT": UPSTREAM_COMMIT,
            "PROFILE": self.profile,
            "ENV_PREFIX": self.task.env_prefix,
            "BUILDER_TIMEOUT": f"{float(protocol['builder_session_sec'])}",
            "VERIFIER_TIMEOUT": f"{float(verifier_timeout)}",
            "MAX_DEV_ROUNDS": str(one_stop.MAX_DEV_ROUNDS),
        }

    def env_builder_stage(self) -> str:
        """Upstream images/env-builder/Dockerfile, verbatim, as the first build stage."""
        text = (self.upstream / "images" / "env-builder" / "Dockerfile").read_text(encoding="utf-8")
        lines = text.splitlines()
        index = next(i for i, line in enumerate(lines) if line.startswith("FROM "))
        lines[index] += " AS envbuild"
        return "\n".join(lines) + "\n"

    def upstream_stage(self, image: str) -> str:
        """An upstream images/<image>/Dockerfile from its FROM line (Ubuntu digest, apt snapshot, pins)."""
        text = (self.upstream / "images" / image / "Dockerfile").read_text(encoding="utf-8")
        lines = text.splitlines()
        index = next(i for i, line in enumerate(lines) if line.startswith("FROM "))
        return "\n".join(lines[index:]) + "\n"

    def base_stage(self, packages: str) -> str:
        """Upstream task-base (the Candidate image) with the evaluator's extra packages."""
        body = self.upstream_stage("task-base")
        if "ARG INSTALL_EXTRA=\n" not in body:
            raise RuntimeError("unexpected upstream task-base Dockerfile")
        return body.replace("ARG INSTALL_EXTRA=\n", f'ARG INSTALL_EXTRA="{packages}"\n')

    def trusted_browser_stages(self, context: Path) -> tuple[str, str]:
        """Upstream images/trusted-browser/Dockerfile build stages (exported by `agentswe setup` upstream)."""
        if "trusted-browser" not in self.task.data.get("images", []):
            return "", ""
        source = self.upstream / "images" / "trusted-browser"
        lines = (source / "Dockerfile").read_text(encoding="utf-8").splitlines()
        first = next(i for i, line in enumerate(lines) if line.startswith("FROM "))
        last = max(i for i, line in enumerate(lines) if line.startswith("FROM scratch"))
        for name in TRUSTED_BROWSER_FILES:
            shutil.copy2(source / name, context / name)
        stages = "\n".join(lines[first:last]) + "\n"
        final = (
            f"COPY --from=assemble /out/trusted-browser-v1 {DEPS_ROOT}/trusted-browser-v1\n"
            f"COPY --from=assemble /out/runtime-dependencies-v1 {DEPS_ROOT}/runtime-dependencies-v1\n"
            f"ENV AGENTSWE_TRUSTED_BROWSER_BUNDLE={DEPS_ROOT}/trusted-browser-v1 \\\n"
            f"    AGENTSWE_PPTX_RENDERER_LIBS={DEPS_ROOT}/runtime-dependencies-v1/pptx/lib\n"
        )
        return stages, final

    def copy_env_spec(self, context: Path) -> None:
        shutil.copytree(self.task.root / "env", context / "env-spec")
        shutil.copy2(self.upstream / "images" / "env-builder" / "agentswe-build-env", context / "agentswe-build-env")

    def env_aliases(self, one_stop) -> list[str]:
        runtime_contract = sys.modules["runtime_contract"]
        aliases = runtime_contract.environment_aliases(Path("/nonexistent"), self.task.env_prefix)
        package = self.destination / "environment" / "builder-package"
        aliases.add(runtime_contract.documented_prefix(package, self.task.env_prefix))
        return sorted(alias for alias in aliases if alias != self.task.env_prefix)

    def write_builder_environment(self, one_stop) -> None:
        context = self.destination / "environment"
        public = context / "builder-package"
        with _Staging(self.task, self.profile) as (builder_package, benchmark):
            one_stop.prepare_public_builder_package(
                builder_package=builder_package, benchmark=benchmark, destination=public)
        self.copy_env_spec(context)
        shutil.copy2(self.task.root / "adapter" / "builder-template" / "environment" / "submit_dev_candidate",
                     context / "submit_dev_candidate.upstream")
        aliases = self.env_aliases(one_stop)
        links = " && ".join(
            f"mkdir -p {Path(alias).parent} && ln -s {self.task.env_prefix} {alias}" for alias in aliases
        ) or "true"
        documented = sys.modules["runtime_contract"].documented_prefix(public, self.task.env_prefix)
        self.render("environment/Dockerfile", {
            "ENV_BUILDER_STAGE": self.env_builder_stage(),
            # Upstream Builder trials run FROM builder-codex (Node 24.6.0 + Codex 0.144.1).
            "BASE_STAGE": self.upstream_stage("builder-codex"),
            "ENV_PREFIX": self.task.env_prefix,
            "ENV_ALIASES": links,
            "DOCUMENTED_PREFIX": documented,
        })
        self.render("environment/docker-compose.yaml", {
            "ENV_PREFIX": self.task.env_prefix,
            "EVALUATOR_CAPS": self.evaluator_caps(),
        })
        self.render("tests/docker-compose.yaml", {"EVALUATOR_CAPS": self.evaluator_caps()})

    def evaluator_caps(self) -> str:
        """Upstream's Candidate verifier traces the repair worker (strace) only where task.json asks for it."""
        if "strace" not in self.task.data.get("host", {}).get("host_tools", []):
            return ""
        return "    cap_add:\n      - SYS_PTRACE"

    def write_evaluator_context(self, context: Path, hidden: bool) -> None:
        upstream_task = context / "upstream" / "tasks" / self.task.family / self.task.task_id
        ignore = None if hidden else shutil.ignore_patterns("test_cases")
        copy_overlaid(self.task, "benchmark", self.profile, upstream_task / "benchmark", ignore=ignore)
        copy_overlaid(self.task, "builder_package", self.profile, upstream_task / "builder_package")
        shutil.copytree(self.task.root / "adapter", upstream_task / "adapter",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(self.task.root / "env", upstream_task / "env")
        shutil.copy2(self.task.root / "task.json", upstream_task / "task.json")
        shutil.copytree(self.upstream / "runners" / "creation", context / "upstream" / "runners" / "creation",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(self.upstream / "broker" / "agentswe_broker",
                        context / "upstream" / "broker" / "agentswe_broker",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(RUNTIME_DIR, context / "agentswe_harbor", ignore=shutil.ignore_patterns("__pycache__"))
        self.copy_env_spec(context)
        extra_stages, extra_final = self.trusted_browser_stages(context)
        self.render("tests/Dockerfile" if hidden else "environment/evaluator/Dockerfile", {
            "ENV_BUILDER_STAGE": self.env_builder_stage(),
            "EXTRA_STAGES": extra_stages,
            "EXTRA_FINAL": extra_final,
            "BASE_STAGE": self.base_stage(EVALUATOR_APT_PACKAGES),
            "ENV_PREFIX": self.task.env_prefix,
            "TASK_DIR": f"{UPSTREAM_ROOT}/tasks/{self.task.family}/{self.task.task_id}",
            "UPSTREAM_ROOT": UPSTREAM_ROOT,
        })


class _Staging:
    """Profile-overlaid builder_package and benchmark in a temporary directory."""

    def __init__(self, task: UpstreamTask, profile: str) -> None:
        self.task = task
        self.profile = profile

    def __enter__(self) -> tuple[Path, Path]:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="agentswe-stage-"))
        builder_package = self.tmp / "builder_package"
        benchmark = self.tmp / "benchmark"
        copy_overlaid(self.task, "builder_package", self.profile, builder_package)
        copy_overlaid(self.task, "benchmark", self.profile, benchmark,
                      ignore=shutil.ignore_patterns("test_cases"))
        return builder_package, benchmark

    def __exit__(self, *exc_info: object) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
