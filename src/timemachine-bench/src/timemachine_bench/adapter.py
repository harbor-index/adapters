"""Generate Harbor tasks from the TimeMachine-bench Verified split.

Each upstream record is a Python repository pinned to a historical commit whose
unchanged tests fail on Python 3.12.11 with the dependency versions available
on 2025-07-31. The agent edits the repository until the upstream test script
passes. See README.md for the mapping from the upstream harness.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path

UPSTREAM_REPO = "https://github.com/tohoku-nlp/timemachine-bench"
UPSTREAM_COMMIT = "9928dbf1af1405433d2c2e40227f39fd831d3863"
VERIFIED_JSONL_URL = (
    "https://raw.githubusercontent.com/tohoku-nlp/timemachine-bench/"
    f"{UPSTREAM_COMMIT}/benchmark/data/v1/timemachine-bench-verified.jsonl"
)
VERIFIED_JSONL_SHA256 = "eafedd60f623234fad760bf82a271cad98047ddcc2fdeed32cc89e96408ed3a9"

# services/pypi-timemachine submodule commit at UPSTREAM_COMMIT.
PYPI_TIMEMACHINE_COMMIT = "37eb7d113cea9eafa653949f3223e0df2e8ea56c"
# The image every upstream record's Dockerfile starts from, pinned by digest.
UPSTREAM_BASE_IMAGE = "mirror.gcr.io/python:3.12.11"
BASE_IMAGE = (
    f"{UPSTREAM_BASE_IMAGE}@sha256:c1a5d356638cc86bd865d9019efbc34a6b0c3ad15a21e5ab4eb57bd2d1c3f7ce"
)

# Parity subset: 10 of the 100 Verified records (10%), sampled once with
# random.Random(2026) within each difficulty level in proportion to its size
# (Easy 64 -> 6, Medium 30 -> 3, Hard 6 -> 1). Kept as a literal so the split
# never changes.
PARITY_TASK_IDS = [
    # Easy
    "demorest__sdmpy",
    "exa-analytics__exa",
    "joway__lemon",
    "mycroftai__mycroft-messagebus-client",
    "serioesegmbh__hackwork-gayson",
    "sfneal__pillowimage",
    # Medium
    "behance__chamberlain",
    "scrapedia__scrapy-httpcache",
    "uma-pi1__kge",
    # Hard
    "ledgerhq__marshmallow_dataclass",
]

TEMPLATE_DIR = Path(__file__).parent / "task-template"
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_DOCKER_PLACEHOLDER = re.compile(r"__([A-Z_]+)__")


@dataclass(frozen=True)
class Record:
    raw: dict

    @property
    def repo_name(self) -> str:
        return self.raw["repo_name"]

    @property
    def escaped_name(self) -> str:
        # upstream's file naming: setup_<owner>__<repo>.sh, test_<owner>__<repo>.sh
        return self.repo_name.replace("/", "__")

    @property
    def task_id(self) -> str:
        return sanitize_task_id(self.repo_name)

    @property
    def setup_script(self) -> str:
        return f"setup_{self.escaped_name}.sh"

    @property
    def test_script(self) -> str:
        return f"test_{self.escaped_name}.sh"

    @property
    def test_files(self) -> list[str]:
        # same parsing as upstream agents/src/setup/init/02_extract_assets.py
        return [p.strip() for p in self.raw["test_files"].split(",") if p.strip()]


def sanitize_task_id(repo_name: str) -> str:
    """`Owner/Repo.Name` -> `owner__repo.name` (registry-safe, stable)."""
    slug = repo_name.replace("/", "__").lower()
    slug = re.sub(r"[^a-z0-9._-]", "-", slug)
    return slug.strip("-.")


def load_records(jsonl_path: Path | None, cache_dir: Path) -> list[Record]:
    if jsonl_path is None:
        jsonl_path = cache_dir / f"timemachine-bench-verified-{UPSTREAM_COMMIT[:12]}.jsonl"
        if not jsonl_path.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = jsonl_path.with_suffix(".part")
            with urllib.request.urlopen(VERIFIED_JSONL_URL, timeout=120) as resp:
                tmp.write_bytes(resp.read())
            tmp.rename(jsonl_path)
        digest = hashlib.sha256(jsonl_path.read_bytes()).hexdigest()
        if digest != VERIFIED_JSONL_SHA256:
            raise RuntimeError(
                f"{jsonl_path} has sha256 {digest}, expected {VERIFIED_JSONL_SHA256} "
                f"(upstream commit {UPSTREAM_COMMIT})"
            )
    records = [
        Record(json.loads(line)) for line in jsonl_path.read_text().splitlines() if line.strip()
    ]
    ids = [r.task_id for r in records]
    if len(set(ids)) != len(ids):
        raise RuntimeError("task ids are not unique after sanitization")
    return records


def _toml_str(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _render(template: str, values: dict[str, str]) -> str:
    def sub(match: re.Match) -> str:
        key = match.group(1)
        return values[key] if key in values else match.group(0)

    return _PLACEHOLDER.sub(sub, template)


def _render_docker(template: str, values: dict[str, str]) -> str:
    def sub(match: re.Match) -> str:
        return values[match.group(1)]

    return _DOCKER_PLACEHOLDER.sub(sub, template)


def upstream_extra_steps(record: Record) -> list[str]:
    """Dockerfile lines of the upstream record other than the common skeleton
    (FROM, ENV PIP_*, WORKDIR, COPY repo, RUN setup, ENTRYPOINT, CMD).

    One record (miracle2k/k8s-snapshots) adds a poetry package source pointing
    at the date-filtered index; such lines are kept verbatim, in order.
    """
    common = {
        "FROM mirror.gcr.io/python:3.12.11",
        f"ENV PIP_INDEX_URL=http://localhost:5000/snapshot/{record.raw['migration_target_date']}",
        "ENV PIP_TRUSTED_HOST=localhost",
        "WORKDIR /work",
        f"COPY {record.escaped_name} .",
        f"RUN bash /work/{record.setup_script}",
        'ENTRYPOINT ["bash"]',
        f'CMD ["/work/{record.test_script}"]',
    }
    lines = [line for line in record.raw["dockerfile"].splitlines() if line.strip()]
    missing = common - set(lines)
    if missing:
        raise ValueError(f"{record.repo_name}: unexpected upstream Dockerfile, missing {missing}")
    return [line for line in lines if line not in common]


class TimeMachineBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "verified",
        jsonl_path: Path | None = None,
        cache_dir: Path | None = None,
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split
        self.jsonl_path = jsonl_path
        self.cache_dir = cache_dir or Path.home() / ".cache" / "harbor-timemachine-bench"

    def select(self, records: list[Record]) -> list[Record]:
        selected = records
        if self.split == "parity":
            wanted = set(PARITY_TASK_IDS)
            selected = [r for r in selected if r.task_id in wanted]
        elif self.split != "verified":
            raise ValueError(f"unknown split {self.split!r}; expected 'verified' or 'parity'")
        if self.task_ids:
            wanted = {sanitize_task_id(t) for t in self.task_ids}
            unknown = wanted - {r.task_id for r in records}
            if unknown:
                raise ValueError(f"unknown task ids: {sorted(unknown)}")
            selected = [r for r in selected if r.task_id in wanted]
        if self.limit is not None:
            selected = selected[: self.limit]
        return selected

    def run(self) -> None:
        records = self.select(load_records(self.jsonl_path, self.cache_dir))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        written = skipped = 0
        for record in records:
            task_dir = self.output_dir / record.task_id
            if task_dir.exists():
                if not self.overwrite:
                    skipped += 1
                    continue
                shutil.rmtree(task_dir)
            self.generate_task(record, task_dir)
            written += 1
        print(
            f"Wrote {written} task(s) to {self.output_dir}"
            + (f"; skipped {skipped} existing (use --overwrite)" if skipped else "")
        )

    def generate_task(self, record: Record, task_dir: Path) -> None:
        raw = record.raw
        shutil.copytree(
            TEMPLATE_DIR, task_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )

        values = {
            "task_id": record.task_id,
            "repo_name": _toml_str(record.repo_name),
            "repo_url": _toml_str(raw["repo_url"]),
            "commit_hash": raw["commit_hash"],
            "difficulty": raw["difficulty"].lower(),
            "test_type": _toml_str(raw["test_type"]),
            "test_count": str(int(raw["test_count"])),
            "related_modules": _toml_str(raw["related_modules"]),
            "license": _toml_str(raw["license"]),
            "reproduction_target_date": raw["reproduction_target_date"],
            "reproduction_target_version": raw["reproduction_target_version"],
            "migration_date": raw["migration_target_date"],
            "python_version": raw["migration_target_version"],
        }
        toml_path = task_dir / "task.toml"
        toml_path.write_text(_render(toml_path.read_text(), values))

        instruction_path = task_dir / "instruction.md"
        instruction_path.write_text(
            _render(
                instruction_path.read_text(),
                {
                    "test_script": record.test_script,
                    "setup_script": record.setup_script,
                    "python_version": raw["migration_target_version"],
                    "dependency_versions": raw["dependency_versions"].strip("\n"),
                    "test_files": "\n".join(record.test_files),
                },
            )
        )

        env_dir = task_dir / "environment"
        patches_dir = env_dir / "timemachine" / "patches"
        patches_dir.mkdir(parents=True)
        (patches_dir / "patch.diff").write_text(raw["patch"])
        apply = "patch -p0 -t -E -d /work < /opt/timemachine/patches/patch.diff"
        if raw["test_patch"].strip():
            (patches_dir / "test_patch.diff").write_text(raw["test_patch"])
            apply += " \\\n && patch -p0 -t -E -d /work < /opt/timemachine/patches/test_patch.diff"
        extra = upstream_extra_steps(record)
        extra_block = ""
        if extra:
            extra_block = (
                "\n# Upstream per-record Dockerfile step(s), verbatim.\n" + "\n".join(extra) + "\n"
            )
        dockerfile = env_dir / "Dockerfile"
        dockerfile.write_text(
            _render_docker(
                dockerfile.read_text(),
                {
                    "REPO_NAME": record.repo_name,
                    "REPO_URL": raw["repo_url"],
                    "COMMIT_HASH": raw["commit_hash"],
                    "MIGRATION_DATE": raw["migration_target_date"],
                    "BASE_IMAGE": BASE_IMAGE,
                    "PYPI_TIMEMACHINE_COMMIT": PYPI_TIMEMACHINE_COMMIT,
                    "APPLY_PATCHES": apply,
                    "EXTRA_UPSTREAM_STEPS": extra_block,
                    "SETUP_SCRIPT": record.setup_script,
                },
            )
        )

        (task_dir / "solution" / "gold.patch").write_text(raw["gold_patch"])
        (task_dir / "tests" / "gold.patch").write_text(raw["gold_patch"])
        (task_dir / "tests" / "task_info.json").write_text(
            json.dumps(
                {
                    "repo_name": record.repo_name,
                    "setup_script": record.setup_script,
                    "test_script": record.test_script,
                    "test_files": record.test_files,
                },
                indent=2,
            )
            + "\n"
        )
        for script in task_dir.rglob("*.sh"):
            script.chmod(0o755)
