"""Generate Harbor tasks from SWEeper-Bench.

Each upstream case becomes one Harbor task:

* ``instruction.md``: the exact prompt the upstream runner gives a coding
  agent at the default settings (``prediction.level = "scoped"``,
  ``prediction.prompt_templates = "browser"``, unlimited time budget).
* ``environment/``: the upstream *prediction* product image plus a flattened
  checkout of the buggy commit; the product is started on 127.0.0.1:13200.
* ``solution/``: the upstream reference fix as ``git diff base..reference``.
* ``tests/``: a separate verifier environment built from the upstream
  *evaluation* product image, with the upstream ``evaluation-codex`` browser
  verifier as a sidecar running the unchanged upstream ``browser.py``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

TEMPLATE_DIR = Path(__file__).parent / "task-template"
PROMPT_DIR = Path(__file__).parent / "upstream_prompts"

HF_DATASET = "zlab-princeton/SWEeper-Bench"
HF_FILE = "data/test.jsonl"
# Pinned inputs (see README "Notes & Caveats").
DATASET_REVISION = "f10cd684be52322667f1373c8295624e13791b8f"
RUNNER_COMMIT = "96f86f2bb28f0de6c302c910aabe7baf3fb90b6a"
# Parity fork: upstream plus an optional OPENAI_BASE_URL (no prompt/scoring change).
PARITY_FORK = "https://github.com/Chenngzz/sweeper-bench/tree/harbor-parity"
IMAGE_REGISTRY = "ghcr.io/evigbyen"
IMAGE_TAG = "alpha"
# Versions in the upstream prediction agent images (prediction-codex,
# prediction-claude-code): Node 20.11.1 and the Playwright 1.63.0 CLI.
NODE_VERSION = "20.11.1"
PLAYWRIGHT_VERSION = "1.63.0"

# Fixed task ids used for the three-task pilot and the default parity split.
PILOT_TASK_IDS = ["sweeper-001", "sweeper-073", "sweeper-102"]
# 5% stratified parity subset of the 198 adapted tasks (seed 20261126):
# PHP 6, Go 2, TS/JS 1, Python 1; includes two of the three pilot tasks.
PARITY_TASK_IDS = [
    "sweeper-001",
    "sweeper-008",
    "sweeper-019",
    "sweeper-058",
    "sweeper-073",
    "sweeper-075",
    "sweeper-097",
    "sweeper-157",
    "sweeper-179",
    "sweeper-180",
]
SPLITS = {"pilot": PILOT_TASK_IDS, "parity": PARITY_TASK_IDS}

# Upstream reference fixes that the upstream runner's own `reference` phase does
# not verify (target workflow fails on the original benchmark as well). Left out
# of the default dataset; they can still be generated explicitly with --task-ids.
EXCLUDED_TASKS = {
    "sweeper-045": "upstream reference fix fails the target workflow on the original runner",
    "sweeper-150": "upstream reference fix fails the target workflow on the original runner",
}

REQUIRED_FIELDS = {
    "case_id",
    "slug",
    "repo",
    "base_commit",
    "reference_commit",
    "task_scoped",
    "task_specified",
    "pr",
    "issue",
    "workflows",
}


@dataclass
class Case:
    case_id: str
    slug: str
    repo: str
    base_commit: str
    reference_commit: str
    task_scoped: str
    pr: object
    workflows: str


def load_rows(dataset_file: Path | None, revision: str) -> list[dict]:
    """Read the upstream JSONL from a local file or the pinned HF revision."""
    if dataset_file is None:
        from huggingface_hub import hf_hub_download

        dataset_file = Path(
            hf_hub_download(HF_DATASET, HF_FILE, repo_type="dataset", revision=revision)
        )
    rows = []
    for line in dataset_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def to_case(raw: dict) -> Case:
    """Same schema checks as upstream src/data.py:adapt()."""
    missing = REQUIRED_FIELDS - set(raw)
    unexpected = set(raw) - REQUIRED_FIELDS
    if missing or unexpected:
        raise ValueError(f"dataset schema mismatch: missing={sorted(missing)} unexpected={sorted(unexpected)}")
    if not re.fullmatch(r"sweeper-\d{3}", raw["case_id"]):
        raise ValueError(f"invalid case id: {raw['case_id']}")
    for key in ("base_commit", "reference_commit"):
        if not re.fullmatch(r"[0-9a-f]{40}", raw[key] or ""):
            raise ValueError(f"{raw['case_id']}: invalid {key}")
    workflows = yaml.safe_load(raw["workflows"]) or {}
    categories = sorted(w.get("category") for w in workflows.get("workflows") or [])
    if categories != ["preservation", "target"]:
        raise ValueError(f"{raw['case_id']}: expected one target and one preservation workflow")
    scoped = str(raw.get("task_scoped") or "").strip()
    if not scoped:
        raise ValueError(f"{raw['case_id']}: empty task_scoped")
    return Case(
        case_id=raw["case_id"],
        slug=raw.get("slug") or "",
        repo=raw["repo"],
        base_commit=raw["base_commit"],
        reference_commit=raw["reference_commit"],
        task_scoped=scoped,
        pr=raw.get("pr"),
        workflows=raw["workflows"],
    )


def render_prompt(task: str, templates: str = "browser") -> str:
    """Port of render_prompt() in the upstream runtime/worker.py."""
    body = str(task).strip() + "\n"
    lines = (PROMPT_DIR / f"task-templates-{templates}.md").read_text().splitlines(True)
    out = []
    header = True
    for line in lines:
        if header:
            if line.startswith("#") or not line.strip():
                continue
            header = False
        if "{{TASK_DESCRIPTION}}" in line:
            out.append(body)
            continue
        out.append(line)
    return "".join(out)


def reference_patch(case: Case, cache_dir: Path) -> bytes:
    """`git diff --binary base reference`, cached per case.

    Kept as raw bytes end to end: some repositories commit CRLF files, and any
    newline translation breaks the patch context.
    """
    cached = cache_dir / f"{case.case_id}.patch"
    if cached.is_file():
        return cached.read_bytes()
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{case.case_id}-") as tmp:
        git = ["git", "-C", tmp]
        subprocess.run(["git", "init", "-q", tmp], check=True)
        subprocess.run([*git, "remote", "add", "origin", case.repo], check=True)
        shallow = subprocess.run(
            [*git, "fetch", "-q", "--depth", "1", "origin", case.base_commit, case.reference_commit]
        )
        if shallow.returncode != 0:
            subprocess.run([*git, "fetch", "-q", "origin"], check=True)
        diff = subprocess.run(
            [*git, "diff", "--binary", case.base_commit, case.reference_commit],
            check=True,
            capture_output=True,
        ).stdout
    if b"diff --git " not in diff:
        raise RuntimeError(f"{case.case_id}: empty reference diff")
    cached.write_bytes(diff)
    return diff


def _fill(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def _toml_str(value: object) -> str:
    return json.dumps("" if value is None else str(value))[1:-1]


class SWEeperBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str | None = None,
        dataset_file: Path | None = None,
        dataset_revision: str = DATASET_REVISION,
        image_registry: str = IMAGE_REGISTRY,
        image_tag: str = IMAGE_TAG,
        cache_dir: Path | None = None,
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split
        self.dataset_file = dataset_file
        self.dataset_revision = dataset_revision
        self.image_registry = image_registry.rstrip("/")
        self.image_tag = image_tag
        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "harbor-sweeper-bench")

    def select(self, cases: list[Case]) -> list[Case]:
        wanted = None
        if self.split:
            if self.split not in SPLITS:
                raise SystemExit(f"unknown split {self.split!r}; known: {', '.join(SPLITS)}")
            wanted = list(SPLITS[self.split])
        if self.task_ids:
            wanted = [t for t in self.task_ids if wanted is None or t in wanted]
        if wanted is not None:
            by_id = {c.case_id: c for c in cases}
            unknown = [t for t in wanted if t not in by_id]
            if unknown:
                raise SystemExit(f"unknown task ids: {', '.join(unknown)}")
            cases = [by_id[t] for t in wanted]
        else:
            cases = [c for c in cases if c.case_id not in EXCLUDED_TASKS]
        cases = sorted(cases, key=lambda c: c.case_id)
        if self.limit is not None:
            cases = cases[: self.limit]
        return cases

    def run(self) -> None:
        rows = load_rows(self.dataset_file, self.dataset_revision)
        cases = [to_case(r) for r in rows]
        if len({c.case_id for c in cases}) != len(cases):
            raise ValueError("duplicate case ids")
        selected = self.select(cases)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for case in selected:
            dest = self.output_dir / case.case_id
            if dest.exists():
                if not self.overwrite:
                    print(f"skip {case.case_id}: exists (use --overwrite)")
                    continue
                shutil.rmtree(dest)
            self.generate(case, dest)
            print(f"wrote {dest}")

    def generate(self, case: Case, dest: Path) -> None:
        shutil.copytree(TEMPLATE_DIR, dest)
        # The verifier image builds from tests/ and needs the checkout helper too.
        shutil.copy2(dest / "environment" / "sweeper" / "checkout.sh", dest / "tests" / "sweeper" / "checkout.sh")
        num = case.case_id.split("-")[1]
        values = {
            "task_id": case.case_id,
            "case_id": case.case_id,
            "slug": _toml_str(case.slug),
            "repo": case.repo,
            "base_commit": case.base_commit,
            "reference_commit": case.reference_commit,
            "pr": _toml_str(case.pr),
            "description": _toml_str(case.task_scoped),
            "language_keyword": _toml_str(case.slug),
            "runner_commit": RUNNER_COMMIT,
            "dataset_revision": self.dataset_revision,
            "prediction_image": f"{self.image_registry}/sweeper-{num}-prediction:{self.image_tag}",
            "evaluation_image": f"{self.image_registry}/sweeper-{num}:{self.image_tag}",
            "verifier_image": f"{self.image_registry}/evaluation-codex:{self.image_tag}",
            "node_version": NODE_VERSION,
            "playwright_version": PLAYWRIGHT_VERSION,
        }
        for rel in (
            "task.toml",
            "environment/Dockerfile",
            "tests/Dockerfile",
            "tests/verifier.Dockerfile",
        ):
            path = dest / rel
            path.write_text(_fill(path.read_text(), values))
        (dest / "instruction.md").write_text(render_prompt(case.task_scoped, "browser"))
        (dest / "tests" / "workflows.yaml").write_text(case.workflows)
        patch = reference_patch(case, self.cache_dir / "reference-patches")
        (dest / "solution" / "reference.patch").write_bytes(patch)
        for script in dest.rglob("*.sh"):
            script.chmod(0o755)
