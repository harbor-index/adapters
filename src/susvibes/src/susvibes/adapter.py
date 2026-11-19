"""Convert SusVibes instances into Harbor task directories.

SusVibes (https://github.com/LeiLiLab/susvibes) is a benchmark of 186
repository-level feature tasks. Every instance ships a pre-built Docker image,
a natural-language problem statement, a golden patch and a security test
patch. A submission is scored on functional correctness (the repository's own
tests) and on security (the same suite with the security tests added).

The adapter reads three files from a pinned upstream commit:

* ``datasets/default/susvibes_dataset.jsonl``: the instances
* ``susvibes/env_specs/default/dockerfile.json``: per-instance test command
* ``susvibes/env_specs/default/logs_handler.json``: per-instance log parser
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path

UPSTREAM_REPO = "LeiLiLab/susvibes"
UPSTREAM_COMMIT = "5d957e6f1850595623d551d089193532e6fa60ce"
UPSTREAM_DATASET_ID = "default"

# path in the upstream repository -> sha256 at UPSTREAM_COMMIT
UPSTREAM_FILES = {
    "dataset": (
        f"datasets/{UPSTREAM_DATASET_ID}/susvibes_dataset.jsonl",
        "0cb5fbffe7ba59a8e16d42c293944bdd8e1e23795941a4b496ac1043722b9550",
    ),
    "dockerfile": (
        f"susvibes/env_specs/{UPSTREAM_DATASET_ID}/dockerfile.json",
        "1144b2120c397c9c234f11fc7bbb02f3ab4a4bc57f74f03462d32a5507786298",
    ),
    "logs_handler": (
        f"susvibes/env_specs/{UPSTREAM_DATASET_ID}/logs_handler.json",
        "26af6c737a623d83b9bf130311a0b52bf6a012bc380190d4a51b52338c83838a",
    ),
}

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "harbor-susvibes-adapter"

# Where the repository lives in every task image (WORKSPACE_DIR_NAME upstream).
WORK_DIR = "/project"
# ContainerLimits.RUN_TIMEOUT upstream: seconds allowed for one test-suite run.
RUN_TIMEOUT_SEC = 1800

# Upstream instances this adapter does not generate, with the reason: the
# golden patch does not pass the upstream evaluation at the pinned commit, or
# (urllib3) passes only under one DNS setup (see README, Notes & Caveats).
EXCLUDED_INSTANCES: dict[str, str] = {
    "celery__celery_1f7ad7e6df1e02039b6ab9eec617d283598cad6b": (
        "the security test_patch does not apply to the published task image"
    ),
    "gitpython-developers__gitpython_ca965ecc81853bca7675261729143f54e5bf4cdd": (
        "the golden patch fails the functional threshold (10 failures, 1 allowed): the "
        "test suite needs the project's git history, which the task image does not have"
    ),
    "openstack__neutron_8c213e45902e21d2fe00639ef7d92b35304bde82": (
        "the instance's log parser does not match the unittest summary, so a run in "
        "which every test passes is scored as aborted"
    ),
    "planetlabs__planet-client-python_d71415a83119c5e89d7b80d5f940d162376ee3b7": (
        "the test command (nox) reinstalls unpinned dependencies on every run; with the "
        "current click release the golden patch has 1 failure, 0 allowed"
    ),
    "rohe__pysaml2_6e09a25d9b4b7aa7a506853210a9a14100b8bc9b": (
        "the golden patch has 17 failures, 16 allowed: tests depend on the current date "
        "and on md.incommon.org, which no longer resolves"
    ),
    "sabnzbd__sabnzbd_3766ba54026eaa520dbee5b57a2f33d4954fb98b": (
        "three tests call the project's public self-test host, which answers HTTP 429 "
        "when the two test runs follow each other, so the golden patch does not pass reliably"
    ),
    "urllib3__urllib3_01220354d389cd05474713f8c982d05c9b17aafb": (
        "the test command (nox) reinstalls unpinned dependencies on every run; with the "
        "current cryptography release the golden patch has 29 failures, 4 allowed"
    ),
    "urllib3__urllib3_8d65ea1ecf6e2cdc27d42124e587c1b83a3118b0": (
        "five tests run only when the container resolves 'localhost.'; they are skipped "
        "on Docker's default bridge with public DNS (the upstream setup) but run on "
        "Compose networks, where the golden patch then has 3 failures, 2 allowed"
    ),
}

# Parity subset: a fixed random sample of the sorted, non-excluded instance ids.
PARITY_SEED = 42
PARITY_SIZE = 50

# The CMD statement of an upstream env-spec Dockerfile (same shape as the last
# group of susvibes.env_specs.constants.DOCKERFILE_PATTERN).
_CMD_RE = re.compile(r"^CMD((?:[^\r\n]*\\\r?\n)*[^\r\n]*)(?:\r?\n|$)", re.MULTILINE)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _render(template: str, **values: str) -> str:
    """Replace ``{key}`` placeholders literally (values may contain braces)."""
    pattern = re.compile("|".join(re.escape("{" + key + "}") for key in values))
    return pattern.sub(lambda m: values[m.group(0)[1:-1]], template)


def _toml_string(value: str) -> str:
    """Contents of a TOML basic string (JSON escaping is a subset of TOML's)."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


def parse_test_cmd(dockerfile: str) -> dict:
    """Extract the test command from an env-spec Dockerfile.

    Upstream runs each evaluation container with this CMD. Shell form is
    executed by ``/bin/sh -c``; exec form (a JSON array) is executed directly.
    """
    statements = _CMD_RE.findall(dockerfile)
    if len(statements) != 1:
        raise ValueError(f"Expected exactly one CMD statement, found {len(statements)}")
    body = re.sub(r"\\\r?\n", "", statements[0]).strip()
    if not body:
        raise ValueError("Empty CMD statement")
    if body.startswith("["):
        try:
            argv = json.loads(body)
        except json.JSONDecodeError:
            argv = None
        if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):
            return {"form": "exec", "value": argv}
    return {"form": "shell", "value": body}


def new_files(patch: str) -> list[str]:
    """Paths a unified diff creates (old side ``/dev/null``), in patch order."""
    created: list[str] = []
    old_side = None
    for line in patch.splitlines():
        if line.startswith("--- "):
            old_side = line[4:].split("\t", 1)[0]
        elif line.startswith("+++ ") and old_side is not None:
            path = line[4:].split("\t", 1)[0]
            if old_side == "/dev/null" and path != "/dev/null":
                created.append(path.removeprefix("b/"))
            old_side = None
        else:
            old_side = None
    return list(dict.fromkeys(created))


@dataclass
class SusVibesRecord:
    instance_id: str
    project: str
    base_commit: str
    language: str
    problem_statement: str
    image_name: str
    golden_patch: str
    test_patch: str
    expected_pf: dict
    flags: dict
    cwe_ids: list[str]
    cve_id: str

    @classmethod
    def from_dict(cls, d: dict) -> SusVibesRecord:
        return cls(
            instance_id=d["instance_id"],
            project=d["project"],
            base_commit=d["base_commit"],
            language=d["language"],
            problem_statement=d["problem_statement"],
            image_name=d["image_name"],
            golden_patch=d["golden_patch"],
            test_patch=d["test_patch"],
            expected_pf=d["expected_pf"],
            flags=d.get("flags") or {},
            cwe_ids=list(d.get("cwe_ids") or []),
            cve_id=d.get("cve_id") or "",
        )


class SusVibesLoader:
    """Loads the pinned upstream dataset and env specs.

    With ``susvibes_root`` the files are read from a local checkout of the
    upstream repository; otherwise they are downloaded once from GitHub at
    ``UPSTREAM_COMMIT`` into ``cache_dir``. Either way the content must match
    the pinned checksums.
    """

    def __init__(
        self,
        susvibes_root: Path | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.susvibes_root = Path(susvibes_root) if susvibes_root else None
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR) / UPSTREAM_COMMIT

        dataset_text = self._read("dataset").decode("utf-8")
        records = [
            json.loads(line) for line in dataset_text.split("\n") if line.strip()
        ]
        self._records = {r["instance_id"]: r for r in records}
        self._dockerfiles = json.loads(self._read("dockerfile"))
        self._logs_handlers = json.loads(self._read("logs_handler"))

    def _read(self, key: str) -> bytes:
        rel_path, expected = UPSTREAM_FILES[key]
        if self.susvibes_root is not None:
            data = (self.susvibes_root / rel_path).read_bytes()
        else:
            cached = self.cache_dir / rel_path
            if cached.is_file() and _sha256(cached.read_bytes()) == expected:
                return cached.read_bytes()
            url = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/{UPSTREAM_COMMIT}/{rel_path}"
            print(f"Downloading {url}")
            with urllib.request.urlopen(url, timeout=120) as response:
                data = response.read()
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
        actual = _sha256(data)
        if actual != expected:
            raise ValueError(
                f"{rel_path} does not match SusVibes commit {UPSTREAM_COMMIT[:12]} "
                f"(sha256 {actual}, expected {expected})."
            )
        return data

    def all_ids(self) -> list[str]:
        """Sorted ids of the instances the adapter generates."""
        return sorted(i for i in self._records if i not in EXCLUDED_INSTANCES)

    def load(self, instance_id: str) -> SusVibesRecord:
        if instance_id not in self._records:
            raise KeyError(f"Instance not found: {instance_id}")
        return SusVibesRecord.from_dict(self._records[instance_id])

    def test_cmd(self, instance_id: str) -> dict:
        return parse_test_cmd(self._dockerfiles[instance_id])

    def logs_handler(self, instance_id: str) -> dict:
        return self._logs_handlers[instance_id]


class SusVibesAdapter:
    """SusVibes -> Harbor converter using the file templates in ./task-template.

    Produces, per instance::

        <output-dir>/<task-id>/
            task.toml
            instruction.md
            environment/Dockerfile
            solution/solve.sh
            solution/golden.patch
            tests/test.sh
            tests/sv_eval.py
            tests/config.json
    """

    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "full",
        susvibes_root: Path | None = None,
        cache_dir: Path | None = None,
        template_dir: Path | None = None,
        **kwargs: object,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split
        self.template_dir = Path(
            template_dir or (Path(__file__).parent / "task-template")
        )
        self.loader = SusVibesLoader(susvibes_root=susvibes_root, cache_dir=cache_dir)

    @staticmethod
    def make_local_task_id(instance_id: str) -> str:
        """Registry-safe, stable task id: the upstream instance id, sanitized."""
        task_id = re.sub(r"[^a-z0-9_.-]+", "-", instance_id.lower()).strip("-._")
        if not task_id:
            raise ValueError(f"Cannot derive a task id from {instance_id!r}")
        return task_id

    def parity_ids(self) -> list[str]:
        ids = self.loader.all_ids()
        return sorted(
            random.Random(PARITY_SEED).sample(ids, min(PARITY_SIZE, len(ids)))
        )

    def select_ids(self) -> list[str]:
        if self.split == "full":
            ids = self.loader.all_ids()
        elif self.split == "parity":
            ids = self.parity_ids()
        else:
            raise ValueError(f"Unknown split: {self.split!r}")

        if self.task_ids is not None:
            by_local_id = {self.make_local_task_id(i): i for i in self.loader.all_ids()}
            selected = []
            excluded = {self.make_local_task_id(i): i for i in EXCLUDED_INSTANCES}
            for requested in self.task_ids:
                local_id = self.make_local_task_id(requested)
                if local_id in excluded:
                    reason = EXCLUDED_INSTANCES[excluded[local_id]]
                    raise KeyError(f"Task {requested} is excluded: {reason}")
                instance_id = by_local_id.get(local_id)
                if instance_id is None:
                    raise KeyError(f"Unknown task id: {requested}")
                selected.append(instance_id)
            ids = selected

        if self.limit is not None:
            ids = ids[: self.limit]
        return ids

    def _check_supported(self, rec: SusVibesRecord, logs_handler: dict) -> None:
        """Refuse instances whose scoring route this adapter does not implement.

        Every instance at the pinned commit takes the default route: no flags,
        the ``count`` logs handler and integer failure thresholds.
        """
        if rec.flags.get("gen_test") or rec.flags.get("test_adapter"):
            raise ValueError(f"Unsupported instance flags: {rec.flags}")
        if "count" not in logs_handler or "test_adapter" in logs_handler:
            raise ValueError(f"Unsupported logs handler kinds: {sorted(logs_handler)}")
        for run_name in ("func", "sec"):
            value = rec.expected_pf.get(run_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Unsupported expected_pf[{run_name!r}]: {value!r}")

    def generate_task(self, instance_id: str) -> Path:
        rec = self.loader.load(instance_id)
        logs_handler = self.loader.logs_handler(instance_id)
        self._check_supported(rec, logs_handler)

        task_id = self.make_local_task_id(instance_id)
        task_dir = self.output_dir / task_id
        if task_dir.exists():
            if not self.overwrite:
                raise FileExistsError(f"Target already exists: {task_dir}")
            shutil.rmtree(task_dir)
        shutil.copytree(self.template_dir, task_dir)

        # instruction.md: the upstream prompt template, placeholders filled in
        instruction_path = task_dir / "instruction.md"
        instruction = (
            instruction_path.read_text(encoding="utf-8")
            .replace("__WORK_DIR__", WORK_DIR)
            .replace("__PROBLEM_STATEMENT__", rec.problem_statement)
        )
        instruction_path.write_text(instruction, encoding="utf-8")

        # task.toml
        config_path = task_dir / "task.toml"
        config_path.write_text(
            _render(
                config_path.read_text(encoding="utf-8"),
                task_id=task_id,
                instance_id=_toml_string(rec.instance_id),
                project=_toml_string(rec.project),
                base_commit=_toml_string(rec.base_commit),
                language=_toml_string(rec.language),
                cve_id=_toml_string(rec.cve_id),
                cwe_ids=json.dumps(rec.cwe_ids),
                image_name=_toml_string(rec.image_name),
            ),
            encoding="utf-8",
        )

        # environment/Dockerfile
        dockerfile_path = task_dir / "environment" / "Dockerfile"
        dockerfile_path.write_text(
            _render(
                dockerfile_path.read_text(encoding="utf-8"), image_name=rec.image_name
            ),
            encoding="utf-8",
        )

        # tests/config.json: everything the grader needs, hidden from the agent
        config = {
            "instance_id": rec.instance_id,
            "upstream_commit": UPSTREAM_COMMIT,
            "test_cmd": self.loader.test_cmd(instance_id),
            "run_timeout_sec": RUN_TIMEOUT_SEC,
            "expected_pf": rec.expected_pf,
            "flags": rec.flags,
            "logs_handler": logs_handler,
            "test_patch": rec.test_patch,
        }
        (task_dir / "tests" / "config.json").write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        # solution/: the golden patch and the script that applies it
        (task_dir / "solution" / "golden.patch").write_text(
            rec.golden_patch, encoding="utf-8"
        )
        created = new_files(rec.golden_patch)
        if created:
            quoted = " ".join(
                "'" + path.replace("'", "'\\''") + "'" for path in created
            )
            register = (
                "\n# `git diff` only reports tracked files, so mark the files this patch\n"
                "# creates as intent-to-add to get them into the collected submission.\n"
                f"git add --intent-to-add -- {quoted}\n"
            )
        else:
            register = ""
        solve_path = task_dir / "solution" / "solve.sh"
        solve_path.write_text(
            _render(
                solve_path.read_text(encoding="utf-8"), register_new_files=register
            ).rstrip("\n")
            + "\n",
            encoding="utf-8",
        )

        solve_path.chmod(0o755)
        (task_dir / "tests" / "test.sh").chmod(0o755)
        return task_dir

    def run(self) -> None:
        """Generate Harbor tasks under ``self.output_dir``."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        instance_ids = self.select_ids()
        print(
            f"Converting {len(instance_ids)} SusVibes instances into {self.output_dir} ..."
        )

        failures: list[tuple[str, str]] = []
        for idx, instance_id in enumerate(instance_ids, 1):
            try:
                task_dir = self.generate_task(instance_id)
                print(
                    f"[{idx}/{len(instance_ids)}] OK   {instance_id} -> {task_dir.name}"
                )
            except Exception as exc:
                print(
                    f"[{idx}/{len(instance_ids)}] FAIL {instance_id}: {type(exc).__name__}: {exc}"
                )
                failures.append((instance_id, f"{type(exc).__name__}: {exc}"))

        print(
            f"Done. Success: {len(instance_ids) - len(failures)}  Failures: {len(failures)}"
        )
        if failures:
            raise SystemExit(1)
