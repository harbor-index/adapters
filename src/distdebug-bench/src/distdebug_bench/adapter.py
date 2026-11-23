"""Generate Harbor tasks from DistDebug-Bench (https://github.com/OrderLab/distdebug-bench).

Each bug becomes one task:

- ``instruction.md``: the exact prompt the upstream Codex CLI / OpenCode harnesses
  give (``distdebug_bench.harnesses.cli.cli_prompt``): the Mini-SWE-Agent
  instructions with the issue title, description and attachment list.
- ``environment/``: the system's source at the pinned pre-fix commit and only its
  ancestors (``build_snapshot.sh``), ``.validate_diagnosis.py`` and the issue's
  attachments in ``.artifacts/``, as staged by ``distdebug_bench.agent``.
- ``tests/``: a separate verifier image with the verbatim upstream scorer
  package and this bug's labels; it scores ``/workspace/diagnosis.json``.
- ``solution/solve.sh``: writes the labeled root-cause blocks as the diagnosis.

The adapter reads the benchmark from a checkout of the upstream repository
(cloned at ``BENCHMARK_COMMIT`` unless ``--source-dir`` is given) and copies the
scorer files byte for byte, checking them against ``UPSTREAM_FILE_SHA256``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

BENCHMARK_REPO = "https://github.com/OrderLab/distdebug-bench.git"
# v1.1 labels (2026-11-07).
BENCHMARK_COMMIT = "c3e929b9ee0082345337358e352ae37fc472df4c"
BENCHMARK_VERSION = "v1.1"

# Upstream files copied verbatim into each task, with their hashes at BENCHMARK_COMMIT.
SCORER_FILES = ("__init__.py", "benchmark.py", "evaluate.py", "models.py", "repos.py", "score.py", "validate_diagnosis.py")
UPSTREAM_FILE_SHA256 = {
    "distdebug_bench/__init__.py": "c9dc7028344f170b1182efcac33fe460a692a17555a40731f31eb4948a8ff347",
    "distdebug_bench/benchmark.py": "b36764b51dc7f5dbfa08e52c31e67426f1942ff9aad88b3b79cbffb0e52ac8e6",
    "distdebug_bench/evaluate.py": "a90fb99f4f50836cfec9105c476131a2993ddbb62a44992b0d8092410c4b4296",
    "distdebug_bench/models.py": "aa3519db24b61a48bafdca43d5f2c8a9ed6ef08430dd431af62fe2362ab649cf",
    "distdebug_bench/repos.py": "a23185717147f59a8d3b000d655105eb1754636fbeec3f72a0c14c516b19b3a6",
    "distdebug_bench/score.py": "2739054809167296630f11bdf0de2e02708c4213246aa2e5a2b793d7dbc02807",
    "distdebug_bench/validate_diagnosis.py": "28dd3ec5c9004c193975ebdaa5ec3dcdbba4fa34e6ba58f73134fbade7cb3f67",
    "distdebug_bench/mini_swe_agent_prompt.yaml": "50bb61d7180340b27318fc7af4a12c954e94432bd941aac903ae0e25d0136e65",
}

# Same constants as distdebug_bench.agent / distdebug_bench.harnesses.cli.
ARTIFACTS_DIR = ".artifacts"
MINI_FINISH = "6. Only after validation succeeds, finish by issuing:\n   `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`"
CLI_FINISH = "6. Only after validation succeeds, stop and reply with a one-sentence summary."

# LLM API hosts reachable during the agent phase by default. Upstream runs every
# command offline; this keeps the issue tracker, the fix and the benchmark's own
# repository out of reach while letting an in-container agent call its model.
DEFAULT_ALLOWED_HOSTS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "openrouter.ai",
    "api.stepfun.com",
    "api.deepseek.com",
)

# Systems whose full-history snapshot is large (~1.4-1.6 GB of .git + worktree;
# every other Lite snapshot is <= 0.45 GB).
LARGE_SYSTEMS = {"elasticsearch", "kubernetes"}

TEMPLATE_DIR = Path(__file__).parent / "task-template"
SNAPSHOTS_FILE = Path(__file__).parent / "snapshots.json"
PARITY_FILE = Path(__file__).parent / "parity_subset.txt"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _render(text: str, values: dict[str, str]) -> str:
    """Fill ``{key}`` placeholders for known keys only (other braces stay as-is)."""
    return re.sub(r"\{([a-z_]+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def _toml_str(value: str) -> str:
    """Escape a value for a double-quoted TOML string."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


@dataclass
class Bug:
    bug_id: str
    system: str
    number: str
    report: dict
    blocks: list[dict]
    bug_dir: Path
    artifact_files: list[Path]


class DistDebugBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "lite",
        source_dir: Path | None = None,
        cache_dir: Path | None = None,
        agent_network: str = "allowlist",
        allowed_hosts: list[str] | None = None,
        agent_timeout_sec: float = 3600.0,
        allow_unpinned_source: bool = False,
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split
        self.source_dir = Path(source_dir) if source_dir else None
        self.cache_dir = Path(cache_dir) if cache_dir else Path.home() / ".cache" / "harbor-distdebug-bench"
        if agent_network not in ("allowlist", "public", "no-network"):
            raise ValueError(f"agent_network must be allowlist, public or no-network, not {agent_network!r}")
        self.agent_network = agent_network
        self.allowed_hosts = list(allowed_hosts) if allowed_hosts else list(DEFAULT_ALLOWED_HOSTS)
        self.agent_timeout_sec = agent_timeout_sec
        self.allow_unpinned_source = allow_unpinned_source

    # ------------------------------------------------------------------ source

    def _source(self) -> Path:
        if self.source_dir is not None:
            root = self.source_dir.resolve()
        else:
            root = self.cache_dir / f"distdebug-bench-{BENCHMARK_COMMIT[:12]}"
            if not (root / "benchmark" / "systems.json").exists():
                shutil.rmtree(root, ignore_errors=True)
                root.parent.mkdir(parents=True, exist_ok=True)
                logger.info("Cloning %s at %s", BENCHMARK_REPO, BENCHMARK_COMMIT)
                subprocess.run(["git", "init", "-q", str(root)], check=True)
                subprocess.run(["git", "-C", str(root), "fetch", "-q", "--depth", "1", BENCHMARK_REPO, BENCHMARK_COMMIT], check=True)
                subprocess.run(["git", "-C", str(root), "checkout", "-q", "--detach", "FETCH_HEAD"], check=True)
        if not (root / "benchmark" / "systems.json").exists():
            raise FileNotFoundError(f"{root} is not a DistDebug-Bench checkout (no benchmark/systems.json)")
        mismatched = [rel for rel, digest in UPSTREAM_FILE_SHA256.items() if _sha256(root / rel) != digest]
        if mismatched:
            msg = f"Upstream files differ from the pinned commit {BENCHMARK_COMMIT[:12]}: {mismatched}"
            if not self.allow_unpinned_source:
                raise RuntimeError(msg + " (pass --allow-unpinned-source to generate anyway)")
            logger.warning(msg)
        return root

    def _bug_ids(self, root: Path) -> list[str]:
        if self.split == "lite":
            ids = (root / "benchmark" / "lite.txt").read_text().split()
        elif self.split == "full":
            ids = sorted(f"{p.parent.name}-{p.name}" for p in (root / "benchmark" / "bugs").glob("*/*") if p.is_dir())
        elif self.split == "parity":
            ids = PARITY_FILE.read_text().split()
        else:
            raise ValueError(f"Unknown split {self.split!r}; expected lite, full or parity")
        if self.task_ids:
            wanted = set(self.task_ids)
            unknown = wanted - set(ids)
            if unknown:
                raise ValueError(f"Task ids not in split {self.split!r}: {sorted(unknown)}")
            ids = [i for i in ids if i in wanted]
        if self.limit is not None:
            ids = ids[: self.limit]
        return ids

    @staticmethod
    def _load_bug(root: Path, bug_id: str) -> Bug:
        system, _, number = bug_id.partition("-")
        bug_dir = root / "benchmark" / "bugs" / system / number
        report = json.loads((bug_dir / "report.json").read_text())
        blocks = json.loads((bug_dir / "blocks.json").read_text())
        artifacts: list[Path] = []
        if report.get("artifact_dir") and (bug_dir / report["artifact_dir"]).is_dir():
            artifacts = sorted(p for p in (bug_dir / report["artifact_dir"]).iterdir() if p.is_file())
        return Bug(bug_id, system, number, report, blocks, bug_dir, artifacts)

    # ------------------------------------------------------------- task parts

    @staticmethod
    def build_task_text(bug: Bug) -> str:
        """``distdebug_bench.agent.build_task``: title, description and attachment list."""
        task = f"Title: {bug.report.get('title', '')}\n\n{bug.report.get('description', '').strip()}"
        if bug.artifact_files:
            listing = "\n".join(f"- {p.name}" for p in bug.artifact_files)
            task += (
                f"\n\nAdditional artifacts are available in `./{ARTIFACTS_DIR}/`.\n"
                "Inspect the relevant artifact files directly instead of assuming they are "
                "already summarized in the issue report.\n"
                f"Available artifact files:\n{listing}\n"
            )
        return task

    @staticmethod
    def cli_prompt(root: Path, task: str) -> str:
        """``distdebug_bench.harnesses.cli.cli_prompt``: the prompt upstream gives CLI agents."""
        templates = yaml.safe_load((root / "distdebug_bench" / "mini_swe_agent_prompt.yaml").read_text())["agent"]
        body = templates["instance_template"].split("<system_information>")[0]
        if MINI_FINISH not in body:
            raise RuntimeError("mini_swe_agent_prompt.yaml changed; update the CLI finishing step")
        body = body.replace(MINI_FINISH, CLI_FINISH).replace("{{task}}", task)
        return f"{templates['system_template'].strip()}\n\n{body.strip()}\n"

    @staticmethod
    def oracle_diagnosis(bug: Bug) -> dict:
        return {
            "buggy_blocks": [
                {
                    "file": b["file"],
                    "function": b.get("function", "") or "",
                    "start_line": int(b["start_line"]),
                    "end_line": int(b["end_line"]),
                    "bug_contribution": "Human-labeled root-cause block (oracle).",
                    "patch": "",
                }
                for b in bug.blocks
            ],
            "bug_cause_description": "Oracle diagnosis built from the benchmark's labeled root-cause blocks.",
        }

    def _agent_network_toml(self) -> str:
        if self.agent_network == "allowlist":
            hosts = ", ".join(f'"{h}"' for h in self.allowed_hosts)
            return f'network_mode = "allowlist"\nallowed_hosts = [{hosts}]'
        return f'network_mode = "{self.agent_network}"'

    # ------------------------------------------------------------------ write

    def generate_task(self, root: Path, systems: dict, snapshots: dict, bug: Bug) -> Path:
        task_dir = self.output_dir / bug.bug_id
        if task_dir.exists():
            if not self.overwrite:
                logger.info("Skipping %s (exists; use --overwrite)", bug.bug_id)
                return task_dir
            shutil.rmtree(task_dir)
        snap = snapshots[bug.bug_id]
        if snap["upstream_ref"] != bug.report.get("branch"):
            raise RuntimeError(f"{bug.bug_id}: report.json branch {bug.report.get('branch')!r} != pinned {snap['upstream_ref']!r}")
        system = systems[bug.system]
        instruction = self.cli_prompt(root, self.build_task_text(bug))

        values = {
            "task_id": bug.bug_id,
            "bug_id": bug.bug_id,
            "system": bug.system,
            "system_title": _toml_str(bug.system.capitalize() if bug.system not in ("hdfs",) else "HDFS"),
            "language": _toml_str(system.get("language", "")),
            "benchmark_version": BENCHMARK_VERSION,
            "upstream_ref": _toml_str(snap["upstream_ref"]),
            "commit": snap["commit"],
            "repo_url": snap["repo_url"],
            "difficulty": _toml_str((bug.report.get("bug_complexity") or "unknown").lower()),
            "root_cause_type": _toml_str(bug.report.get("root_cause_type") or ""),
            "num_blocks": str(len(bug.blocks)),
            "agent_timeout_sec": f"{float(self.agent_timeout_sec):.1f}",
            "agent_network": self._agent_network_toml(),
            "build_timeout_sec": "3600.0" if bug.system in LARGE_SYSTEMS else "1800.0",
            "storage_mb": "16384" if bug.system in LARGE_SYSTEMS else "8192",
            "copy_artifacts": f"COPY artifacts/ /workspace/{ARTIFACTS_DIR}/\n" if bug.artifact_files else "",
            "instruction": instruction.rstrip("\n"),
            "oracle_diagnosis": json.dumps(self.oracle_diagnosis(bug), indent=2),
        }

        shutil.copytree(TEMPLATE_DIR, task_dir)
        for rel in ("task.toml", "instruction.md", "environment/Dockerfile", "solution/solve.sh"):
            path = task_dir / rel
            path.write_text(_render(path.read_text(), values), newline="\n")
        for rel in ("solution/solve.sh", "tests/test.sh", "environment/build_snapshot.sh"):
            (task_dir / rel).chmod(0o755)

        # Agent environment: validator and attachments, as staged upstream.
        env_dir = task_dir / "environment"
        shutil.copyfile(root / "distdebug_bench" / "validate_diagnosis.py", env_dir / "validate_diagnosis.py")
        if bug.artifact_files:
            (env_dir / "artifacts").mkdir()
            for path in bug.artifact_files:
                shutil.copyfile(path, env_dir / "artifacts" / path.name)

        # Verifier: verbatim scorer package + this bug's labels only.
        tests_dir = task_dir / "tests"
        pkg = tests_dir / "distdebug_bench"
        pkg.mkdir()
        for name in SCORER_FILES:
            shutil.copyfile(root / "distdebug_bench" / name, pkg / name)
        bench = tests_dir / "benchmark"
        (bench / "bugs" / bug.system / bug.number).mkdir(parents=True)
        shutil.copyfile(root / "benchmark" / "systems.json", bench / "systems.json")
        for name in ("report.json", "blocks.json"):
            shutil.copyfile(bug.bug_dir / name, bench / "bugs" / bug.system / bug.number / name)
        (tests_dir / "bug_id.txt").write_text(bug.bug_id + "\n")
        manifest = {
            "benchmark_repo": BENCHMARK_REPO,
            "benchmark_commit": BENCHMARK_COMMIT,
            "files_sha256": {f"distdebug_bench/{n}": _sha256(pkg / n) for n in SCORER_FILES},
        }
        (tests_dir / "upstream_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return task_dir

    def run(self) -> list[Path]:
        root = self._source()
        systems = json.loads((root / "benchmark" / "systems.json").read_text())
        snapshots = json.loads(SNAPSHOTS_FILE.read_text())["bugs"]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for bug_id in self._bug_ids(root):
            written.append(self.generate_task(root, systems, snapshots, self._load_bug(root, bug_id)))
            logger.info("Wrote %s", written[-1])
        return written
