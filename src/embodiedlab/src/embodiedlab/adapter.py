"""Generate Harbor tasks from an EmbodiedLab source tree.

One task per experiment: the T0 interface level and one public dev instance (the instance the oracle replay was
recorded on). The oracle replays are not public: they are passed in with --oracle-bundle or --oracle-dir, and without
them the tasks are generated with a solution that only says so. The tasks share one prebuilt image (Isaac Sim, the EmbodiedLab environment runtime and benches, the
tool gateway); this module also writes that image's build context, which holds every task's instance record and
settings, readable by root only inside the image.

Everything about an experiment comes from the source tree: the experiment list and table data from the two task
tables of its README, the instance from its own derivation (benchmark.instances.service), the agent prompt and the
workspace texts from scripts/trial.py, the offline evaluator from its benchmark package.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
TEMPLATE = PACKAGE / "task-template"
IMAGE = PACKAGE / "image"
ORACLE_INDEX = PACKAGE / "oracle.json"

SOURCE_REPO = "https://github.com/HexaFuture/EmbodiedLab.git"
SOURCE_REF = "v0.2.1"
WORKSPACE = "/home/agent/workspace"
EXPOSURE = "T0"
# [task].authors: the first authors of the EmbodiedLab paper.
AUTHORS = ["Hongcheng Gao", "Zelin Zheng"]

# The parity subset (provisional, to be agreed with the Harbor team): the ten adapted determination experiments with
# the shortest wall-clock limits, with the shortest thermal one swapped in so that every discipline appears.
PARITY_SUBSET = (
    "hooke_k", "pendulum_g", "incline_friction", "projectile_landing", "beam_compliance",
    "battery_emf", "grating_wavelength", "slit_width", "thermistor_beta", "spectro_basic",
)

# Optimization experiments whose reference optimum the evaluator would otherwise compute while scoring (up to about
# half an hour on 48 cores for the filter, too long for a verifier). It is computed once per instance here, with the
# evaluator's own prewarm entry point, and shipped to the verifier (tests/el/reference_cache/; see tests/score.py).
PREWARM = {
    "active_filter": ("benchmark.evaluation.filter", "EMBODIEDLAB_FILTER_ASTAR_CACHE", "filter_astar.json"),
    "truss_bridge": ("benchmark.evaluation.truss", "EMBODIEDLAB_TRUSS_PSTAR_CACHE", "truss_pstar.json"),
    "vibration_absorber": ("benchmark.evaluation.absorber", "EMBODIEDLAB_ABSORBER_ASTAR_CACHE", "absorber_astar.json"),
}


def task_name(exp: str) -> str:
    return exp.replace("_", "-")


def number(value) -> str:
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def substitute(text: str, values: dict) -> str:
    for key, value in values.items():
        text = text.replace(f"@@{key}@@", str(value))
    left = re.findall(r"@@[A-Z_]+@@", text)
    if left:
        raise ValueError(f"unfilled template placeholders {left}")
    return text


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def readme_table(src: Path) -> dict[str, dict]:
    """The README's two task tables: measurement rows (# | id | Experiment | Discipline | Dimension | Tier | Tolerance |
    Sim budget | Wall limit) and optimization rows (# | id | Experiment | Discipline | Success criterion | Sim budget |
    Wall limit)."""
    rows = {}
    for line in (src / "README.md").read_text().splitlines():
        m = re.match(r"\|\s*(\d+)\s*\|\s*`([a-z0-9_]+)`\s*\|(.*)\|\s*$", line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(3).split("|")]
        if len(cells) == 7:
            row = {"title": cells[0], "discipline": cells[1], "dimension": cells[2], "tier": cells[3],
                   "tolerance": cells[4]}
        elif len(cells) == 5:
            row = {"title": cells[0], "discipline": cells[1], "dimension": "Optimization", "tier": "optimization",
                   "tolerance": cells[2]}
        else:
            raise ValueError(f"unexpected README task row: {line}")
        rows[m.group(2)] = {"number": int(m.group(1)), **row}
    if len(rows) != 35:
        raise ValueError(f"expected 35 experiments in the README task tables, found {len(rows)}")
    return rows


class EmbodiedLabAdapter:
    def __init__(self, output_dir: Path, limit: int | None = None, overwrite: bool = False,
                 task_ids: list[str] | None = None, split: str = "full", source_dir: Path | None = None,
                 source_ref: str = SOURCE_REF, oracle_dir: Path | None = None, image: str | None = None,
                 image_context: Path | None = None, cache_dir: Path | None = None, all_experiments: bool = False,
                 oracle_bundle: str | None = None):
        self.output_dir = Path(output_dir).resolve()
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split
        self.source_dir = Path(source_dir).resolve() if source_dir else None
        self.source_ref = source_ref
        self.oracle_dir = Path(oracle_dir).resolve() if oracle_dir else None
        self.oracle_bundle = oracle_bundle or os.environ.get("EMBODIEDLAB_ORACLE_BUNDLE") or None
        self.image = image
        self.image_context = (Path(image_context) if image_context
                              else self.output_dir.parent / f"{self.output_dir.name}-image").resolve()
        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "harbor-embodiedlab").resolve()
        self.all_experiments = all_experiments
        self.index = json.loads(ORACLE_INDEX.read_text())

    # -- inputs --------------------------------------------------------------------------------------------------

    def _source(self) -> Path:
        if self.source_dir:
            return self.source_dir
        target = self.cache_dir / f"EmbodiedLab-{self.source_ref}"
        if not (target / "README.md").is_file():
            shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--depth", "1", "--branch", self.source_ref, SOURCE_REPO, str(target)],
                           check=True)
        return target

    def _oracle_files(self) -> Path | None:
        """The directory holding <experiment>.jsonl.xz, the recorded runs the oracle replays, or None.

        The recorded runs are not public (they end in the submitted answer of the task's instance). They come from
        --oracle-dir, or from the bundle file named in oracle.json given with --oracle-bundle (a local path or a
        private https link), whose checksum must match oracle.json.
        """
        if self.oracle_dir:
            return self.oracle_dir
        if not self.oracle_bundle:
            return None
        bundle = self.index["bundle"]
        # one directory per bundle: an updated bundle of the same version is extracted afresh
        target = self.cache_dir / f"oracle-{self.index['benchmark_version']}-{bundle['sha256'][:12]}"
        if not target.is_dir():
            archive = self.cache_dir / bundle["name"]
            archive.parent.mkdir(parents=True, exist_ok=True)
            if not archive.is_file() or sha256(archive) != bundle["sha256"]:
                if re.match(r"https://", self.oracle_bundle):
                    urllib.request.urlretrieve(self.oracle_bundle, archive)
                else:
                    shutil.copy(Path(self.oracle_bundle).expanduser(), archive)
            if sha256(archive) != bundle["sha256"]:
                raise ValueError(f"the oracle bundle {self.oracle_bundle} does not match the checksum in oracle.json")
            staging = Path(tempfile.mkdtemp(dir=self.cache_dir))
            with tarfile.open(archive) as tar:
                tar.extractall(staging, filter="data")
            staging.rename(target)
        return target

    @staticmethod
    def _load_trial_module(src: Path):
        for path in (str(src / "sdk" / "src"), str(src)):
            if path not in sys.path:
                sys.path.insert(0, path)
        spec = importlib.util.spec_from_file_location("el_trial", src / "scripts" / "trial.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _version(src: Path) -> str:
        text = (src / "sdk" / "src" / "embodiedlab_api" / "__init__.py").read_text()
        return re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)

    # -- selection -----------------------------------------------------------------------------------------------

    def _adapted(self, table: dict) -> list[str]:
        """The experiments of the adapted benchmark, in table order: those with a validated oracle replay (all 35
        with --all-experiments)."""
        ordered = sorted(table, key=lambda e: table[e]["number"])
        if self.all_experiments:
            return ordered
        return [e for e in ordered if e in self.index["experiments"]]

    def _selected(self, adapted: list[str]) -> list[str]:
        chosen = [e for e in adapted if e in PARITY_SUBSET] if self.split == "parity" else list(adapted)
        if self.task_ids:
            wanted = {t.split("/")[-1].replace("-", "_") for t in self.task_ids}
            unknown = wanted - set(adapted)
            if unknown:
                raise ValueError(f"unknown or excluded task ids: {sorted(unknown)}")
            chosen = [e for e in chosen if e in wanted]
        return chosen[: self.limit] if self.limit is not None else chosen

    def _seed(self, exp: str) -> int:
        return int(self.index["experiments"].get(exp, {}).get("seed", 0))

    # -- generation ----------------------------------------------------------------------------------------------

    def run(self) -> list[Path]:
        src = self._source()
        version = self._version(src)
        if version != self.index["benchmark_version"]:
            raise ValueError(f"the source tree is EmbodiedLab {version}, but the oracle replays were validated on "
                             f"{self.index['benchmark_version']}")
        image = self.image or f"embodiedlab/harbor:{version}"
        trial = self._load_trial_module(src)
        table = readme_table(src)
        adapted = self._adapted(table)
        selected = self._selected(adapted)
        oracle = self._oracle_files()

        from benchmark.instances.service import derive, materialize

        made = []
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._image_context(src, trial)
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            records = {}
            for exp in adapted:
                instance = derive(exp, "dev", self._seed(exp))
                entry = self.index["experiments"].get(exp)
                if entry and entry["instance_id"] != instance.instance_id:
                    raise ValueError(f"{exp}: the source tree derives {instance.instance_id} for dev seed "
                                     f"{entry['seed']}, but the oracle was recorded on {entry['instance_id']}")
                materialize(instance, store)
                records[exp] = (instance, store / f"{instance.instance_id}.json")
                self._private_files(exp, instance, records[exp][1], table[exp], version, image)
            for exp in selected:
                task = self.output_dir / task_name(exp)
                if task.exists():
                    if not self.overwrite:
                        continue
                    shutil.rmtree(task)
                instance, record = records[exp]
                self._make_task(src, trial, task, exp, instance, record, table[exp], version, image, oracle)
                made.append(task)
        return made

    def _values(self, exp: str, instance, meta: dict, version: str, image: str) -> dict:
        from benchmark.registry.loader import load

        spec = load(exp)
        sim_s, wall_s = spec.budget["sim_s"], float(spec.budget.get("wall_s") or 10800)
        return {"TASK_NAME": task_name(exp), "EXP_ID": exp, "SEED": self._seed(exp), "EXPOSURE": EXPOSURE,
                "INSTANCE_ID": instance.instance_id, "SIM_S": number(sim_s), "WALL_S": f"{wall_s:.1f}",
                "MAX_STEPS": int(float(sim_s) * 30), "NUMBER": meta["number"],
                "TITLE": meta["title"].replace('"', "'"), "DISCIPLINE": meta["discipline"],
                "DISCIPLINE_KW": meta["discipline"].lower(), "DIMENSION": meta["dimension"], "TIER": meta["tier"],
                "TOLERANCE": meta["tolerance"].replace('"', "'"), "IMAGE": image, "BENCHMARK_VERSION": version,
                "AUTHORS": ", ".join(f'{{ name = "{author}" }}' for author in AUTHORS)}

    def _image_context(self, src: Path, trial) -> None:
        """The shared image's build context: Dockerfile + el-harbor/ (start script, gateway, workspace renderer and
        its texts from trial.py, the agent client). _private_files adds each task's instance record and settings."""
        target = self.image_context
        if target.exists():
            shutil.rmtree(target)
        el = target / "el-harbor"
        (el / "tasks").mkdir(parents=True)
        shutil.copy(IMAGE / "Dockerfile", target / "Dockerfile")
        for name in ("up.sh", "gateway.py", "render.py"):
            shutil.copy(IMAGE / name, el / name)
        (el / "up.sh").chmod(0o755)
        texts = {"PROMPT": trial.PROMPT, "README": trial.README, "EXPOSURE_NOTES": trial.EXPOSURE_NOTES,
                 "MANIPULATION_INTRO": trial.MANIPULATION_INTRO, "FINGER_MOTION": trial.FINGER_MOTION,
                 "ROBOT_NOTE": trial.ROBOT_NOTE}
        (el / "templates.json").write_text(json.dumps(texts, indent=1, ensure_ascii=False))
        shutil.copytree(src / "sdk" / "src" / "embodiedlab_api", el / "embodiedlab_api",
                        ignore=shutil.ignore_patterns("__pycache__"))

    def _private_files(self, exp: str, instance, record: Path, meta: dict, version: str, image: str) -> None:
        values = self._values(exp, instance, meta, version, image)
        private = self.image_context / "el-harbor" / "tasks" / task_name(exp)
        private.mkdir(parents=True)
        shutil.copy(record, private / "instance.json")
        settings = {
            # the EmbodiedLab environment daemon, configured as scripts/trial.py's Worker configures it
            "EMBODIEDLAB_ENV_ID": "env_harbor", "EMBODIEDLAB_ENV_PROFILE": "embodiedlab",
            "EMBODIEDLAB_ENV_BOOTSTRAP": "embodiedlab_sdk.robot.embodiedlab.bootstrap:bootstrap",
            "EMBODIEDLAB_BENCH_ISAAC_PYTHON": "/isaac-sim/python.sh", "EMBODIEDLAB_ENVD_URL": "http://127.0.0.1:18731",
            "EMBODIEDLAB_SEED": "0", "EMBODIEDLAB_TASK_ID": exp, "EMBODIEDLAB_INSTANCE_ID": values["INSTANCE_ID"],
            "EMBODIEDLAB_EXPOSURE": EXPOSURE, "EMBODIEDLAB_MAX_STEPS": str(values["MAX_STEPS"]),
            "EMBODIEDLAB_VIEWER": "off", "EMBODIEDLAB_CAPTURE_DIR": "/opt/embodiedlab/capture",
            "EMBODIEDLAB_RECORD_VIDEO": "1",
            # the tool gateway
            "SIM_UDS": "/run/el/envd.sock", "GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": "18731",
            "AGENT_CONTROLLER_URL": "http://127.0.0.1:18731", "EL_WORKSPACE": WORKSPACE, "EL_AGENT_USER": "agent",
            "EL_EXP_ID": exp, "EL_EXPOSURE": EXPOSURE, "EL_SIM_S": values["SIM_S"], "EL_WALL_S": values["WALL_S"],
        }
        (private / "el.env").write_text("".join(f"{key}='{value}'\n" for key, value in settings.items()))

    def _prewarm(self, src: Path, exp: str, record: Path, instance_id: str) -> Path | None:
        """Compute (or reuse) the reference optimum of this instance; return the cache file to ship."""
        if exp not in PREWARM:
            return None
        module, variable, name = PREWARM[exp]
        cache = self.cache_dir / "reference" / instance_id / name
        cache.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, **{variable: str(cache)})
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(src), env.get("PYTHONPATH")]))
        print(f"{exp}: reference optimum of {instance_id} (cached in {cache})", file=sys.stderr, flush=True)
        subprocess.run([sys.executable, "-m", module, "prewarm", str(record)], cwd=src, env=env, check=True)
        return cache

    def _make_task(self, src: Path, trial, task: Path, exp: str, instance, record: Path, meta: dict, version: str,
                   image: str, oracle: Path | None) -> None:
        values = self._values(exp, instance, meta, version, image)
        shutil.copytree(TEMPLATE, task, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("task.toml", "environment/Dockerfile"):
            (task / name).write_text(substitute((task / name).read_text(), values))
        prompt = trial.PROMPT.format(workspace=WORKSPACE, exposure=EXPOSURE,
                                     exposure_note=trial.EXPOSURE_NOTES[EXPOSURE])
        (task / "instruction.md").write_text((task / "instruction.md").read_text().replace("@@PROMPT@@", prompt))
        # the oracle: the recorded run of this instance
        entry = self.index["experiments"].get(exp)
        if entry and oracle is not None:
            calls = oracle / f"{exp}.jsonl.xz"
            if sha256(calls) != entry["sha256"]:
                raise ValueError(f"checksum mismatch for the oracle replay {calls}")
            shutil.copy(calls, task / "solution" / "calls.jsonl.xz")
        else:
            why = ("The oracle replay of this task is not public; regenerate the task with --oracle-bundle."
                   if entry else "No oracle replay exists for this experiment.")
            (task / "solution" / "solve.sh").write_text(f"#!/bin/bash\necho '{why}' >&2\nexit 1\n")
            (task / "solution" / "replay.py").unlink()
        # the verifier: the offline evaluator, the instance record and the reference optimum where one is needed
        el = task / "tests" / "el"
        el.mkdir()
        for package in ("benchmark", "experiments"):
            shutil.copytree(src / package, el / package, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(record, el / "instance.json")
        cache = self._prewarm(src, exp, record, instance.instance_id)
        if cache is not None:
            (el / "reference_cache").mkdir()
            shutil.copy(cache, el / "reference_cache" / cache.name)
        for script in ("solution/solve.sh", "tests/test.sh"):
            (task / script).chmod(0o755)
