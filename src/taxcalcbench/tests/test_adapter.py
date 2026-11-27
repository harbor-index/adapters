"""Equivalence tests against a pinned TaxCalcBench checkout.

Run from src/taxcalcbench/:
    TAXCALCBENCH_ROOT=/path/to/tax-calc-bench uv run --extra parity --with pytest pytest tests

Checks:
- instruction.md starts with the exact prompt upstream builds for that case;
- the vendored scorer (tests/score.py) returns the same strict / lenient /
  by-line results as upstream ``TaxReturnEvaluator`` on the oracle answer, an
  empty answer, an answer with one line off by $1 and an answer with every
  line duplicated;
- the oracle is strictly correct and the empty answer is not;
- the scorer's command line writes the reward (and none when it fails);
- the parity and sanity subsets are fixed and equal the shipped case lists, a
  failed generation or replacement keeps the old task, and the generated files
  are the expected set.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from taxcalcbench.adapter import STAGING_DIR, TY25, TaxCalcBenchAdapter, check_pinned_checkout

ROOT = os.environ.get("TAXCALCBENCH_ROOT")
pytestmark = pytest.mark.skipif(not ROOT, reason="set TAXCALCBENCH_ROOT to an upstream checkout")


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("tasks")
    adapter = TaxCalcBenchAdapter(output_dir=out, benchmark_root=Path(ROOT))
    adapter.run()
    return adapter, out


@pytest.fixture(scope="module")
def upstream_evaluator():
    sys.path.insert(0, ROOT)
    try:
        from tax_calc_bench.tax_return_evaluator import TaxReturnEvaluator
    finally:
        sys.path.remove(ROOT)
    return TaxReturnEvaluator()


def _load_scorer(task_dir: Path):
    spec = importlib.util.spec_from_file_location(f"score_{task_dir.name}", task_dir / "tests/score.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _off_by_one(oracle: str) -> str:
    lines = oracle.splitlines()
    for i, line in enumerate(lines):
        if " | " in line and not line.rstrip().endswith("| 0"):
            head, amount = line.rsplit("| ", 1)
            lines[i] = f"{head}| {repr(float(amount) + 1)}"
            break
    return "\n".join(lines) + "\n"


def test_all_cases_generated(generated):
    adapter, out = generated
    cases = adapter.discover()
    assert len(cases) == 101
    assert sorted(p.name for p in out.iterdir()) == sorted(c.task_id for c in cases)


def test_instruction_is_the_prompt_followed_by_one_notes_block(generated):
    # What upstream itself sends for each case is compared in test_parity_agent
    # (test_request_matches_upstream); this checks the file layout around it.
    from taxcalcbench.adapter import NOTES_HEADING

    adapter, out = generated
    for case in adapter.discover():
        prompt = adapter._upstream_prompt(case)
        instruction = (out / case.task_id / "instruction.md").read_text(encoding="utf-8")
        assert instruction.startswith(prompt + "\n" + NOTES_HEADING + "\n"), case.task_id
        assert instruction.count(NOTES_HEADING) == 1, case.task_id
        assert "/app/output/return.txt" in instruction[len(prompt):], case.task_id


def test_scorer_matches_upstream(generated, upstream_evaluator):
    adapter, out = generated
    for case in adapter.discover():
        task_dir = out / case.task_id
        scorer = _load_scorer(task_dir)
        spec = json.loads((task_dir / "tests/scored_fields.json").read_text())
        expected_xml = (task_dir / "tests/expected.xml").read_text()
        oracle = (task_dir / "solution/oracle_return.txt").read_text()
        jurisdiction = case.source_id.split("-")[1] if case.tax_year == TY25 else None

        answers = [("oracle", oracle), ("empty", ""), ("off_by_one", _off_by_one(oracle)), ("duplicate", oracle + oracle)]
        for name, answer in answers:
            mine = scorer.evaluate(answer, expected_xml, spec["tax_year"], spec["fields"])
            theirs = upstream_evaluator.evaluate(
                answer, expected_xml, tax_year=case.tax_year, jurisdiction=jurisdiction
            )
            assert mine["strictly_correct_return"] == theirs.strictly_correct_return, (case.task_id, name)
            assert mine["lenient_correct_return"] == theirs.lenient_correct_return, (case.task_id, name)
            assert mine["correct_by_line_score"] == pytest.approx(theirs.correct_by_line_score), (case.task_id, name)
            assert mine["lenient_correct_by_line_score"] == pytest.approx(
                theirs.lenient_correct_by_line_score
            ), (case.task_id, name)
            if name == "oracle":
                assert mine["strictly_correct_return"], case.task_id
            elif name == "duplicate":
                # Upstream TY25 parsing ignores a label that appears more than once;
                # TY24 parsing takes the first occurrence. Matching upstream is what counts.
                if case.tax_year == TY25:
                    assert not mine["strictly_correct_return"], (case.task_id, name)
            else:
                assert not mine["strictly_correct_return"], (case.task_id, name)


def _case_list(name: str) -> list[tuple[str, ...]]:
    path = Path(__file__).resolve().parents[1] / "parity" / name
    return [tuple(line.split()) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def test_case_lists_match_the_adapter_whatever_the_year_filter(tmp_path):
    def rows(split, years):
        return [(c.task_id, c.tax_year, c.source_id)
                for c in TaxCalcBenchAdapter(output_dir=tmp_path, benchmark_root=Path(ROOT),
                                             split=split, tax_years=years).discover()]

    # The shipped case lists (read by both parity sides) equal the adapter's selection, all three columns.
    full = _case_list("full_cases.txt")
    assert len(full) == 101 and rows("full", ("ty25", "ty24")) == full
    sanity = _case_list("sanity_cases.txt")
    assert len(sanity) == 6 and set(sanity) <= set(full)
    assert rows("sanity", ("ty25", "ty24")) == sanity
    assert rows("sanity", ("ty24",)) == [r for r in sanity if r[1] == "ty24"]
    assert rows("sanity", ("ty25",)) == [r for r in sanity if r[1] == "ty25"]
    with pytest.raises(ValueError):
        TaxCalcBenchAdapter(output_dir=tmp_path, benchmark_root=Path(ROOT), split="parity")
    # The original side's runs 4-5 used the same lines in the Harbor side's (byte-sorted task id) order.
    reordered = _case_list("full_cases_harbor_order.txt")
    assert sorted(reordered) == sorted(full) and reordered == sorted(full, key=lambda r: r[0].encode())


def test_failed_generation_leaves_no_partial_task_and_keeps_the_old_one(tmp_path, monkeypatch):
    out = tmp_path / "ds"
    adapter = TaxCalcBenchAdapter(output_dir=out, benchmark_root=Path(ROOT), task_ids=["ty25-ca-001"], overwrite=True)
    adapter.run()
    marker = out / "ty25-ca-001" / "instruction.md"
    before = marker.read_text()

    def boom(case):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(adapter, "_scored_fields", boom)
    with pytest.raises(RuntimeError):
        adapter.run()
    assert marker.read_text() == before  # the old task is still there, untouched
    assert sorted(p.name for p in out.iterdir()) == ["ty25-ca-001"]  # and no work folder is left
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ds"]


def test_generated_task_files(generated):
    _, out = generated
    task = out / "ty25-ca-001"
    assert {p.name for p in (task / "tests").iterdir()} == {
        "Dockerfile", "test.sh", "score.py", "expected.xml", "scored_fields.json",
        "UPSTREAM_LICENSE", "UPSTREAM_NOTICE.txt"}
    assert {p.suffix for p in (task / "environment" / "input").iterdir()} <= {".pdf", ".json"}
    for agent_visible in [task / "instruction.md", task / "environment" / "Dockerfile"]:
        text = agent_visible.read_text().lower()
        assert "harbor" not in text and "taxcalcbench" not in text and "canary" not in text
    # The input tag follows the tax year; no placeholder is left in any task.toml.
    assert '"pdf"' in (task / "task.toml").read_text() and '"json"' not in (task / "task.toml").read_text()
    ty24 = (out / "ty24-single-senior-blind-over-65" / "task.toml").read_text()
    assert '"json"' in ty24 and '"pdf"' not in ty24
    assert not any("{" in (d / "task.toml").read_text().split("[metadata]")[1].split("[agent]")[0]
                   for d in out.iterdir() if d.is_dir())


def test_failed_replacement_puts_the_old_task_back(tmp_path, monkeypatch):
    out = tmp_path / "ds"
    adapter = TaxCalcBenchAdapter(output_dir=out, benchmark_root=Path(ROOT), task_ids=["ty25-ca-001"], overwrite=True)
    adapter.run()
    marker = out / "ty25-ca-001" / "instruction.md"
    before = marker.read_text()
    real_rename = Path.rename

    def rename(self, target):
        if self.parent.name == STAGING_DIR and not self.name.endswith(".previous"):
            raise OSError("simulated failure while moving the new task into place")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError):
        adapter.run()
    monkeypatch.undo()
    assert marker.read_text() == before
    assert sorted(p.name for p in out.iterdir()) == ["ty25-ca-001"]


def test_killed_replacement_is_recovered_on_the_next_run(tmp_path):
    # A run that was killed between moving the old task aside and moving the new
    # one in leaves the old task in the staging folder; the next run puts it back.
    out = tmp_path / "ds"
    adapter = TaxCalcBenchAdapter(output_dir=out, benchmark_root=Path(ROOT), task_ids=["ty25-ca-001"])
    adapter.run()
    before = (out / "ty25-ca-001" / "instruction.md").read_text()
    staging = out / STAGING_DIR
    staging.mkdir()
    (out / "ty25-ca-001").rename(staging / "ty25-ca-001.previous")
    (staging / "ty25-ca-001").mkdir()  # half-written replacement
    assert adapter.run() == []  # the old task is back, so there is nothing to write
    assert sorted(p.name for p in out.iterdir()) == ["ty25-ca-001"]
    assert (out / "ty25-ca-001" / "instruction.md").read_text() == before


def test_output_folder_with_an_unwritable_parent_works(tmp_path):
    parent = tmp_path / "locked"
    out = parent / "ds"
    out.mkdir(parents=True)
    parent.chmod(0o555)
    try:
        adapter = TaxCalcBenchAdapter(output_dir=out, benchmark_root=Path(ROOT), task_ids=["ty25-ca-001"])
        assert [p.name for p in adapter.run()] == ["ty25-ca-001"]
    finally:
        parent.chmod(0o755)
    assert sorted(p.name for p in out.iterdir()) == ["ty25-ca-001"]


def _score_cli(task: Path, answer: Path, work: Path) -> tuple[subprocess.CompletedProcess, Path, Path]:
    report, reward = work / "score.json", work / "reward.txt"
    for f in (report, reward):
        f.unlink(missing_ok=True)
    proc = subprocess.run(
        [sys.executable, str(task / "tests" / "score.py"), "--output", str(answer),
         "--expected", str(task / "tests" / "expected.xml"), "--fields", str(task / "tests" / "scored_fields.json"),
         "--report", str(report), "--reward", str(reward)], capture_output=True, text=True)
    return proc, report, reward


def test_scorer_command_line_writes_the_reward(generated, tmp_path):
    _, out = generated
    for task_id in ("ty25-ca-001", "ty24-single-senior-blind-over-65"):
        task = out / task_id
        oracle = task / "solution" / "oracle_return.txt"
        empty, huge, link = tmp_path / "empty.txt", tmp_path / "huge.txt", tmp_path / f"link-{task_id}.txt"
        empty.write_text("")
        huge.write_bytes(oracle.read_bytes() + b"\n" * (5 * 1024 * 1024 + 1))
        link.symlink_to(oracle)
        expected = {"oracle": (oracle, "1", True, False), "missing": (tmp_path / "nope.txt", "0", False, False),
                    "empty": (empty, "0", True, False), "huge": (huge, "0", True, True),
                    "symlink": (link, "0", False, False)}
        for name, (answer, reward_text, found, too_large) in expected.items():
            proc, report, reward = _score_cli(task, answer, tmp_path)
            assert proc.returncode == 0, (task_id, name, proc.stderr)
            assert reward.read_text().strip() == reward_text, (task_id, name)
            result = json.loads(report.read_text())
            assert (result["output_found"], result["output_too_large"]) == (found, too_large), (task_id, name)


def test_scorer_failure_writes_no_reward(generated, tmp_path):
    _, out = generated
    task = tmp_path / "broken"
    (task / "tests").mkdir(parents=True)
    source = out / "ty25-ca-001"
    for name in ("score.py", "expected.xml"):
        (task / "tests" / name).write_bytes((source / "tests" / name).read_bytes())
    (task / "tests" / "scored_fields.json").write_text("{not json")
    proc, report, reward = _score_cli(task, source / "solution" / "oracle_return.txt", tmp_path)
    assert proc.returncode != 0 and not reward.exists() and not report.exists()


def _relocated_test_sh(task: Path, root: Path) -> Path:
    """The task's test.sh with its container paths moved under ``root`` (logic unchanged)."""
    text = (task / "tests" / "test.sh").read_text(encoding="utf-8")
    for container_path, local in (("/logs/verifier", root / "logs"), ("/tests/", f"{root / 'tests'}/"),
                                  ("/app/output", root / "output"), ("/usr/local/bin/python3", sys.executable)):
        assert container_path in text
        text = text.replace(container_path, str(local))
    for name in ("logs", "tests", "output"):
        (root / name).mkdir(parents=True, exist_ok=True)
    for f in (task / "tests").iterdir():
        (root / "tests" / f.name).write_bytes(f.read_bytes())
    script = root / "test.sh"
    script.write_text(text, encoding="utf-8")
    return script


def test_test_sh_writes_a_reward_only_when_the_scorer_ran(generated, tmp_path):
    _, out = generated
    task = out / "ty25-ca-001"
    script = _relocated_test_sh(task, tmp_path)
    logs = tmp_path / "logs"
    (tmp_path / "output" / "return.txt").write_bytes((task / "solution" / "oracle_return.txt").read_bytes())
    assert subprocess.run(["bash", str(script)], capture_output=True).returncode == 0
    assert (logs / "reward.txt").read_text().strip() == "1" and (logs / "score.json").exists()
    # The scorer cannot run: files left from before must not stand in for a result.
    (logs / "reward.json").write_text('{"reward": 1}')
    (tmp_path / "tests" / "scored_fields.json").write_text("{not json")
    failed = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert failed.returncode == 1 and "no reward written" in failed.stderr
    assert not (logs / "reward.txt").exists() and not (logs / "reward.json").exists()
    assert not (logs / "score.json").exists()


def test_pinned_checkout_check(tmp_path):
    git = ("git", "-c", "user.name=t", "-c", "user.email=t@example.com")
    repo = tmp_path / "repo"
    data = repo / "tax_calc_bench" / "ty25" / "test_data"
    data.mkdir(parents=True)
    (data / "case.json").write_text("{}")
    (repo / "tax_calc_bench" / "prompt.py").write_text("PROMPT = 'x'\n")
    (repo / ".gitignore").write_text("*.cache\n")
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True, text=True)  # noqa: E731
    run("git", "init", "-q")
    run(*git, "add", ".")
    run(*git, "commit", "-q", "-m", "init")
    head = run("git", "rev-parse", "HEAD").stdout.strip()
    assert check_pinned_checkout(repo, head) == head
    with pytest.raises(RuntimeError, match="expected"):
        check_pinned_checkout(repo)  # not the pinned commit
    # Saved outputs under results/ are allowed; anything else is a local change.
    results = repo / "tax_calc_bench" / "ty25" / "results"
    results.mkdir()
    (results / "out.md").write_text("saved output")
    assert check_pinned_checkout(repo, head) == head
    (data / ".DS_Store").write_text("Finder's folder file is harmless")
    (repo / ".DS_Store").write_text("here too")
    assert check_pinned_checkout(repo, head) == head
    (data / "extra.cache").write_text("ignored by git, still an extra file")
    with pytest.raises(RuntimeError, match="extra files in the test data"):
        check_pinned_checkout(repo, head)
    (data / "extra.cache").unlink()
    (data / "case.json").write_text('{"changed": true}')
    with pytest.raises(RuntimeError, match="local changes"):
        check_pinned_checkout(repo, head)
    # A change hidden from git status by an index flag is found as well, for both flags git has.
    for flag in ("assume-unchanged", "skip-worktree"):
        run("git", "update-index", f"--{flag}", "tax_calc_bench/ty25/test_data/case.json")
        with pytest.raises(RuntimeError, match="hidden from git status"):
            check_pinned_checkout(repo, head)
        run("git", "update-index", f"--no-{flag}", "tax_calc_bench/ty25/test_data/case.json")
    # The same for code and prompts, outside the test data.
    run("git", "checkout", "-q", "--", ".")
    (repo / "tax_calc_bench" / "prompt.py").write_text("PROMPT = 'changed'\n")
    run("git", "update-index", "--assume-unchanged", "tax_calc_bench/prompt.py")
    with pytest.raises(RuntimeError, match="hidden from git status"):
        check_pinned_checkout(repo, head)
    run("git", "update-index", "--no-assume-unchanged", "tax_calc_bench/prompt.py")
    # A checkout that leaves out upstream's saved outputs (a sparse checkout) is fine.
    run("git", "checkout", "-q", "--", ".")
    run(*git, "add", "-f", "tax_calc_bench/ty25/results/out.md")
    run(*git, "commit", "-q", "-m", "a saved output")
    newer = run("git", "rev-parse", "HEAD").stdout.strip()
    run("git", "update-index", "--skip-worktree", "tax_calc_bench/ty25/results/out.md")
    assert check_pinned_checkout(repo, newer) == newer


def test_template_by_products_are_not_copied_into_tasks(tmp_path, monkeypatch):
    import shutil

    import taxcalcbench.adapter as adapter_module

    template = tmp_path / "task-template"
    shutil.copytree(adapter_module.TEMPLATE_DIR, template)
    (template / ".DS_Store").write_text("finder")
    (template / "tests" / ".DS_Store").write_text("finder")
    (template / "tests" / "__pycache__").mkdir()
    (template / "tests" / "__pycache__" / "score.cpython-313.pyc").write_bytes(b"x")
    monkeypatch.setattr(adapter_module, "TEMPLATE_DIR", template)
    out = tmp_path / "ds"
    TaxCalcBenchAdapter(output_dir=out, benchmark_root=Path(ROOT), task_ids=["ty25-ca-001"]).run()
    names = {p.name for p in (out / "ty25-ca-001").rglob("*")}
    assert ".DS_Store" not in names and "__pycache__" not in names and "score.py" in names
