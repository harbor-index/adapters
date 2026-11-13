"""Generate the pinned 100-case DeepPlanning Shopping levels 1–2 subset."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

CODE_URL = "https://github.com/QwenLM/Qwen-Agent.git"
CODE_REVISION = "31a4d36d123688581a9e9744427272b33ce940e0"
DATA_REVISION = "213876cce679f993a476d01042e13d111c0e3648"
DOMAIN = "benchmark/deepplanning/shoppingplanning"
BASE_IMAGE = "python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df"
ARCHIVES = {
    1: "632a3b5d0db1fa0717474b9361b2d7102aeb7e1f00c32cb842f012ff7adf8000",
    2: "58b65bff1f5e9eefb580b02202ef1394a87f8d82466a7aff5e1329a812e16d55",
}
TASKS = {
    f"shopping-level{level}-case-{case:03d}": (level, case)
    for level in (1, 2)
    for case in range(1, 51)
}
TEMPLATES = Path(__file__).parent / "task-template"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive) as bundle:
        if any(not (m.isfile() or m.isdir()) for m in bundle.getmembers()):
            raise ValueError("Source archive contains non-regular entries")
        bundle.extractall(destination, filter="data")


class DeepPlanningShoppingAdapter:
    def __init__(
        self,
        output_dir: Path,
        source_dir: Path | None = None,
        data_dir: Path | None = None,
        task_ids: list[str] | None = None,
        limit: int | None = None,
        overwrite: bool = False,
        split: str = "full",
        environment: str = "smol",
    ):
        self.output_dir = output_dir.resolve()
        self.source_dir = source_dir.resolve() if source_dir else None
        self.data_dir = (
            data_dir
            or Path.home() / ".cache/harbor-adapters/deepplanning" / DATA_REVISION
        ).resolve()
        self.overwrite = overwrite
        self.environment = environment
        if environment not in ("docker", "smol") or split not in ("full", "parity"):
            raise ValueError("Unknown environment or split")
        if limit is not None and limit < 1:
            raise ValueError("--limit must be positive")
        if task_ids is not None and (not task_ids or set(task_ids) - TASKS.keys()):
            raise ValueError("Unknown or empty --task-ids selection")
        selected = [
            t
            for t, (_, n) in TASKS.items()
            if (split == "full" or n <= 5) and (task_ids is None or t in task_ids)
        ]
        self.task_ids = selected[:limit] if limit is not None else selected
        if not self.task_ids:
            raise ValueError("No tasks selected")

    def run(self) -> list[Path]:
        for protected in (self.source_dir, self.data_dir):
            if protected and (
                self.output_dir == protected
                or self.output_dir in protected.parents
                or protected in self.output_dir.parents
            ):
                raise ValueError(
                    "Output must be separate from source and data directories"
                )
        for name in self.task_ids:
            target = self.output_dir / name
            if target.is_symlink():
                raise ValueError(f"Refusing symlink destination: {target}")
            if target.exists():
                if not self.overwrite:
                    raise FileExistsError(f"{target} exists; use --overwrite")
                marker = json.loads((target / "provenance.json").read_text())
                if (
                    marker.get("adapter") != "deepplanning-shopping"
                    or marker.get("task_id") != name
                ):
                    raise ValueError(
                        f"Refusing to replace unrelated directory: {target}"
                    )
        with tempfile.TemporaryDirectory(prefix="deepplanning-adapter-") as temp:
            work = Path(temp)
            repo = self.source_dir or work / "repo"
            if self.source_dir is None:
                subprocess.run(
                    [
                        "git",
                        "clone",
                        "--quiet",
                        "--filter=blob:none",
                        "--no-checkout",
                        CODE_URL,
                        str(repo),
                    ],
                    check=True,
                )
            source = work / "source"
            source.mkdir()
            archive = work / "source.tar"
            with archive.open("wb") as out:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "archive",
                        CODE_REVISION,
                        DOMAIN,
                        "LICENSE",
                    ],
                    stdout=out,
                    check=True,
                )
            extract(archive, source)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            for level in sorted({TASKS[t][0] for t in self.task_ids}):
                name = f"database_level{level}.tar.gz"
                path = self.data_dir / name
                if not path.exists():
                    url = f"https://huggingface.co/datasets/Qwen/DeepPlanning/resolve/{DATA_REVISION}/{name}"
                    with (
                        urllib.request.urlopen(url, timeout=60) as response,
                        path.with_suffix(".part").open("wb") as out,
                    ):
                        shutil.copyfileobj(response, out)
                    path.with_suffix(".part").replace(path)
                if sha256(path) != ARCHIVES[level]:
                    raise ValueError(f"Dataset checksum mismatch: {path}")
                extract(path, work)
            domain = source / DOMAIN
            prompts = {}
            for node in ast.parse((domain / "agent/prompts.py").read_text()).body:
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                ):
                    key = node.targets[0].id
                    if key in ("SYSTEM_PROMPT_level1", "SYSTEM_PROMPT_level2"):
                        prompts[key] = ast.literal_eval(node.value)
            env = Environment(
                loader=FileSystemLoader(TEMPLATES),
                undefined=StrictUndefined,
                keep_trailing_newline=True,
                autoescape=False,
            )
            staged = work / "staged"
            for name in self.task_ids:
                self._generate(
                    domain, source / "LICENSE", work, staged / name, name, prompts, env
                )
            self.output_dir.mkdir(parents=True, exist_ok=True)
            for name in self.task_ids:
                target = self.output_dir / name
                if target.exists():
                    shutil.rmtree(target)
                shutil.move(staged / name, target)
        return [self.output_dir / name for name in self.task_ids]

    def _generate(
        self,
        domain: Path,
        license_file: Path,
        work: Path,
        target: Path,
        name: str,
        prompts: dict,
        env: Environment,
    ) -> None:
        level, number = TASKS[name]
        case = work / f"database_level{level}" / f"case_{number}"
        gold = json.loads((case / "validation_cases.json").read_text())
        queries = json.loads(
            (domain / f"data/level_{level}_query_meta.json").read_text()
        )
        query = next(q["query"] for q in queries if str(q["id"]) == str(number))
        query_correction = query != gold["query"]
        if query_correction:
            # Dataset v1.1 specifies size 44, matching its reference products.
            # The repository's query metadata still requests size 36 in this case.
            if (level, number) != (1, 36) or query.replace(
                "size 36 items", "size 44 items"
            ) != gold["query"]:
                raise ValueError(f"Unexpected source query mismatch for {name}")
            query = gold["query"]
        context = {
            "task_id": name,
            "level": level,
            "query": query,
            "system_prompt": prompts[f"SYSTEM_PROMPT_level{level}"],
            "base_image": BASE_IMAGE,
            "smol": self.environment == "smol",
        }
        for path in TEMPLATES.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(TEMPLATES)
            if self.environment == "smol" and path.name == "Dockerfile":
                continue  # Prebuilt-image tasks use Harbor's loose-file upload.
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix in (".toml", ".md") or path.name == "Dockerfile":
                out.write_text(env.get_template(str(rel)).render(**context))
            else:
                shutil.copy2(path, out)
        state = target / "environment/state"
        state.mkdir()
        for filename in ("products.jsonl", "user_info.json", "cart.json"):
            shutil.copy2(case / filename, state / filename)
        shutil.copytree(
            domain / "tools",
            target / "environment/tools",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        native = target / "environment/native_agent/agent"
        native.mkdir(parents=True)
        for filename in ("shopping_agent.py", "call_llm.py"):
            shutil.copy2(domain / "agent" / filename, native / filename)
        (target / "environment/prompt.json").write_text(
            json.dumps(
                {"system": context["system_prompt"], "query": query}, ensure_ascii=False
            )
            + "\n"
        )
        shutil.copy2(license_file, target / "environment/UPSTREAM_LICENSE")
        shutil.copy2(license_file, target / "tests/UPSTREAM_LICENSE")
        shutil.copy2(
            domain / "evaluation/evaluation_pipeline.py",
            target / "tests/evaluation_pipeline.py",
        )
        for role in ("tests", "solution"):
            shutil.copy2(
                case / "validation_cases.json", target / role / "reference.json"
            )
        hashes = {
            str(p.relative_to(domain)): sha256(p)
            for p in sorted((domain / "tools").rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        }
        hashes["evaluation/evaluation_pipeline.py"] = sha256(
            domain / "evaluation/evaluation_pipeline.py"
        )
        native_hashes = {
            f"agent/{filename}": sha256(domain / "agent" / filename)
            for filename in ("shopping_agent.py", "call_llm.py")
        }
        hashes.update(native_hashes)
        (native.parent / "source.json").write_text(
            json.dumps({"revision": CODE_REVISION, "sha256": native_hashes}, indent=2)
            + "\n"
        )
        (target / "provenance.json").write_text(
            json.dumps(
                {
                    "adapter": "deepplanning-shopping",
                    "task_id": name,
                    "code_repository": CODE_URL,
                    "code_revision": CODE_REVISION,
                    "dataset": "Qwen/DeepPlanning",
                    "dataset_revision": DATA_REVISION,
                    "archive_sha256": ARCHIVES[level],
                    "level": level,
                    "case": number,
                    "environment": self.environment,
                    "query_source": "dataset-v1.1"
                    if query_correction
                    else "repository-and-dataset",
                    "source_sha256": hashes,
                },
                indent=2,
            )
            + "\n"
        )
        (target / "NOTICE.md").write_text(
            "DeepPlanning, Qwen Team. Original code/data: Apache 2.0.\n"
            f"Source: {CODE_URL.removesuffix('.git')}/tree/{CODE_REVISION}/{DOMAIN}\n"
            f"Dataset: https://huggingface.co/datasets/Qwen/DeepPlanning/tree/{DATA_REVISION}\n"
            "Adapter additions: Harbor packaging, CLI tool transport, reference replay, "
            "isolated verification and provenance. Original prompts, tools and scoring preserved.\n"
        )
