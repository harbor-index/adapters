from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from adapter import TEMPLATE_DIR, GAIAAdapter
from run_adapter import records_to_tasks

# Keep the template free of __pycache__ (the adapter copies tests/ files into tasks).
sys.dont_write_bytecode = True

SCORER = TEMPLATE_DIR / "tests" / "gaia_scorer.py"
# scorer.py at commit 1349a17979f0aca0ee9c46cd7ec26eb2fb41102e of the
# gaia-benchmark/leaderboard Space.
UPSTREAM_SHA256 = "0d44c07f3046eec521697c22e3eaca8719cc81e422a8eaf32695c5f22bdac6e2"
HEADER_END = "# --- end of vendoring header ---\n"

# (agent answer, ground truth): rejected by the previous test.sh, accepted by
# the official scorer. The first two are real agent answers.
OFFICIAL_ACCEPTS = [
    (
        "Mapping Human-Oriented Information to Software Agents for Online Systems Usage",
        "Mapping Human Oriented Information to Software Agents for Online Systems Usage",
    ),
    ("chair.", "chair"),
    ("1,000", "1000"),
    ("$17", "17"),
    ("17.0", "17"),
    ("sea gull", "seagull"),
    ("apple, banana", "apple,banana"),
    ("Paris", "paris"),
]
OFFICIAL_REJECTS = [
    ("Lyon", "Paris"),
    ("18", "17"),
    ("apple", "apple,banana"),
    ("banana, apple", "apple,banana"),
]


def load_scorer():
    spec = importlib.util.spec_from_file_location("gaia_scorer", SCORER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vendored_scorer_is_upstream_apart_from_numpy_import() -> None:
    header, body = SCORER.read_text(encoding="utf-8").split(HEADER_END)
    assert "1349a17979f0aca0ee9c46cd7ec26eb2fb41102e" in header
    assert UPSTREAM_SHA256 in header
    upstream = body.replace(
        "import warnings\n\n", "import warnings\n\nimport numpy as np\n", 1
    )
    assert hashlib.sha256(upstream.encode()).hexdigest() == UPSTREAM_SHA256


@pytest.mark.parametrize(("answer", "truth"), OFFICIAL_ACCEPTS)
def test_official_scorer_accepts(answer: str, truth: str) -> None:
    assert load_scorer().question_scorer(answer, truth) is True


@pytest.mark.parametrize(("answer", "truth"), OFFICIAL_REJECTS)
def test_official_scorer_rejects(answer: str, truth: str) -> None:
    assert load_scorer().question_scorer(answer, truth) is False


def render_task(tmp_path: Path, truth: str) -> tuple[Path, Path]:
    """Render one task through the adapter from a stub GAIA record.

    Returns the task directory and an empty sandbox root that stands in for
    the container's filesystem (see run_in_sandbox).
    """
    record = {"task_id": "stub", "Question": "Q?", "Final answer": truth, "Level": 1}
    adapter = GAIAAdapter(output_dir=tmp_path / "tasks")
    task_dir = adapter.generate_task(records_to_tasks([record], source_name="stub")[0])
    root = tmp_path / "root"
    (root / "app").mkdir(parents=True)
    (root / "tests").symlink_to(task_dir / "tests")
    return task_dir, root


def run_in_sandbox(script: Path, root: Path) -> None:
    """Run a rendered task script with its container paths moved under root."""
    text = script.read_text()
    for container_path, host_path in (
        ("/logs/verifier", root / "logs" / "verifier"),
        ("/app/", f"{root / 'app'}/"),
        ("/tests", root / "tests"),
    ):
        text = text.replace(container_path, str(host_path))
    # check=True: test.sh must exit 0 whatever the verdict.
    subprocess.run(["bash", "-c", text], check=True, timeout=60)


def reward(root: Path) -> str:
    return (root / "logs" / "verifier" / "reward.txt").read_text().strip()


def verify(tmp_path: Path, truth: str, answer: str | None) -> str:
    task_dir, root = render_task(tmp_path, truth)
    if answer is not None:
        (root / "app" / "answer.txt").write_text(answer)
    run_in_sandbox(task_dir / "tests" / "test.sh", root)
    return reward(root)


@pytest.mark.parametrize(("answer", "truth"), OFFICIAL_ACCEPTS)
def test_test_sh_rewards_answers_the_official_scorer_accepts(
    tmp_path: Path, answer: str, truth: str
) -> None:
    assert verify(tmp_path, truth, answer) == "1"


@pytest.mark.parametrize(("answer", "truth"), OFFICIAL_REJECTS)
def test_test_sh_rejects_wrong_answers(tmp_path: Path, answer: str, truth: str) -> None:
    assert verify(tmp_path, truth, answer) == "0"


def test_test_sh_rejects_missing_answer_file(tmp_path: Path) -> None:
    assert verify(tmp_path, "Paris", None) == "0"


def test_test_sh_gives_0_when_the_scorer_fails(tmp_path: Path) -> None:
    task_dir, root = render_task(tmp_path, "Paris")
    (task_dir / "tests" / "gaia_scorer.py").unlink()
    (root / "app" / "answer.txt").write_text("Paris")
    run_in_sandbox(task_dir / "tests" / "test.sh", root)
    assert reward(root) == "0"


@pytest.mark.parametrize("truth", ["Paris", "17", "apple, banana", "3; 4.5; x"])
def test_oracle_solution_is_rewarded(tmp_path: Path, truth: str) -> None:
    task_dir, root = render_task(tmp_path, truth)
    run_in_sandbox(task_dir / "solution" / "solve.sh", root)
    run_in_sandbox(task_dir / "tests" / "test.sh", root)
    assert reward(root) == "1"


def test_task_toml_names_the_task_and_credits_the_gaia_authors(tmp_path: Path) -> None:
    task_dir, _ = render_task(tmp_path, "Paris")
    task = tomllib.loads((task_dir / "task.toml").read_text())["task"]
    assert task["name"] == "gaia-benchmark/gaia__stub"
    assert task["authors"][0]["name"] == "Grégoire Mialon"
