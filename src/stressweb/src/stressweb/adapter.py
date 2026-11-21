"""Generate self-contained Note tasks from immutable, verified public assets."""
import hashlib
import json
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

PACKAGE = Path(__file__).parent
MANIFEST = json.loads((PACKAGE / "manifest.json").read_text())
SOURCES = json.loads((PACKAGE / "sources.json").read_text())
SITE = "websites/PRD_20260115_175319_note/generated-website-query-1"
TASKS = {
    f"note-q{query}-{mode.lower()}": (query, mode)
    for query in MANIFEST["queries"] for mode in MANIFEST["modes"]
}


class StressWebAdapter:
    def __init__(self, output_dir, cache_dir, task_ids=None, limit=None, overwrite=False):
        self.output_dir = Path(output_dir)
        self.cache = Path(cache_dir)
        self.task_ids = list(TASKS) if task_ids is None else task_ids
        unknown = set(self.task_ids) - TASKS.keys()
        if unknown or len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError(f"Unknown or duplicate task IDs: {self.task_ids}")
        if limit is not None:
            if limit < 0:
                raise ValueError("--limit must be nonnegative")
            self.task_ids = self.task_ids[:limit]
        self.overwrite = overwrite

    def asset(self, name):
        target = self.cache / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == SOURCES[name]:
            return target
        if name.startswith("StressWeb/"):
            prefix = "https://raw.githubusercontent.com/inclusionAI/AWorld-RL/" + MANIFEST["code_revision"]
        else:
            prefix = "https://huggingface.co/datasets/HuggingSelf/StressWeb/resolve/" + MANIFEST["data_revision"]
        with urlopen(f"{prefix}/{name}", timeout=60) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != SOURCES[name]:
            raise ValueError(f"Content hash mismatch for {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
            tmp.write(content)
        os.replace(tmp.name, target)
        return target

    def run(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for name in self.task_ids:
            task = self.output_dir / name
            if task.exists() and not self.overwrite:
                raise FileExistsError(f"{task} already exists; use --overwrite")
            query, mode = TASKS[name]
            site = SITE + MANIFEST["modes"][mode]
            files = {key: "environment/browser/site/" + key[len(site)+1:]
                     for key in SOURCES if key.startswith(site + "/")}
            for module in ("web_actions", "playwright_controller"):
                files[f"StressWeb/{module}.py"] = f"environment/browser/{module}.py"
            files[f"StressWeb/evaluator/note_query{query}.py"] = "tests/evaluator.py"
            query_file = self.asset("queries/note_query.md")
            prompt = re.search(rf"^{query}\. (.+)$", query_file.read_text(), re.MULTILINE).group(1)
            with tempfile.TemporaryDirectory(dir=self.output_dir) as temp:
                staging = Path(temp) / name
                shutil.copytree(PACKAGE / "task-template", staging,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                with ThreadPoolExecutor(max_workers=6) as workers:
                    for asset, dest in zip(workers.map(self.asset, files), files.values()):
                        target = staging / dest
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(asset, target)
                for filename, marker, replacement in [("instruction.md", "@QUERY@", prompt),
                    ("task.toml", "@TASK@", name), ("solution/solve.sh", "@QUERY_ID@", str(query)), ("solution/solve.sh", "@MODE@", mode)]:
                    path = staging / filename
                    path.write_text(path.read_text().replace(marker, replacement))
                (staging / "environment/browser/query.json").write_text(json.dumps(
                    {"query": prompt, "query_id": query, "mode": mode}, indent=2) + "\n")
                # Task distributions can be generated under a restrictive HPC umask.
                # Make only the published assets traversable/readable for runtime users.
                for path in [staging, *staging.rglob("*")]:
                    path.chmod(0o755 if path.is_dir() or path.suffix == ".sh" else 0o644)
                if task.exists():
                    if not self.overwrite:
                        raise FileExistsError(f"{task} was generated concurrently")
                    if not (task / "task.toml").is_file() or f'name = "stressweb/{name}"' not in (task / "task.toml").read_text():
                        raise ValueError(f"Cannot overwrite unrelated path: {task}")
                    backup = Path(temp) / "previous"
                    task.rename(backup)
                    try:
                        staging.rename(task)
                    except BaseException:
                        backup.rename(task)
                        raise
                else:
                    staging.rename(task)
