"""Generation tests on a tiny fake task repo (no network)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from workspacebench import adapter as ad
from workspacebench.prompt import wrap_prompt


def _fake_repo(root: Path, language: str, ids=range(1, 389)) -> Path:
    tasks = root / ad.TASK_DIRS[language]
    personas = list(ad.PERSONA_DIRS)
    for i in ids:
        d = tasks / str(i)
        (d / "data").mkdir(parents=True)
        (d / "data" / f"abc_in{i}.txt").write_text("input")
        meta = {
            "id": str(i), "language": language, "persona": personas[i % len(personas)],
            "task": f"Task {i}: write `out{i}.md`.", "task_diff": "medium",
            "output_files": [f"out{i}.md"], "rubrics": ["r1", "r2"], "rubric_types": ["Basic Evaluation"] * 2,
            "data_manifest": [{"filename": f"in{i}.txt", "stored_relpath": f"data/abc_in{i}.txt"}],
        }
        (d / "metadata.json").write_text(json.dumps(meta))
    return root


@pytest.fixture
def repo(tmp_path):
    _fake_repo(tmp_path / "repo", "en")
    _fake_repo(tmp_path / "repo", "cn")
    return tmp_path / "repo"


def test_full_split_generates_both_languages(repo, tmp_path):
    out = tmp_path / "out"
    ad.WorkspaceBenchAdapter(out, tasks_root=repo, limit=None).run()
    names = sorted(p.name for p in out.iterdir())
    assert len(names) == 776 - len(ad.EXCLUDED_TASKS) == 723
    assert names[0] == "cn-001" and "en-388" in names
    assert not set(names) & ad.EXCLUDED_TASKS.keys()


def test_parity_split_is_english_subset(repo, tmp_path):
    out = tmp_path / "out"
    ad.WorkspaceBenchAdapter(out, tasks_root=repo, split="parity").run()
    names = {p.name for p in out.iterdir()}
    assert len(names) == 50 == len(ad.PARITY_IDS)
    assert names == {f"en-{i:03d}" for i in ad.PARITY_IDS}
    assert ad.PARITY_IDS <= ad.PARITY_POOL and len(ad.PARITY_POOL - ad.LITE_IDS) == 10
    assert set(ad.PARITY_REPLACEMENTS.values()) == ad.PARITY_POOL - ad.LITE_IDS
    assert not names & ad.EXCLUDED_TASKS.keys()


def test_task_contents(repo, tmp_path):
    out = tmp_path / "out"
    oracle = tmp_path / "oracle" / "en-033"
    oracle.mkdir(parents=True)
    (oracle / "out33.md").write_text("answer")
    ad.WorkspaceBenchAdapter(out, tasks_root=repo, task_ids=["en-033"], oracle_dir=tmp_path / "oracle").run()
    t = out / "en-033"

    toml = tomllib.loads((t / "task.toml").read_text())
    assert toml["task"]["name"] == "workspacebench/en-033"
    persona = ad.PERSONA_DIRS[list(ad.PERSONA_DIRS)[33 % 5]]
    assert toml["environment"]["storage_mb"] == ad.PERSONA_STORAGE_MB[persona]

    instruction = (t / "instruction.md").read_text()
    assert instruction == wrap_prompt(prompt="Task 33: write `out33.md`.", work_dir="/app",
                                      task_target_output_dir="model_output", language="en")
    assert (t / "tests" / "instruction.md").read_text() == instruction
    assert f"--persona {persona}" in (t / "environment" / "Dockerfile").read_text()
    assert (t / "tests" / "task" / "data" / "abc_in33.txt").exists()
    assert json.loads((t / "tests" / "task" / "metadata.json").read_text())["id"] == "33"
    assert (t / "solution" / "output" / "out33.md").read_text() == "answer"
    assert "{" not in (t / "task.toml").read_text().split("[verifier.env]")[0].replace("{ name", "")


def test_verifier_is_isolated(repo, tmp_path):
    """Grading runs in a separate container; the collect hook carries output names only, never rubrics."""
    import base64
    out = tmp_path / "out"
    ad.WorkspaceBenchAdapter(out, tasks_root=repo, task_ids=["en-033"]).run()
    t = out / "en-033"
    toml = tomllib.loads((t / "task.toml").read_text())
    assert toml["verifier"]["environment_mode"] == "separate"
    assert "/logs/wsb_collected" in toml["artifacts"]
    (hook,) = toml["verifier"]["collect"]
    meta = json.loads(base64.b64decode(hook["command"].split("--meta-b64 ")[1]))
    assert meta == {"output_files": ["out33.md"]}
    assert "rubrics" not in hook["command"] and "r1" not in json.dumps(meta)
    assert "COPY collect_outputs.py" in (t / "environment" / "Dockerfile").read_text()
    assert toml["verifier"]["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


def test_skills_split_per_tool_like_upstream(repo, tmp_path):
    """Claude gets docx/pdf/pptx/xlsx, Codex gets docx/pdfs/slides/spreadsheets (upstream's image)."""
    out = tmp_path / "out"
    ad.WorkspaceBenchAdapter(out, tasks_root=repo, task_ids=["en-033"]).run()
    df = (out / "en-033" / "environment" / "Dockerfile").read_text()
    assert not list(out.rglob("__pycache__"))
    assert "for s in docx pdf pptx xlsx; do" in df
    assert "for s in docx pdfs slides spreadsheets; do" in df
    assert "/root/.agents/skills" in df
    assert "skills/office/. " not in df  # no wholesale copy of all 7


def test_prompt_matches_upstream_shape():
    en = wrap_prompt(prompt=" do it ", work_dir="/app", task_target_output_dir="model_output", language="en")
    assert en.startswith("[Important Requirement 1: Working Directory]\n")
    assert "Place all output files under: /app/model_output\n\ndo it\n" in en
    cn = wrap_prompt(prompt="做", work_dir="/app", task_target_output_dir="model_output", language="cn")
    assert "/app/model_output下" in cn and cn.rstrip().endswith("['output/a.txt','report.md']")


def test_wrong_task_count_fails(tmp_path):
    _fake_repo(tmp_path / "small", "en", ids=range(1, 10))
    with pytest.raises(RuntimeError, match="Expected 388"):
        ad.WorkspaceBenchAdapter(tmp_path / "out", tasks_root=tmp_path / "small", languages=["en"]).run()


def _fetch_module():
    import importlib.util

    import sys

    spec = importlib.util.spec_from_file_location("fetch_workspace", ad.TEMPLATE_DIR / "environment" / "fetch_workspace.py")
    mod = importlib.util.module_from_spec(spec)
    before, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ inside the task template
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = before
    return mod


def test_chunked_fetch_unpacks_identical_files_and_resumes(tmp_path, monkeypatch):
    """Range-request unpacking matches zipfile byte for byte, also after a dropped connection."""
    import os
    import random
    import zipfile

    fw = _fetch_module()
    rnd = random.Random(0)
    src = {
        "P/": None,
        "P/a.txt": b"hello " * 1000,
        "P/sub/": None,
        "P/sub/b.bin": rnd.randbytes(300_000),
        "P/sub/c.txt": "中文 ".encode() * 5000,
        "Q/other.txt": b"not this persona",
    }
    zpath = tmp_path / "w.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for i, (name, data) in enumerate(src.items()):
            if data is None:
                z.writestr(zipfile.ZipInfo(name), b"")
            else:
                z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED if i % 2 else zipfile.ZIP_STORED)
    blob = zpath.read_bytes()
    with zipfile.ZipFile(zpath) as z:
        infos = sorted(z.infolist(), key=lambda i: i.header_offset)
        ends = [i.header_offset for i in infos[1:]] + [z.start_dir]
    dest = tmp_path / "dest"
    entries = []
    for i, end in zip(infos, ends):
        if not i.filename.startswith("P/"):
            continue
        rel = i.filename[2:]
        out = None if not rel or i.is_dir() else str(dest / rel)
        if out:
            os.makedirs(os.path.dirname(out), exist_ok=True)
        entries.append(fw.Entry(i, end, out))

    calls = []

    def fetch(start, end):
        calls.append((start, end))
        data = blob[start:end]
        for k in range(0, len(data), 4096):
            if len(calls) == 1 and k >= 200_000:
                raise ConnectionError("simulated drop")
            yield data[k:k + 4096]

    monkeypatch.setattr(fw.time, "sleep", lambda s: None)
    chunks = fw.plan_chunks(entries)
    assert len(chunks) == 1  # persona entries are adjacent
    total = sum(fw.unpack_chunk(fetch, c, log=lambda *a, **k: None) for c in chunks)
    files = {n: d for n, d in src.items() if n.startswith("P/") and d is not None}
    assert total == sum(len(d) for d in files.values())
    for name, data in files.items():
        assert (dest / name[2:]).read_bytes() == data
    assert len(calls) == 2 and calls[1][0] > calls[0][0]  # resumed at the entry in progress, not from the start
    q_start = next(i.header_offset for i in infos if i.filename.startswith("Q/"))
    assert max(e for _, e in calls) <= q_start  # never reads another persona's bytes

    assert len(fw.plan_chunks(entries, limit=1)) == len(entries)  # one run per entry when the limit is tiny


def test_excluded_tasks_have_reasons():
    assert len(ad.EXCLUDED_TASKS) == 53
    assert all(n[:3] in ("en-", "cn-") and len(r) > 30 for n, r in ad.EXCLUDED_TASKS.items())


def test_metadata_normalized_like_upstream(repo, tmp_path):
    """The judge reads the same metadata fields upstream's downloader adds (id, job, file_system, user_profit)."""
    out = tmp_path / "out"
    ad.WorkspaceBenchAdapter(out, tasks_root=repo, task_ids=["en-033"]).run()
    meta = json.loads((out / "en-033" / "tests" / "task" / "metadata.json").read_text())
    assert meta["id"] == "33"
    assert meta["job"] == meta["persona"]
    assert meta["file_system"] == meta["user_profit"] == ad.PERSONA_TO_FILE_SYSTEM[meta["persona"]]
