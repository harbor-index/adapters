"""Generate Harbor tasks from FutureHouse's ether0-benchmark (test split).

Each upstream problem becomes one Harbor task. The agent receives ether0's
official answer-format instruction followed by the problem text (the same
prompt construction as ether0's published baseline) and writes its final
response to ``/app/answer.txt``. A separate verifier container scores that
response with ether0's own reward code (``extract_answer_loose`` +
``EVAL_FUNCTIONS`` with ``test=True``), pre-installed in the verifier image
with ether0's locked dependency versions.

Only problems whose reference answer ships with the dataset are adapted; see
``ADAPTED_REWARD_FUNCTIONS``. The README documents the excluded problems.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download

HF_REPO_ID = "futurehouse/ether0-benchmark"
HF_REVISION = "c7d5e59960087f360bc32a5006bb994324b38c35"
HF_PARQUET = "data/test-00000-of-00001.parquet"

# Verbatim copy of ether0.model_prompts.LOOSE_XML_ANSWER_USER_PROMPT at commit
# b77e0d8eb06f62c8528da72bff22d4c7fee11371, which ether0's published baseline
# prepends to every problem.
ANSWER_FORMAT_PROMPT = (
    "When answering,"
    " be sure to place the final answer as"
    " SMILES notation into XML tags <answer></answer>."
    " An example is <answer>CCO</answer>."
)

# Reward functions whose reference answer is shipped with the dataset:
# - exact-match style checks keep the reference in the `solution` field;
# - constraint checks accept the dataset's `ideal` molecule.
ANSWER_FROM_SOLUTION = frozenset({"str_eval", "product_eval"})
ANSWER_FROM_IDEAL = frozenset({"formula_eval", "functional_group_eval"})
ADAPTED_REWARD_FUNCTIONS = ANSWER_FROM_SOLUTION | ANSWER_FROM_IDEAL

SOLUTION_SEPARATOR = "!:!"
_SAFE_ID = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True)
class Ether0Problem:
    upstream_id: str
    problem: str
    solution: str
    reward_function: str
    problem_type: str
    reference_answer: str

    @property
    def task_id(self) -> str:
        """Stable, registry-safe task id derived from the upstream UUID."""
        return _SAFE_ID.sub("-", self.upstream_id.lower()).strip("-")

    @property
    def category(self) -> str:
        return self.problem_type.split("/")[0]


def _toml_string(value: str) -> str:
    """Render a TOML basic string (JSON escaping is a valid subset)."""
    return json.dumps(value, ensure_ascii=False)


def load_problems(parquet_path: Path | None = None) -> list[Ether0Problem]:
    """Load the pinned test split and keep the problems with shipped references."""
    if parquet_path is None:
        try:
            parquet_path = Path(
                hf_hub_download(
                    repo_id=HF_REPO_ID,
                    filename=HF_PARQUET,
                    repo_type="dataset",
                    revision=HF_REVISION,
                )
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not download {HF_PARQUET} from {HF_REPO_ID}@{HF_REVISION}: {exc}. "
                "Check access to Hugging Face, or pass --parquet-path."
            ) from exc
    try:
        rows = pq.read_table(parquet_path).to_pylist()
    except (OSError, ValueError) as exc:  # pyarrow raises ArrowInvalid, a ValueError
        raise RuntimeError(f"Could not read the test split from {parquet_path}: {exc}") from exc
    problems: list[Ether0Problem] = []
    for row in rows:
        reward_function, answer_info, problem_type = row["solution"].split(
            SOLUTION_SEPARATOR
        )
        if reward_function not in ADAPTED_REWARD_FUNCTIONS:
            continue
        reference = (
            answer_info if reward_function in ANSWER_FROM_SOLUTION else row["ideal"]
        )
        if not reference:
            raise ValueError(f"Problem {row['id']} has no reference answer.")
        problems.append(
            Ether0Problem(
                upstream_id=row["id"],
                problem=row["problem"],
                solution=row["solution"],
                reward_function=reward_function,
                problem_type=problem_type,
                reference_answer=reference,
            )
        )
    problems.sort(key=lambda p: p.task_id)
    ids = [p.task_id for p in problems]
    if len(ids) != len(set(ids)):
        raise ValueError("Task ids are not unique after sanitization.")
    return problems


class Ether0BenchmarkAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        parquet_path: Path | None = None,
        docker_image: str | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = set(task_ids) if task_ids else None
        self.parquet_path = parquet_path
        # Optional prebuilt image built from task-template/environment/Dockerfile.
        # When set, Harbor runs it instead of building the Dockerfile per task.
        self.docker_image = docker_image
        self.template_dir = Path(__file__).parent / "task-template"

    def run(self) -> None:
        problems = load_problems(self.parquet_path)
        if self.task_ids is not None:
            problems = [p for p in problems if p.task_id in self.task_ids]
            missing = self.task_ids - {p.task_id for p in problems}
            if missing:
                raise ValueError(f"Unknown or excluded task ids: {sorted(missing)}")
        if self.limit is not None:
            problems = problems[: self.limit]

        self.output_dir.mkdir(parents=True, exist_ok=True)
        written = skipped = 0
        for problem in problems:
            task_dir = self.output_dir / problem.task_id
            if task_dir.exists():
                if not self.overwrite:
                    skipped += 1
                    continue
                shutil.rmtree(task_dir)
            self._write_task(problem, task_dir)
            written += 1
        print(
            f"Wrote {written} tasks to {self.output_dir}"
            + (f" (skipped {skipped} existing; use --overwrite)" if skipped else "")
        )

    def _write_task(self, problem: Ether0Problem, task_dir: Path) -> None:
        shutil.copytree(self.template_dir, task_dir)

        instruction = (task_dir / "instruction.md").read_text()
        instruction = instruction.replace("{{ANSWER_FORMAT_PROMPT}}", ANSWER_FORMAT_PROMPT)
        instruction = instruction.replace("{{PROBLEM}}", problem.problem.strip())
        (task_dir / "instruction.md").write_text(instruction)

        task_toml = (task_dir / "task.toml").read_text()
        for key, value in {
            # task_id is already restricted to [a-z0-9-], so it is safe inside the
            # quoted name literal in the template.
            "{{TASK_ID}}": problem.task_id,
            "{{CATEGORY}}": _toml_string(problem.category),
            "{{PROBLEM_TYPE}}": _toml_string(problem.problem_type),
            "{{UPSTREAM_ID}}": _toml_string(problem.upstream_id),
            "{{DOCKER_IMAGE_LINE}}": (
                f"docker_image = {_toml_string(self.docker_image)}"
                if self.docker_image
                else ""
            ),
        }.items():
            task_toml = task_toml.replace(key, value)
        (task_dir / "task.toml").write_text(task_toml)

        solve = (task_dir / "solution" / "solve.sh").read_text()
        solve = solve.replace("{{REFERENCE_ANSWER}}", problem.reference_answer)
        (task_dir / "solution" / "solve.sh").write_text(solve)

        config = {
            "upstream_id": problem.upstream_id,
            "problem_type": problem.problem_type,
            "solution": problem.solution,
        }
        (task_dir / "tests" / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        )

        for script in ("solution/solve.sh", "tests/test.sh"):
            (task_dir / script).chmod(0o755)
