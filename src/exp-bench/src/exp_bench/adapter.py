"""Generate Harbor tasks from EXP-Bench (arXiv:2505.24785).

Each EXP-Bench task asks the agent to answer a research question from a real ICLR /
NeurIPS 2024 paper by designing, implementing and running an experiment inside the
paper's code repository, with the README and the task's key source files masked.
The agent prompt is the upstream ``eval_gen_prompt.txt`` filled exactly as upstream
``evaluation/eval.py:generate_task_prompt`` fills it for OpenHands / Inspect agents.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "task-template"
DEFAULT_DATA_FILE = PACKAGE_DIR / "data" / "exp_bench_tasks.json.gz"
PARITY_SPLIT_FILE = PACKAGE_DIR / "data" / "parity_task_ids.txt"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "harbor-exp-bench-adapter"

# Upstream eval.py values for OpenHands / Inspect agents.
OUTPUT_SCRIPT_NAME = "/workspace/reproduce_exp_bench.sh"
ADDITIONAL_INFO = (
    "The code repo is available in /workspace. "
    "LLM related credentials (if needed) are available in /workspace/setup_apis_exp/"
)
ITERATION = 1


def strip_workspace_prefix(path: str) -> str:
    """Upstream eval.py:strip_workspace_prefix (repo-relative path of a masked file)."""
    if path.startswith("/workspace/"):
        return path[len("/workspace/") :]
    if path.startswith("/tmp/"):
        return path[len("/tmp/") :]
    if path.startswith("/"):
        return path[1:]
    return path


def format_hours(hours: float) -> str:
    """Render the duration like upstream's JSON config values (0.5 -> '0.5', 1.0 -> '1')."""
    return f"{hours:g}"


def toml_escape(value: str) -> str:
    return " ".join(str(value).split()).replace("\\", "\\\\").replace('"', '\\"')


def render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def github_raw_path(path: str) -> str:
    """Path handling of upstream helper/utils.py:get_file_from_github."""
    if path.startswith("/workspace"):
        path = path[len("/workspace") :]
    return path.lstrip("/")


def fetch_source(repo: str, commit: str, path: str, expected_sha256: str, cache_dir: Path) -> str:
    """Download one ground-truth source file at the pinned commit, verifying its hash."""
    rel = github_raw_path(path)
    cached = cache_dir / repo / commit / rel
    if cached.exists():
        data = cached.read_bytes()
    else:
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/{rel}"
        data = None
        for attempt in range(5):
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    data = resp.read()
                break
            except (urllib.error.URLError, TimeoutError) as e:
                if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                    raise FileNotFoundError(url) from e
                time.sleep(3 * (attempt + 1))
        if data is None:
            raise RuntimeError(f"Could not download {url}")
    text = data.decode("utf-8", errors="replace")
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"sha256 mismatch for {repo}@{commit}:{rel}")
    if not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    return text


class EXPBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        data_file: Path | None = None,
        split: str = "full",
        agent_timeout_hours: float = 0.5,
        cpus: int = 4,
        memory_mb: int = 16384,
        storage_mb: int = 51200,
        gpus: int = 0,
        cache_dir: Path | None = None,
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.data_file = Path(data_file) if data_file else DEFAULT_DATA_FILE
        self.split = split
        self.agent_timeout_hours = agent_timeout_hours
        self.cpus = cpus
        self.memory_mb = memory_mb
        self.storage_mb = storage_mb
        self.gpus = gpus
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self.eval_gen_prompt = (PACKAGE_DIR / "prompts" / "eval_gen_prompt.txt").read_text()

    # ------------------------------------------------------------------ data

    @staticmethod
    def invalid_reason(task: dict) -> str | None:
        """Tasks the upstream harness marks ``invalid_task`` (eval.py:mask_repo fails).

        Upstream masks the README first and then each listed source file; if a listed
        file does not exist in the repository, masking fails and the task is skipped.
        """
        missing = [path for path, sha in task["setup_script_sha256"].items() if sha is None]
        if missing:
            return f"masked source file(s) not in the repository at the pinned commit: {missing}"
        if "README.md" in task["masked_files"]:
            return "masks README.md, which upstream already removed before masking sources"
        return None

    def load_tasks(self) -> list[dict]:
        if not self.data_file.exists():
            raise FileNotFoundError(
                f"EXP-Bench task bundle not found at {self.data_file}. "
                "Pass --data-file or build it with scripts/build_task_bundle.py."
            )
        if self.data_file.suffix == ".gz":
            with gzip.open(self.data_file, "rt", encoding="utf-8") as f:
                bundle = json.load(f)
        else:
            bundle = json.loads(self.data_file.read_text())
        tasks = [t for t in bundle["tasks"] if self.invalid_reason(t) is None]
        if self.split == "parity":
            wanted = [
                line.strip()
                for line in PARITY_SPLIT_FILE.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            ]
            by_id = {t["task_id"]: t for t in tasks}
            tasks = [by_id[i] for i in wanted]
        if self.task_ids:
            wanted_ids = set(self.task_ids)
            tasks = [t for t in tasks if t["task_id"] in wanted_ids]
            missing = wanted_ids - {t["task_id"] for t in tasks}
            if missing:
                raise ValueError(f"Unknown task ids: {sorted(missing)}")
        if self.limit is not None:
            tasks = tasks[: max(0, self.limit)]
        return tasks

    # ------------------------------------------------------------------ prompt

    def output_filename(self, task: dict) -> str:
        """eval.py:get_output_eval_gen_filename (without the leading slash)."""
        hours = format_hours(self.agent_timeout_hours)
        return f"{task['paper_id']}_task_index_{task['task_index']}_iter_{ITERATION}_duration_{hours}_eval_gen.json"

    def instruction(self, task: dict) -> str:
        data = task["task_data"]
        question = data["question"] if "question" in data else data["hypothesis"]
        return self.eval_gen_prompt.format(
            question=question,
            method=data["method"],
            agent_instructions=data.get("agent_instructions", ""),
            output_json_path="/workspace/" + self.output_filename(task),
            output_script_name=OUTPUT_SCRIPT_NAME,
            additional_info=ADDITIONAL_INFO,
        )

    # ------------------------------------------------------------------ write

    def generate_task(self, task: dict) -> Path:
        task_dir = self.output_dir / task["task_id"]
        if task_dir.exists():
            if not self.overwrite:
                return task_dir
            shutil.rmtree(task_dir)
        shutil.copytree(TEMPLATE_DIR, task_dir)

        agent_timeout_sec = int(round(self.agent_timeout_hours * 3600))
        values = {
            "task_id": task["task_id"],
            "paper_title": toml_escape(task["paper_title"]),
            "conference": task["conference"],
            "paper_id": str(task["paper_id"]),
            "task_index": str(task["task_index"]),
            "task_type": str(task["task_type"]),
            "repo": task["repo"],
            "commit": task["commit"],
            "agent_timeout_sec": f"{float(agent_timeout_sec):.1f}",
            "verifier_timeout_sec": "5400.0",
            "cpus": str(self.cpus),
            "memory_mb": str(self.memory_mb),
            "storage_mb": str(self.storage_mb),
            "gpus": str(self.gpus),
        }
        toml_path = task_dir / "task.toml"
        toml_path.write_text(render(toml_path.read_text(), values))

        (task_dir / "instruction.md").write_text(self.instruction(task) + "\n")

        prep = task_dir / "environment" / "prepare_workspace.sh"
        masked = "\n".join(f"    {json.dumps(path)}" for path in task["masked_files"])
        prep.write_text(
            render(
                prep.read_text(),
                {
                    "repo_url": f"https://github.com/{task['repo']}.git",
                    "commit": task["commit"],
                    "masked_files": masked,
                },
            )
        )

        output_filename = self.output_filename(task)
        setup_scripts = {
            path: fetch_source(task["repo"], task["commit"], path, sha, self.cache_dir)
            for path, sha in task["setup_script_sha256"].items()
        }
        spec = {
            "task_id": task["task_id"],
            "paper_id": str(task["paper_id"]),
            "task_index": task["task_index"],
            "task_type": task["task_type"],
            "github_url": task["github_url"],
            "repo": task["repo"],
            "commit": task["commit"],
            "output_filename": output_filename,
            "setup_script_paths": task["setup_script_paths"],
            "setup_scripts": setup_scripts,
            "task_data": task["task_data"],
        }
        (task_dir / "tests" / "task_data.json").write_text(
            json.dumps(spec, indent=1, ensure_ascii=False) + "\n"
        )

        solution_dir = task_dir / "solution"
        solve = solution_dir / "solve.sh"
        solve.write_text(render(solve.read_text(), {"output_filename": output_filename}))
        for rel, content in setup_scripts.items():
            target = solution_dir / "oracle_files" / strip_workspace_prefix(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        answer = {
            "design": task["task_data"]["design_complexity"],
            "conclusion": task["task_data"]["expected_outcome"],
        }
        (solution_dir / "oracle_answer.json").write_text(
            json.dumps(answer, indent=2, ensure_ascii=False) + "\n"
        )

        for script in (solve, task_dir / "tests" / "test.sh", prep):
            script.chmod(0o755)
        return task_dir

    def run(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        tasks = self.load_tasks()
        for task in tasks:
            self.generate_task(task)
        print(f"Generated {len(tasks)} EXP-Bench tasks in {self.output_dir}")
