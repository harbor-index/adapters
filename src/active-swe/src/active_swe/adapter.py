"""Convert Active-SWE (Recorded track) instances into Harbor task directories.

Each upstream instance ships a prebuilt Docker image, the files pending review,
a gold patch, an eval script, a log parser name and the ground-truth
FAIL_TO_PASS / PASS_TO_PASS tests. The generated task:

- environment/Dockerfile  FROM the upstream instance image (+ git sanitize,
                          + an off-PATH Python for the verifier)
- instruction.md          upstream Recorded prompt (PATCH_GEN_PROMPT)
- solution/               gold patch + solve.sh (oracle)
- tests/                  eval script, GT config, vendored upstream grader
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

HF_DATASET = "XLearning-SCU/Active-SWE"
# Pinned dataset revision (2026-08-06) so task content is reproducible.
HF_REVISION = "cf042f048bfe717277f598bf2cd7e8038a48ad39"
HF_FILE = "data/Active-SWE.parquet"

AGENT_TIMEOUT_SEC = 5400  # upstream --claude-timeout-seconds default
VERIFIER_TIMEOUT_SEC = 3600

TEMPLATE_DIR = Path(__file__).parent / "task-template"
ENVIRONMENT_FIXES = {
    "hard-nushell__nushell__4951": "cargo-locked.Dockerfile",
    "hard-pygments__pygments__2347": "conda-testbed.Dockerfile",
    "hard-pygments__pygments__2354": "conda-testbed.Dockerfile",
    "hard-spulec__moto__3868": "conda-testbed.Dockerfile",
    "simple-astral-sh__ruff__199": "cargo-locked.Dockerfile",
    "simple-napari__napari__2099": "conda-testbed.Dockerfile",
    "simple-nushell__nushell__16838": "cargo-locked.Dockerfile",
    "simple-nushell__nushell__16957": "cargo-locked.Dockerfile",
    "simple-nushell__nushell__4120": "cargo-locked.Dockerfile",
    "simple-nushell__nushell__4382": "cargo-locked.Dockerfile",
    "simple-nushell__nushell__4514": "cargo-locked.Dockerfile",
    "simple-nushell__nushell__4777": "cargo-locked.Dockerfile",
    "simple-nushell__nushell__5779": "cargo-locked.Dockerfile",
    "simple-pandas-dev__pandas__39420": "conda-testbed.Dockerfile",
    "simple-pandas-dev__pandas__44873": "conda-testbed.Dockerfile",
    "simple-pandas-dev__pandas__47206": "conda-testbed.Dockerfile",
    "simple-tobymao__sqlglot__1309": "conda-testbed.Dockerfile",
    "simple-tobymao__sqlglot__1340": "conda-testbed.Dockerfile",
    "simple-tobymao__sqlglot__772": "conda-testbed.Dockerfile",
    "simple-tobymao__sqlglot__899": "conda-testbed.Dockerfile",
    "simple-uutils__coreutils__2161": "cargo-locked.Dockerfile",
    "simple-uutils__coreutils__3033": "cargo-locked.Dockerfile",
    "simple-uutils__coreutils__3053": "cargo-locked.Dockerfile",
    "simple-uutils__coreutils__3126": "cargo-locked.Dockerfile",
    "simple-uutils__coreutils__3591": "cargo-locked.Dockerfile"
}
PARITY_IDS_FILE = Path(__file__).parent / "parity_task_ids.txt"


def make_task_id(set_name: str, instance_id: str) -> str:
    """Stable, registry-safe local id. `set` is included because 3 instance ids
    appear in both the simple and hard sets."""
    raw = f"{set_name}-{instance_id}".lower()
    return re.sub(r"[^a-z0-9_.-]+", "-", raw).strip("-")


def _toml_str(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class ActiveSWERecord:
    instance_id: str
    set: str
    repo: str
    language: str
    image: str
    files_pending_review: list[str]
    patch: str
    eval_script: str
    test_framework: str
    log_parser: str
    major_categories: list[str]
    fail_to_pass: list[str]
    pass_to_pass: list[str]

    @property
    def task_id(self) -> str:
        return make_task_id(self.set, self.instance_id)

    @classmethod
    def from_row(cls, row: dict) -> "ActiveSWERecord":
        return cls(
            instance_id=row["instance_id"],
            set=row["set"],
            repo=row["repo"],
            language=row["language"],
            image=row["image"],
            files_pending_review=list(row["files_pending_review"]),
            patch=row["patch"],
            eval_script=row["eval_script"],
            test_framework=row["test_framework"],
            log_parser=row["log_parser"],
            major_categories=[str(c) for c in (row.get("major_category") or [])],
            fail_to_pass=list(row["FAIL_TO_PASS"]),
            pass_to_pass=list(row["PASS_TO_PASS"]),
        )


def load_records(data_file: Path | None = None, cache_dir: Path | None = None) -> list[ActiveSWERecord]:
    if data_file is None:
        cache_dir = cache_dir or Path.home() / ".cache" / "harbor-active-swe"
        cache_dir.mkdir(parents=True, exist_ok=True)
        data_file = cache_dir / f"Active-SWE-{HF_REVISION[:12]}.parquet"
        if not data_file.exists():
            url = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/{HF_REVISION}/{HF_FILE}"
            print(f"Downloading {url}")
            tmp = data_file.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)
            tmp.rename(data_file)
    rows = pq.read_table(data_file).to_pylist()
    records = [ActiveSWERecord.from_row(r) for r in rows]
    ids = [r.task_id for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError("task ids are not unique")
    return records


class ActiveSWEAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "full",
        data_file: Path | None = None,
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split
        self.data_file = data_file

    def _select(self, records: list[ActiveSWERecord]) -> list[ActiveSWERecord]:
        records = sorted(records, key=lambda r: r.task_id)
        if self.split == "parity":
            wanted = set(PARITY_IDS_FILE.read_text().split())
            records = [r for r in records if r.task_id in wanted]
        if self.task_ids:
            wanted = set(self.task_ids)
            records = [r for r in records if r.task_id in wanted or r.instance_id in wanted]
        if self.limit is not None:
            records = records[: self.limit]
        return records

    def run(self) -> None:
        records = self._select(load_records(self.data_file))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        for rec in records:
            if self.generate_task(rec):
                written += 1
        print(f"Wrote {written} task(s) to {self.output_dir} (selected {len(records)})")

    def generate_task(self, rec: ActiveSWERecord) -> bool:
        task_dir = self.output_dir / rec.task_id
        if task_dir.exists():
            if not self.overwrite:
                print(f"skip (exists): {rec.task_id}")
                return False
            shutil.rmtree(task_dir)
        shutil.copytree(TEMPLATE_DIR, task_dir)

        # instruction.md: upstream PATCH_GEN_PROMPT, rendered the same way jinja does.
        instr_path = task_dir / "instruction.md"
        files_block = "".join(f"- {f}\n" for f in rec.files_pending_review)
        instr_path.write_text(instr_path.read_text().replace("{files_pending_review}", files_block))

        # task.toml
        toml_path = task_dir / "task.toml"
        toml = toml_path.read_text()
        for key, value in {
            "task_id": rec.task_id,
            "instance_id": rec.instance_id,
            "set": rec.set,
            "repo": rec.repo,
            "language": rec.language,
            "language_tag": rec.language.lower().replace("+", "p"),
            "test_framework": rec.test_framework,
            "difficulty": "hard" if rec.set == "hard" else "medium",
            "agent_timeout": f"{AGENT_TIMEOUT_SEC:.1f}",
            "verifier_timeout": f"{VERIFIER_TIMEOUT_SEC:.1f}",
        }.items():
            toml = toml.replace("{" + key + "}", _toml_str(value))
        # JSON string arrays are valid TOML arrays.
        toml = toml.replace("{major_category}", json.dumps(rec.major_categories))
        toml_path.write_text(toml)

        # environment/Dockerfile
        df_path = task_dir / "environment" / "Dockerfile"
        dockerfile = df_path.read_text().replace("{docker_image}", rec.image)
        fix = ENVIRONMENT_FIXES.get(rec.task_id)
        if fix:
            dockerfile += "\n" + (Path(__file__).parent / "environment-fixes" / fix).read_text()
        df_path.write_text(dockerfile)

        # solution/
        (task_dir / "solution" / "gold.patch").write_text(rec.patch)

        # tests/
        tests = task_dir / "tests"
        (tests / "eval_script.sh").write_text(rec.eval_script)
        (tests / "config.json").write_text(
            json.dumps(
                {
                    "instance_id": rec.instance_id,
                    "set": rec.set,
                    "repo": rec.repo,
                    "language": rec.language,
                    "test_framework": rec.test_framework,
                    "log_parser": rec.log_parser,
                    "FAIL_TO_PASS": rec.fail_to_pass,
                    "PASS_TO_PASS": rec.pass_to_pass,
                },
                indent=2,
            )
        )
        for sh in (task_dir / "solution" / "solve.sh", tests / "test.sh", tests / "eval_script.sh"):
            sh.chmod(0o755)
        return True
