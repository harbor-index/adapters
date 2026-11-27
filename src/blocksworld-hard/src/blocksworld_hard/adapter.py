"""Adapt upstream PDDL prompts, reference plans, and VAL grading."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

UPSTREAM_URL = "https://github.com/karthikv792/LLMs-Planning.git"
UPSTREAM_REVISION = "fc638a1aff7df3fe7a1a1d289fa2c04cc24dc284"
TEMPLATE = Path(__file__).parent / "task-template"
PROMPT = "The following is a PDDL specification of a planning problem. The first part, under the heading [DOMAIN], is the domain file. The second part, under the heading [QUERY PROBLEM], is the problem file. Using this information, which is correct, and no further assumptions, find a plan which, when run from the specified initial state, satisfies the specified goal. Provide your answer as a sequence of actions in PDDL format. An action ACTION which acts on two objects OBJ1 and OBJ2 would be written (ACTION OBJ1 OBJ2). Do not provide anything else in your answer.\n"


class BlocksworldHardAdapter:
    def __init__(
        self,
        output_dir: Path,
        source_dir: Path | None = None,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "full",
    ):
        self.output_dir = output_dir
        self.source_dir = source_dir
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split

    def run(self):
        with tempfile.TemporaryDirectory(prefix="blocksworld-hard-") as temp:
            source = self.source_dir or Path(temp) / "upstream"
            if self.source_dir is None:
                subprocess.run(
                    ["git", "clone", "--quiet", UPSTREAM_URL, str(source)], check=True
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(source),
                        "checkout",
                        "--quiet",
                        UPSTREAM_REVISION,
                    ],
                    check=True,
                )
            revision = subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            if revision != UPSTREAM_REVISION:
                raise ValueError(
                    f"Expected upstream {UPSTREAM_REVISION}, got {revision}"
                )
            self.generate(source)

    def generate(self, source):
        data = source / "llm_planning_analysis/instances/blocksworld_hard"
        domain = (data / "generated_domain.pddl").read_text()
        solutions = json.loads((data / "info.json").read_text())
        ids = [f"blocksworld-hard-{i:03d}" for i in range(1, 111)]
        if self.split == "parity":
            ids = ids[::10]
        if self.task_ids is not None:
            unknown = set(self.task_ids) - set(ids)
            if unknown:
                raise ValueError(f"Unknown task IDs in {self.split}: {sorted(unknown)}")
            ids = [i for i in ids if i in self.task_ids]
        if self.limit is not None:
            if self.limit < 1:
                raise ValueError("--limit must be positive")
            ids = ids[: self.limit]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for task_id in ids:
            target = self.output_dir / task_id
            if target.exists():
                if not self.overwrite:
                    raise FileExistsError(
                        f"{target} exists; use --overwrite to regenerate"
                    )
                shutil.rmtree(target)
            shutil.copytree(TEMPLATE, target)
            filename = f"instance-{int(task_id.rsplit('-', 1)[1])}.pddl"
            problem = (data / "generated" / filename).read_text()
            # Same formatting as per_instance_output_pddl in the upstream harness.
            query_problem = "(define" + problem.split("(define")[1][:-1].strip() + "\n)"
            prompt = PROMPT + "[DOMAIN]\n" + domain.strip() + "\n\n"
            prompt += "[QUERY PROBLEM]\n" + query_problem.strip() + "\n\n[PLAN]"
            (target / "instruction.md").write_text(
                "Save your answer to /app/plan.txt.\n\n" + prompt + "\n"
            )
            config = (target / "task.toml").read_text().replace("{task_id}", task_id)
            (target / "task.toml").write_text(config)
            (target / "tests/domain.pddl").write_text(domain)
            (target / "tests/problem.pddl").write_text(problem)
            (target / "solution/plan.txt").write_text(
                solutions[filename]["plan"] + "\n"
            )
        print(f"Generated {len(ids)} tasks in {self.output_dir}")
