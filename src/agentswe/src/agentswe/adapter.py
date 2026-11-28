from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

UPSTREAM_REPO = "https://github.com/VectorSpaceLab/AgentSWE.git"
UPSTREAM_COMMIT = "6643c7398b2d7dd58b32b6a3a829e564ab68a23f"
DEFAULT_PROFILE = "lite-v1.1"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "harbor-agentswe"
TEMPLATE_DIR = Path(__file__).resolve().parent / "task-template"
RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
UPSTREAM_ROOT = "/opt/agentswe-upstream"
DEPS_ROOT = "/opt/agentswe-deps"

# Creation tasks whose per-case jobs use only the `main` service and no host-fetched downloads. The README
# gives the reason for each excluded task.
SUPPORTED_TASKS = (
    "repository-bug-repair",
    "database-analytics",
    "scientific-pdf-translation",
    "evidence-grounded-document-qa",
)
AUTHORS = ("Jiahao Wang", "Hongjin Qian", "Yuyang Hu", "Jiajun Zhang", "Zheng Liu")
TRUSTED_BROWSER_FILES = ("runtime-dependencies.manifest.json", "fonts.manifest.json", "assemble.py")
EVALUATOR_PACKAGES = "curl strace"
# Unpacking the Builder environment and the environment digests before the held-out cases.
VERIFIER_SETUP_SEC = 1800

# Optional mirrors and proxy for image builds (upstream's image build args); unset, the public sources are used.
BUILD_ARGS = {
    "DOCKER_REGISTRY": "${AGENTSWE_DOCKER_REGISTRY:-docker.io}",
    "APT_MIRROR": "${AGENTSWE_APT_MIRROR:-}",
    "CONDA_FORGE_URL": "${AGENTSWE_CONDA_CHANNEL:-https://conda.anaconda.org/conda-forge}",
    "PIP_INDEX_URL": "${AGENTSWE_PIP_INDEX_URL:-https://pypi.org/simple}",
    "NODE_DIST_URL": "${AGENTSWE_NODE_DIST_URL:-https://nodejs.org/dist}",
    "NPM_REGISTRY": "${AGENTSWE_NPM_REGISTRY:-https://registry.npmjs.org}",
    "PLAYWRIGHT_DOWNLOAD_HOST": "${AGENTSWE_PLAYWRIGHT_DOWNLOAD_HOST:-}",
    "UBUNTU_ARCHIVE_URL": "${AGENTSWE_UBUNTU_MIRROR:-http://archive.ubuntu.com/ubuntu}",
    "CFT_BASE_URL": "${AGENTSWE_CFT_BASE_URL:-https://storage.googleapis.com/chrome-for-testing-public}",
    "http_proxy": "${AGENTSWE_BUILD_PROXY:-}",
    "https_proxy": "${AGENTSWE_BUILD_PROXY:-}",
}
# Runtime model and Result judge of the evaluation (profile lite-v1.1 defaults); None marks a required value.
EVALUATION_ENV = {
    "AGENTSWE_RUNTIME_BASE_URL": "https://api.deepseek.com/v1",
    "AGENTSWE_RUNTIME_API_KEY": None,
    "AGENTSWE_RUNTIME_MODEL": "deepseek-flash",
    "AGENTSWE_RUNTIME_EFFORT": "high",
    "AGENTSWE_RUNTIME_MODEL_ALIASES": "deepseek-flash",
    "AGENTSWE_JUDGE_BASE_URL": "https://api.deepseek.com/v1",
    "AGENTSWE_JUDGE_API_KEY": None,
    "AGENTSWE_JUDGE_MODEL": "deepseek-flash",
    "AGENTSWE_JUDGE_EFFORT": "max",
}
# The Builder's model, reached only through the builder role broker in the evaluator.
BUILDER_ENV = {
    "AGENTSWE_BUILDER_BASE_URL": None,
    "AGENTSWE_BUILDER_API_KEY": None,
    "AGENTSWE_BUILDER_MODEL": None,
    "AGENTSWE_BUILDER_EFFORT": "xhigh",
}


@dataclass(frozen=True)
class UpstreamTask:
    root: Path
    data: dict
    env: dict

    @property
    def task_id(self) -> str:
        return self.data["id"]

    @property
    def family(self) -> str:
        return self.data["family"]

    @property
    def env_prefix(self) -> str:
        """Where upstream's env.Dockerfile builds the environment, and so where its scripts point."""
        return self.env["mount"]


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


def from_first(lines: list[str], prefix: str) -> int:
    return next(index for index, line in enumerate(lines) if line.startswith(prefix))


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
        cache_dir: Path = DEFAULT_CACHE_DIR,
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split
        self.profile = profile
        self.upstream_dir = upstream_dir
        self.cache_dir = cache_dir

    def select_tasks(self, upstream: Path) -> list[UpstreamTask]:
        tasks = []
        for path in sorted((upstream / "tasks" / "creation").glob("*/task.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["id"] not in SUPPORTED_TASKS:
                continue
            if self.split in ("lite", "parity") and not data.get("lite"):
                continue
            if self.task_ids and data["id"] not in self.task_ids and data["short"] not in self.task_ids:
                continue
            env = json.loads((path.parent / "env" / "env.json").read_text(encoding="utf-8"))
            tasks.append(UpstreamTask(path.parent, data, env))
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
        one_stop, runtime_contract = self.import_upstream()
        env_stages = self.env_build_stages()
        evaluator = self.destination / "environment" / "evaluator"
        self.write_evaluator_context(evaluator, env_stages, hidden=False)
        self.write_evaluator_context(self.destination / "tests", env_stages, hidden=True)
        staged = evaluator / "upstream" / "tasks" / self.task.family / self.task.task_id
        self.write_builder_context(one_stop, runtime_contract, staged, env_stages)
        self.render("environment/docker-compose.yaml", {
            "BUILD_ARGS": self.yaml_mapping(BUILD_ARGS, 2),
            "ENV_PREFIX": self.task.env_prefix,
            "EVALUATOR_CAPS": self.evaluator_caps(),
            "EVALUATOR_ENV": self.yaml_mapping(EVALUATION_ENV | BUILDER_ENV, 6),
        })
        self.render("tests/docker-compose.yaml", {
            "BUILD_ARGS": self.yaml_mapping(BUILD_ARGS, 8),
            "EVALUATOR_CAPS": self.evaluator_caps(),
        })
        self.render("task.toml", self.toml_values(one_stop))
        # Upstream's Builder prompt, unchanged.
        self.render("instruction.md", {
            "BUILDER_INSTRUCTION": one_stop.builder_instruction(int(self.task.data["protocol"]["max_dev_rounds"])),
        })
        for script in self.destination.rglob("*.sh"):
            script.chmod(0o755)

    def import_upstream(self):
        """The task's one_stop.py (with its adapter.py) and runners/creation/runtime_contract.py."""
        for name in ("one_stop", "adapter", "runtime_contract"):
            sys.modules.pop(name, None)
        paths = [str(self.task.root / "adapter"), str(self.upstream / "runners" / "creation")]
        sys.path[:0] = paths
        try:
            return importlib.import_module("one_stop"), importlib.import_module("runtime_contract")
        finally:
            for path in paths:
                sys.path.remove(path)

    def render(self, relative: str, values: dict[str, str]) -> None:
        text = (TEMPLATE_DIR / relative).read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        leftover = re.findall(r"\{\{[A-Z_]+\}\}", text)
        if leftover:
            raise RuntimeError(f"unrendered placeholders in {relative}: {leftover}")
        (self.destination / relative).write_text(text, encoding="utf-8")

    @staticmethod
    def yaml_mapping(values: dict[str, str | None], indent: int) -> str:
        """Compose entries that read each value from the environment of `harbor run`, with its default."""
        return "\n".join(
            " " * indent + f"{key}: " + (value if value is not None and value.startswith("${")
                                         else "${" + f"{key}:-{value or ''}" + "}")
            for key, value in values.items()
        )

    def toml_values(self, one_stop) -> dict[str, str]:
        protocol = self.task.data["protocol"]
        adapter = one_stop.adapter
        case = protocol["heldout_cases"][0]
        candidate = tomllib.loads(adapter.candidate_task_toml(case))
        evaluation = tomllib.loads(adapter.eval_task_toml(case))
        # Every held-out case, one at a time, at upstream's per-job limits (including the Eval retries of tasks
        # that have them), after unpacking the Builder environment.
        candidate_sec = candidate["agent"]["timeout_sec"] + candidate["verifier"]["timeout_sec"]
        eval_sec = (evaluation["agent"]["timeout_sec"] + evaluation["verifier"]["timeout_sec"]) * (
            1 + getattr(adapter, "DEFAULT_EVAL_INFRASTRUCTURE_RETRIES", 0))
        verifier_timeout = VERIFIER_SETUP_SEC + len(protocol["heldout_cases"]) * (candidate_sec + eval_sec)
        return {
            "TASK_ID": self.task.task_id,
            "SHORT": self.task.data["short"],
            "TITLE": self.task.data["title"],
            "FAMILY": self.task.family,
            "TIER": self.task.data.get("tier", ""),
            "LITE": "true" if self.task.data.get("lite") else "false",
            "AUTHORS": ", ".join(f'{{ name = "{name}" }}' for name in AUTHORS),
            "COMMIT": UPSTREAM_COMMIT,
            "PROFILE": self.profile,
            "ENV_PREFIX_RELATIVE": self.task.env_prefix.lstrip("/"),
            "BUILDER_TIMEOUT": f"{float(protocol['builder_session_sec'])}",
            "VERIFIER_TIMEOUT": f"{float(verifier_timeout)}",
            "MAX_DEV_ROUNDS": str(protocol["max_dev_rounds"]),
            "VERIFIER_ENV": "\n".join(
                f'{key} = "${{{key}}}"' if value is None else f'{key} = "${{{key}:-{value}}}"'
                for key, value in EVALUATION_ENV.items()
            ),
        }

    def env_build_stages(self) -> str:
        """Upstream's environment build: images/env-builder/Dockerfile plus the build stage of
        images/env-builder/env.Dockerfile, which runs agentswe-build-env on the task's env spec."""
        if self.task.env.get("downloads"):
            raise RuntimeError(f"{self.task.task_id}: env.json downloads (host-fetched assets) are not supported")
        env_name = self.task.env["name"]
        if self.task.env_prefix != f"/opt/agentswe/benchmark/envs/{env_name}":
            raise RuntimeError(f"{self.task.task_id}: env mount {self.task.env_prefix} is not the env.Dockerfile path")
        images = self.upstream / "images" / "env-builder"
        builder = (images / "Dockerfile").read_text(encoding="utf-8").splitlines()
        builder[from_first(builder, "FROM ")] += " AS envbuilder"
        env_lines = (images / "env.Dockerfile").read_text(encoding="utf-8").splitlines()
        stage = "\n".join(env_lines[from_first(env_lines, "FROM "):from_first(env_lines, "FROM scratch")])
        for old, new in (
            ("FROM ${ENV_BUILDER} AS build", "FROM envbuilder AS envbuild"),
            ("ARG ENV_NAME\n", f"ARG ENV_NAME={env_name}\n"),
            ("COPY --from=spec . /spec", "COPY env-spec /spec"),
        ):
            if old not in stage:
                raise RuntimeError(f"unexpected upstream env.Dockerfile: {old!r} not found")
            stage = stage.replace(old, new, 1)
        return "\n".join(builder) + "\n\n" + stage + "\n"

    def upstream_stage(self, image: str) -> str:
        """An upstream images/<image>/Dockerfile from its FROM line (Ubuntu digest, apt snapshot, pins)."""
        lines = (self.upstream / "images" / image / "Dockerfile").read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[from_first(lines, "FROM "):]) + "\n"

    def base_stage(self) -> str:
        """Upstream task-base (the image of upstream's per-case jobs) with the evaluator's extra packages."""
        body = self.upstream_stage("task-base")
        if "ARG INSTALL_EXTRA=\n" not in body:
            raise RuntimeError("unexpected upstream task-base Dockerfile")
        return body.replace("ARG INSTALL_EXTRA=\n", f'ARG INSTALL_EXTRA="{EVALUATOR_PACKAGES}"\n')

    def trusted_browser(self, context: Path) -> tuple[str, str]:
        """Upstream images/trusted-browser/Dockerfile build stages, as `agentswe setup` exports them."""
        if "trusted-browser" not in self.task.data.get("images", []):
            return "", ""
        source = self.upstream / "images" / "trusted-browser"
        for name in TRUSTED_BROWSER_FILES:
            shutil.copy2(source / name, context / name)
        lines = (source / "Dockerfile").read_text(encoding="utf-8").splitlines()
        last = max(index for index, line in enumerate(lines) if line.startswith("FROM scratch"))
        # These stages pin exact package versions of their apt snapshot, which a live mirror does not serve; keep
        # them on the snapshot even when APT_MIRROR is set for the other stages.
        stages = ("\n".join(lines[from_first(lines, "FROM "):last]) + "\n").replace("APT_MIRROR", "TRUSTED_BROWSER_APT_MIRROR")
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

    def has_fontconfig(self) -> bool:
        return "/fontconfig-" in (self.task.root / "env" / "conda-explicit.txt").read_text(encoding="utf-8")

    def builder_service(self, one_stop, runtime_contract, public: Path) -> dict:
        """Upstream's Builder service (one_stop.builder_compose, then runtime_contract.prepare_compose), computed
        against a probe that has what prepare_compose reads from the environment: bin/pip's shebang, lib/,
        browsers/ and etc/fonts/fonts.conf. The Builder image asserts the real environment matches it."""
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "environment"
            (probe / "bin").mkdir(parents=True)
            (probe / "lib").mkdir()
            (probe / "bin" / "pip").write_text(f"#!{self.task.env_prefix}/bin/python\n", encoding="utf-8")
            if self.has_fontconfig():
                (probe / "etc" / "fonts").mkdir(parents=True)
                (probe / "etc" / "fonts" / "fonts.conf").write_text("", encoding="utf-8")
            compose = one_stop.builder_compose(public_package=public, env_prefix=probe,
                                               workspace_submission=Path(tmp) / "submission",
                                               controller_socket=Path(tmp) / "controller.sock", controller_token="")
            service = runtime_contract.prepare_compose(Path(tmp) / "docker-compose.yaml", compose)["services"]["main"]
            service["aliases"] = sorted(volume["target"] for volume in service["volumes"]
                                        if Path(volume["source"]) == probe.resolve())
        return service

    def write_builder_context(self, one_stop, runtime_contract, staged: Path, env_stages: str) -> None:
        context = self.destination / "environment"
        public = context / "builder-package"
        one_stop.prepare_public_builder_package(
            builder_package=staged / "builder_package", benchmark=staged / "benchmark", destination=public)
        self.copy_env_spec(context)
        shutil.copy2(self.task.root / "adapter" / "builder-template" / "environment" / "submit_dev_candidate",
                     context / "submit_dev_candidate.upstream")
        service = self.builder_service(one_stop, runtime_contract, public)
        prefix = self.task.env_prefix
        checks = [f"test -d {prefix}/lib", f"test ! -e {prefix}/browsers",
                  f"test {'-f' if self.has_fontconfig() else '! -e'} {prefix}/etc/fonts/fonts.conf"]
        links = [f"mkdir -p {Path(alias).parent} && ln -s {prefix} {alias}"
                 for alias in service["aliases"] if alias != prefix]
        # The controller token comes from the evaluator at run time (environment/submit_dev_candidate).
        environment = {key: value for key, value in service["environment"].items()
                       if key != "AGENTSWE_DEV_CONTROLLER_TOKEN" and not key.lower().endswith("_proxy")}
        self.render("environment/Dockerfile", {
            "ENV_BUILD_STAGES": env_stages,
            # Upstream Builder trials run FROM builder-codex (Node 24.6.0 + Codex 0.144.1).
            "BASE_STAGE": self.upstream_stage("builder-codex"),
            "ENV_PREFIX": prefix,
            "ENV_LAYOUT": " \\\n && ".join(checks + links),
            "BUILDER_ENV": " \\\n    ".join(f"{key}={value}" for key, value in sorted(environment.items())),
        })

    def evaluator_caps(self) -> str:
        """Upstream's Candidate verifier traces the repair worker (strace) only where task.json asks for it."""
        if "strace" not in self.task.data.get("host", {}).get("host_tools", []):
            return ""
        return "\n    cap_add:\n      - SYS_PTRACE"

    def write_evaluator_context(self, context: Path, env_stages: str, hidden: bool) -> None:
        upstream_task = context / "upstream" / "tasks" / self.task.family / self.task.task_id
        ignore = None if hidden else shutil.ignore_patterns("test_cases")
        copy_overlaid(self.task, "benchmark", self.profile, upstream_task / "benchmark", ignore=ignore)
        copy_overlaid(self.task, "builder_package", self.profile, upstream_task / "builder_package")
        shutil.copytree(self.task.root / "adapter", upstream_task / "adapter", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(self.task.root / "env", upstream_task / "env")
        shutil.copy2(self.task.root / "task.json", upstream_task / "task.json")
        for part in (("runners", "creation"), ("broker", "agentswe_broker")):
            shutil.copytree(self.upstream.joinpath(*part), context.joinpath("upstream", *part),
                            ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(RUNTIME_DIR / "agentswe_harbor", context / "agentswe_harbor", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(RUNTIME_DIR / "bin", context / "bin")
        self.copy_env_spec(context)
        trusted_stages, trusted_final = self.trusted_browser(context)
        self.render("tests/Dockerfile" if hidden else "environment/evaluator/Dockerfile", {
            "ENV_BUILD_STAGES": env_stages,
            "EXTRA_STAGES": trusted_stages,
            "EXTRA_FINAL": trusted_final,
            "BASE_STAGE": self.base_stage(),
            "ENV_PREFIX": self.task.env_prefix,
            "TASK_DIR": f"{UPSTREAM_ROOT}/tasks/{self.task.family}/{self.task.task_id}",
            "UPSTREAM_ROOT": UPSTREAM_ROOT,
        })
