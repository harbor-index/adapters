"""Adapt the eight generative tasks with upstream two-shot prompts and scorers."""

import gzip
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml
from jinja2 import Template

UPSTREAM_URL = "https://github.com/IBM/ACPBench.git"
UPSTREAM_REVISION = "c93f61c31efb63fa662bcc24d27c15556d319710"
TEMPLATE = Path(__file__).parent / "task-template"


class ACPBenchHardAdapter:
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
        with tempfile.TemporaryDirectory(prefix="acpbench-hard-") as temp:
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
        records = []
        for category in (
            "app",
            "areach",
            "just",
            "land",
            "nexta",
            "prog",
            "reach",
            "val",
        ):
            config_name = {"areach": "act_reach", "nexta": "next_act"}.get(
                category, category
            )
            config = yaml.safe_load(
                (source / f"configs/tasks/acp_gen_chat/{config_name}.yaml").read_text()
            )
            with gzip.open(
                source / f"dataset/{category}/test.gen.json.gz", "rt"
            ) as stream:
                data = json.load(stream)
            for index, record in enumerate(data):
                task_id = (
                    f"acpbench-hard-{category}-{str(record['id']).replace('-', 'n')}"
                )
                if self.split == "parity" and index % 10:
                    continue
                records.append((task_id, category, record, config))
        if self.task_ids is not None:
            unknown = set(self.task_ids) - {r[0] for r in records}
            if unknown:
                raise ValueError(f"Unknown task IDs in {self.split}: {sorted(unknown)}")
            records = [r for r in records if r[0] in self.task_ids]
        if self.limit is not None:
            if self.limit < 1:
                raise ValueError("--limit must be positive")
            records = records[: self.limit]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for task_id, category, record, config in records:
            target = self.output_dir / task_id
            if target.exists():
                if not self.overwrite:
                    raise FileExistsError(
                        f"{target} exists; use --overwrite to regenerate"
                    )
                shutil.rmtree(target)
            shutil.copytree(TEMPLATE, target)
            template = Template(config["doc_to_text"])
            examples = [
                template.render(**example) + " " + example["answer"]
                for example in config["fewshot_config"]["samples"][:2]
            ]
            prompt = "\n\n".join(examples + [template.render(**record)])
            (target / "instruction.md").write_text(
                "Save your answer to /app/answer.txt.\n\n" + prompt + "\n"
            )
            task_config = (
                (target / "task.toml").read_text().replace("{task_id}", task_id)
            )
            (target / "task.toml").write_text(task_config)
            (target / "tests/record.json").write_text(json.dumps(record))
            (target / "tests/category.txt").write_text(category)
            (target / "solution/answer.txt").write_text(
                oracle_answer(category, record) + "\n"
            )
        print(f"Generated {len(records)} tasks in {self.output_dir}")


def oracle_answer(category, record):
    answer = record["answer"]
    if category == "app":
        return " ".join(answer)
    if category == "prog":
        return "[" + ", ".join(answer["pos"]) + "] [" + ", ".join(answer["neg"]) + "]"
    if category == "val":
        return str(answer) if answer != -1 else "None"
    if category in ("land", "nexta"):
        return answer["yes"][0] if answer["yes"] else "None"
    if category == "reach":
        return f"({answer[0]})" if answer else "None"
    if category == "areach":
        return answer[0] if answer else "None"
    # Upstream annotations identify removable actions; the trailing integer is
    # the 1-based occurrence for a single action, or -1 for a consecutive pair.
    plan = re.findall(r"\([^()]+\)", record["question"].split('"')[1])
    removal = answer[0]
    actions = removal[:-1]
    matches = [i for i in range(len(plan)) if plan[i : i + len(actions)] == actions]
    index = matches[0 if len(actions) == 2 else int(removal[-1]) - 1]
    return " ".join(plan[:index] + plan[index + len(actions) :])
