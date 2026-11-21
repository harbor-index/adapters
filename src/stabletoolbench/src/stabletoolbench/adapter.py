"""Generate the development tasks from immutable StableToolBench sources."""
import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory, mkdtemp
import tomllib
from urllib.request import urlopen
from zipfile import ZipFile

REVISION = "aa4ed9f4737ad98bd706663f01d63623c3427812"
TOOLS_REVISION = "0c25df5e6937aae854c2cc1bb203297600afce8b"
BASELINE_REVISION = "ea70398f955e7b58f8807187936818e794ce5140"
BASELINE_SHA256 = "c3c755f68ccd006788d247eb73d644b4c4ea13620ea4702621d921958a80b79c"
TASKS = {int(key): value for key, value in
         json.loads(Path(__file__).with_name("sources.json").read_text()).items()}
SPLIT = "G1_instruction"
TEMPLATE = Path(__file__).with_name("task-template")


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def upstream_text(repo, path):
    return subprocess.check_output(
        ["git", "-C", str(repo), "show", f"{REVISION}:{path}"], text=True
    )


def definitions(source, names):
    selected = {node.name: ast.get_source_segment(source, node) for node in ast.parse(source).body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names}
    return "\n\n".join(selected[name] for name in names)


def native_wrapper(repo):
    # Import the pinned environment itself without importing its optional training,
    # model-backbone, retrieval and live RapidAPI modules.
    prefix = "toolbench/inference/Downstream_tasks"
    imports = "import os, re, json, time\nimport requests\nfrom tqdm import tqdm\nfrom termcolor import colored"
    helpers = definitions(upstream_text(repo, "toolbench/utils.py"), ["standardize", "change_name"])
    base = upstream_text(repo, prefix + "/base_env.py")
    wrapper = definitions(upstream_text(repo, prefix + "/rapidapi_multithread.py"),
                          ["get_white_list", "contain", "rapidapi_wrapper"])
    return "\n\n".join([imports, helpers, base, wrapper]) + "\n"


@contextmanager
def reference_archive(archive_path, wanted, stage):
    if not any(TASKS[qid]["trace_source"] == "baseline" for qid in wanted):
        yield None
        return
    if archive_path is None:
        archive_path = stage / "data_baselines.zip"
        url = ("https://huggingface.co/datasets/stabletoolbench/baselines/resolve/"
               f"{BASELINE_REVISION}/data_baselines.zip")
        with urlopen(url, timeout=120) as source, archive_path.open("wb") as dest:
            shutil.copyfileobj(source, dest)
    with archive_path.open("rb") as raw:
        if hashlib.file_digest(raw, "sha256").hexdigest() != BASELINE_SHA256:
            raise ValueError("Unexpected digest for data_baselines.zip")
        raw.seek(0)
        with ZipFile(raw) as archive:
            yield archive


def reference_answer(repo, archive, query):
    qid = query["query_id"]
    source = TASKS[qid]
    if source["trace_source"] == "upstream":
        raw = subprocess.check_output(
            ["git", "-C", str(repo), "show", f"{REVISION}:{source['trace_path']}"]
        )
    else:
        raw = archive.read(source["trace_path"])
    if hashlib.sha256(raw).hexdigest() != source["trace_sha256"]:
        raise ValueError(f"Unexpected reference trace digest for {qid}")
    answer = json.loads(raw)["answer_generation"]
    final = json.loads(answer["final_answer"])
    if (answer["query"] != query["query"] or not answer["valid_data"]
            or final.get("return_type") != "give_answer" or not final.get("final_answer")):
        raise ValueError(f"No complete reference path for {qid}")
    return answer


def tool_documents(qid, tools_root, destination):
    documents = {}
    for relative, expected in TASKS[qid]["tools"].items():
        path = Path(relative)
        if tools_root is None:
            url = f"https://huggingface.co/datasets/Adorg/ToolBench/resolve/{TOOLS_REVISION}/toolenv/tools/{relative}"
            with urlopen(url, timeout=60) as response:
                contents = response.read()
        else:
            contents = (tools_root / path).read_bytes()
        if hashlib.sha256(contents).hexdigest() != expected:
            raise ValueError(f"Unexpected digest for {relative}")
        tool_file = destination / path
        tool_file.parent.mkdir(parents=True, exist_ok=True)
        tool_file.write_bytes(contents)
        tool = json.loads(contents)
        documents[path.parent.name, tool["tool_name"]] = (path.stem, {api["name"]: api for api in tool["api_list"]})
    return documents


def allowed_tools(query, documents, reference_functions):
    allowed = []
    names = []
    for api in query["api_list"]:
        category = api["category_name"]
        stem, available = documents[category, api["tool_name"]]
        original = available[api["api_name"]]
        if (api["api_description"] != original["description"]
                or any(api[key] != original[key] for key in ("required_parameters", "optional_parameters"))):
            raise ValueError(f"Tool definition does not match query {query['query_id']}")
        # All API names in this release use ASCII words. Check the normalized
        # name against the pinned runner's own recorded function declaration.
        wire_name = re.sub(r"[^a-z0-9]+", "_", api["api_name"].lower()).strip("_")
        names.append(f"{wire_name}_for_{stem}"[-64:])
        allowed.append({"category": category, "tool_name": stem, "api_name": wire_name})
    recorded = [entry.get("function", entry)["name"] for entry in reference_functions]
    if recorded != [*names, "Finish"] or len(set(names)) != len(names):
        raise ValueError(f"Native function declarations do not match query {query['query_id']}")
    return allowed


def make_task(task_dir, repo, tools_root, archive, query, runtime, tool_eval, license_text):
    qid = query["query_id"]
    name = f"g1-instruction-{qid}"
    shutil.copytree(TEMPLATE, task_dir)
    broker = task_dir / "environment" / "broker"
    tests = task_dir / "tests"
    answer = reference_answer(repo, archive, query)
    documents = tool_documents(qid, tools_root, broker / "tools")
    save_json(broker / "allowed-tools.json", allowed_tools(query, documents, answer["function"]))
    actions = [msg["function_call"] for msg in answer["train_messages"][-1] if msg.get("function_call")]
    if len(actions) < 2 or actions[-1]["name"] != "Finish":
        raise ValueError(f"No executable reference path for {qid}")
    functions = {entry.get("function", entry)["name"] for entry in answer["function"]} - {"Finish"}
    if any(action["name"] not in functions for action in actions[:-1]):
        raise ValueError(f"Unexpected actions in reference for {qid}")
    solution = task_dir / "solution"
    save_json(solution / "calls.json", actions[:-1])
    if script := TASKS[qid].get("oracle"):
        shutil.copyfile(Path(__file__).with_name(script), solution / script)
        launcher = solution / "solve.sh"
        launcher.write_text(launcher.read_text().replace("/oracle.py", "/" + script))
    # Query labels and reference actions never enter the agent image or broker.
    public_query = {k: query[k] for k in ("query_id", "query", "api_list")}
    save_json(broker / "query.json", public_query)
    (broker / "native.py").write_text(runtime)
    (broker / "UPSTREAM-LICENSE").write_text(license_text)
    for path, content in tool_eval.items():
        dest = tests / "tooleval" / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    (tests / "tooleval" / "LICENSE").write_text(license_text)
    save_json(tests / "query.json", {"query_id": qid, "query": query["query"]})
    for filename, marker, value in [("task.toml", "{task_id}", name), ("instruction.md", "{query}", query["query"])]:
        dest = task_dir / filename
        dest.write_text(dest.read_text().replace(marker, value))


@contextmanager
def output_lock(directory):
    # Lock the directory itself on POSIX so a generated dataset contains only
    # task directories. Windows uses a byte-range lock on a dedicated file.
    if os.name == "nt":
        import msvcrt
        with (directory / ".stabletoolbench.lock").open("a+b") as stream:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            yield
        return
    import fcntl
    fd = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def check_destination(dest, qid, overwrite):
    if dest.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {dest}")
    if not dest.exists():
        return
    if not overwrite:
        raise FileExistsError(f"{dest} exists. Use --overwrite to regenerate it.")
    try:
        cfg = tomllib.loads((dest / "task.toml").read_text())
        query = json.loads((dest / "tests" / "query.json").read_text())
        matches = (cfg["task"]["name"] == f"stabletoolbench/{dest.name}"
                   and query["query_id"] == qid)
    except (OSError, KeyError, ValueError, TypeError):
        matches = False
    if not matches:
        raise ValueError(f"Refusing to overwrite unrelated path: {dest}")


def publish_tasks(output_dir, stage, wanted, overwrite):
    targets = [(output_dir / f"g1-instruction-{qid}", qid) for qid in wanted]
    with output_lock(output_dir):
        for dest, qid in targets:
            check_destination(dest, qid, overwrite)
        # Store backups outside automatic staging cleanup. If storage fails
        # during rollback, undelivered backups remain available for recovery.
        backups = Path(mkdtemp(prefix=".stabletoolbench-backups-", dir=output_dir))
        backed_up, installed = [], []
        try:
            for dest, _ in targets:
                if dest.exists():
                    dest.rename(backups / dest.name)
                    backed_up.append(dest)
                (stage / dest.name).rename(dest)
                installed.append(dest)
        except BaseException:
            for dest in reversed(installed):
                dest.rename(stage / dest.name)
            for dest in reversed(backed_up):
                (backups / dest.name).rename(dest)
            backups.rmdir()
            raise
        shutil.rmtree(backups)
    for dest, _ in targets:
        print(dest)


class StableToolBenchAdapter:
    def __init__(self, output_dir, upstream_dir=None, toolenv_dir=None, task_ids=None, limit=None, overwrite=False,
                 baseline_archive=None):
        self.output_dir = Path(output_dir)
        self.upstream_dir = Path(upstream_dir) if upstream_dir else None
        self.toolenv_dir = Path(toolenv_dir) if toolenv_dir else None
        self.baseline_archive = Path(baseline_archive) if baseline_archive else None
        self.task_ids = task_ids
        self.limit = limit
        self.overwrite = overwrite

    def run(self):
        wanted = list(TASKS) if self.task_ids is None else list(dict.fromkeys(self.task_ids))
        if not set(wanted) <= TASKS.keys():
            raise ValueError(f"Supported development IDs are {list(TASKS)}")
        if self.limit is not None:
            if self.limit < 0:
                raise ValueError("--limit must be nonnegative")
            wanted = wanted[:self.limit]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=self.output_dir) as staging:
            stage = Path(staging)
            repo = self.upstream_dir
            if repo is None:
                repo = stage / "upstream"
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                subprocess.run(["git", "-C", str(repo), "fetch", "-q", "--depth=1",
                                "https://github.com/THUNLP-MT/StableToolBench.git", REVISION], check=True)
            split_file = f"solvable_queries/test_instruction/{SPLIT}.json"
            queries = {q["query_id"]: q for q in json.loads(upstream_text(repo, split_file))}
            runtime = native_wrapper(repo)
            prefix = "toolbench/tooleval/"
            paths = subprocess.check_output(["git", "-C", str(repo), "ls-tree", "-r", "--name-only",
                                             REVISION, "--", prefix], text=True).splitlines()
            files = {p[len(prefix):]: upstream_text(repo, p) for p in paths
                     if p.endswith((".py", ".yaml", ".txt"))
                     and (p.startswith((prefix+"evaluation/", prefix+"evaluators/")) or
                          p[len(prefix):] in {"convert_to_answer_format.py", "eval_pass_rate.py", "utils.py"})}
            license_text = upstream_text(repo, "LICENSE")
            with reference_archive(self.baseline_archive, wanted, stage) as archive:
                for qid in wanted:
                    name = f"g1-instruction-{qid}"
                    make_task(stage/name, repo, self.toolenv_dir, archive, queries[qid], runtime,
                              files, license_text)
            publish_tasks(self.output_dir, stage, wanted, self.overwrite)
