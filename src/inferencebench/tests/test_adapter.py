"""Tests that run without a GPU, a container runtime, or API keys.

uv run --with pytest --with pytest-asyncio --with harbor==0.25.0 pytest tests/
"""

import importlib.util
import json
import subprocess
import sys
import tomllib
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from inferencebench.adapter import InferenceBenchAdapter
from inferencebench.score import load_rewards, summarize

TEMPLATE_TESTS = Path(__file__).parents[1] / "src" / "inferencebench" / "task-template" / "tests"
SCENARIO_TASKS = {
    "a": "inference_scenario_a_input_heavy",
    "b": "inference_scenario_b_output_heavy",
    "c": "inference_scenario_c_high_load",
    "d": "inference_scenario_d_general",
}


@pytest.fixture(scope="module")
def tasks(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("tasks")
    InferenceBenchAdapter(output).run()
    return output


def test_generates_twelve_tasks_with_stable_unique_names(tasks: Path) -> None:
    names = sorted(
        tomllib.loads((path / "task.toml").read_text())["task"]["name"] for path in tasks.iterdir()
    )
    assert len(set(names)) == 12
    assert names[0] == "aisa-group/inferencebench-a-mistral-7b-21-1337"
    assert names[-1] == "aisa-group/inferencebench-d-mistral-7b-999-777"


def test_limit_and_task_ids(tmp_path: Path) -> None:
    InferenceBenchAdapter(tmp_path / "limit", limit=2).run()
    assert len(list((tmp_path / "limit").iterdir())) == 2
    task_id = "inferencebench-c-mistral-7b-248-428"
    InferenceBenchAdapter(tmp_path / "ids", task_ids=[task_id]).run()
    assert [path.name for path in (tmp_path / "ids").iterdir()] == [task_id]
    with pytest.raises(ValueError):
        InferenceBenchAdapter(tmp_path / "bad", task_ids=["nope"]).run()


def test_no_unrendered_placeholders(tasks: Path) -> None:
    for task in tasks.iterdir():
        for name in ("task.toml", "environment/Dockerfile", "solution/solve.sh"):
            text = (task / name).read_text()
            for key in ("{task_id}", "{agent_seed}", "{eval_seed}", "{quality_seed}", "{vllm_version}"):
                assert key not in text, (name, key)
        assert "{instruction}" not in (task / "instruction.md").read_text()


def test_task_config(tasks: Path) -> None:
    config = tomllib.loads((tasks / "inferencebench-b-mistral-7b-248-428" / "task.toml").read_text())
    assert config["environment"]["gpus"] == 1
    assert config["environment"]["gpu_types"] == ["H100!", "H100"]
    assert config["verifier"]["env"]["ANTHROPIC_BASE_URL"] == "${ANTHROPIC_BASE_URL:-}"
    assert config["agent"]["user"] == "agent"
    assert config["agent"]["timeout_sec"] == 7200.0
    assert config["verifier"]["environment_mode"] == "shared"
    assert config["environment"]["env"]["INFERENCE_BENCH_DATASET_SEED"] == "248"
    assert config["verifier"]["env"]["INFERENCE_BENCH_DATASET_SEED"] == "428"
    assert config["verifier"]["collect"][0]["command"].startswith("/bin/bash ")


def test_agent_image_has_no_evaluation_seed_or_verifier_files(tasks: Path) -> None:
    env = tasks / "inferencebench-a-mistral-7b-21-1337" / "environment"
    assert "1337" not in (env / "Dockerfile").read_text()
    for path in (env / "home").rglob("*"):
        if path.is_file():
            assert "1337" not in path.read_text(errors="ignore"), path
    assert not list(env.rglob("test.sh"))
    assert not list(env.rglob("solve.sh"))


def test_root_commands_do_not_resolve_through_the_agents_directory(tasks: Path) -> None:
    dockerfile = (tasks / "inferencebench-a-mistral-7b-21-1337" / "environment" / "Dockerfile").read_text()
    final_path = [line for line in dockerfile.splitlines() if line.startswith("ENV PATH=")][-1]
    assert final_path.index("/usr/bin") < final_path.index("/home/agent/.local/bin")
    # No later ENV line may reorder it.
    assert dockerfile.rindex('ENV PATH="') == dockerfile.index(final_path)
    for name in ("environment/collect.sh", "tests/test.sh"):
        script = (tasks / "inferencebench-a-mistral-7b-21-1337" / name).read_text()
        assert 'export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"' in script
        assert "cd /\n" in script


def test_judge_prompt_points_at_the_copy(tasks: Path) -> None:
    prompt = (
        tasks
        / "inferencebench-a-mistral-7b-21-1337"
        / "tests"
        / "ib"
        / "src"
        / "disallowed_usage_judge"
        / "prompt.txt"
    ).read_text()
    assert "/home/agent" not in prompt
    assert "/var/lib/ib_judge/task" in prompt
    assert "/var/lib/ib_judge/contamination_judgement.txt" in prompt


def test_scripts_parse(tasks: Path) -> None:
    task = tasks / "inferencebench-a-mistral-7b-21-1337"
    for name in ("environment/collect.sh", "tests/test.sh", "solution/solve.sh"):
        subprocess.run(["bash", "-n", str(task / name)], check=True)


def run_retry_loop(tasks: Path, seconds_left: int, evaluator_rc: int) -> tuple[str, list[str]]:
    """Run test.sh's retry functions with stand-ins for the server and the evaluator."""
    script = (tasks / "inferencebench-a-mistral-7b-21-1337" / "tests" / "test.sh").read_text()
    functions = script[script.index("run_eval_attempt() {") : script.index('echo "=== InferenceBench verifier')]
    # run_eval_attempt sends the evaluator's output to EVAL_LOG, so the stand-in reports on fd 3.
    harness = f"""
exec 3>&1
EVAL_DEADLINE=$(( $(date +%s) + {seconds_left} ))
LOGS=/tmp; SCENARIO=x; EVAL_LOG=/dev/null; IB=/tmp; INFERENCE_BENCH_SERVER_URL=u; INFERENCE_BENCH_QUALITY_TAU=0.95
server_ready() {{ return 0; }}
start_server() {{ return 0; }}
log() {{ :; }}
timeout() {{ echo "limit=$3" >&3; return {evaluator_rc}; }}
{functions}
FINAL_EVAL_RC=1
run_eval_with_retry 3
echo "loop=$? final=$FINAL_EVAL_RC"
"""
    output = subprocess.run(["bash", "-c", harness], check=True, capture_output=True, text=True).stdout
    lines = output.splitlines()
    return lines[-1], lines[:-1]


def test_evaluation_attempts_are_limited_only_by_the_verifier_deadline(tasks: Path) -> None:
    summary, attempts = run_retry_loop(tasks, seconds_left=30000, evaluator_rc=124)
    assert summary == "loop=1 final=124"
    assert len(attempts) == 3
    # vLLM's default settings need about 8600 s for scenario B.
    assert all(int(line.removeprefix("limit=")) > 8600 for line in attempts)


def test_no_evaluation_starts_after_the_verifier_deadline(tasks: Path) -> None:
    summary, attempts = run_retry_loop(tasks, seconds_left=-1, evaluator_rc=0)
    assert summary == "loop=1 final=1"
    assert attempts == []


def test_evaluation_deadline_leaves_time_to_write_the_reward(tasks: Path) -> None:
    task = tasks / "inferencebench-a-mistral-7b-21-1337"
    limit = tomllib.loads((task / "task.toml").read_text())["verifier"]["timeout_sec"]
    assert "+ 41400 ))" in (task / "tests" / "test.sh").read_text()
    # One server start (900 s) can begin just before the deadline.
    assert limit - 41400 > 900


@pytest.fixture
def write_reward(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Load write_reward.py with a stand-in for upstream's primary_metric."""
    hpo = types.ModuleType("src.eval.inference.hpo_search_baselines")
    hpo.FAILURE_SCORE = 1e-6
    hpo.SCENARIO_TO_SHORT = {task: key for key, task in SCENARIO_TASKS.items()}
    hpo.primary_metric = lambda metrics, scenario: (metrics["primary"], "test")
    for name in ("src", "src.eval", "src.eval.inference"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, hpo.__name__, hpo)
    spec = importlib.util.spec_from_file_location("write_reward", TEMPLATE_TESTS / "write_reward.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_run(logs: Path, primary: float, *, speed=True, quality=True, flagged=False, judged=True) -> None:
    (logs / "metrics.json").write_text(
        json.dumps(
            {"primary": primary, "speed_check": {"pass": speed}, "quality_check": {"pass": quality}}
        )
    )
    if judged:
        (logs / "contamination_judgement.txt").write_text(
            ("contamination detected" if flagged else "no contamination detected") + "\n"
        )
        (logs / "disallowed_model_judgement.txt").write_text("only allowed use detected\n")


@pytest.mark.parametrize(
    ("scenario", "primary", "expected"),
    [
        # Claude Sonnet 4.6: Table 12 in native units, Table 2 as speedups.
        ("a", 1 / 0.24207, 3.47),
        ("b", 1 / 0.00341, 12.03),
        ("c", 4.48, 33.93),
        ("d", 5.06, 3.01),
    ],
)
def test_baselines_reproduce_the_papers_table(write_reward, tmp_path, scenario, primary, expected) -> None:
    write_run(tmp_path, primary)
    rewards = write_reward.rewards_for(tmp_path, SCENARIO_TASKS[scenario], "0", "0")
    assert rewards["valid"] == 1
    assert rewards["judge_valid"] == 1
    assert rewards["reward"] == pytest.approx(expected, abs=0.02)


@pytest.mark.parametrize(
    ("kwargs", "eval_rc"),
    [
        ({"quality": False}, "0"),
        ({"speed": False}, "0"),
        ({"flagged": True}, "0"),
        ({}, "1"),
    ],
)
def test_failed_runs_count_as_the_baseline(write_reward, tmp_path, kwargs, eval_rc) -> None:
    write_run(tmp_path, 100.0, **kwargs)
    rewards = write_reward.rewards_for(tmp_path, SCENARIO_TASKS["c"], eval_rc, "0")
    assert rewards["reward"] == 1.0
    assert rewards["valid"] == 0


def test_missing_metrics_count_as_the_baseline(write_reward, tmp_path) -> None:
    rewards = write_reward.rewards_for(tmp_path, SCENARIO_TASKS["a"], "1", "1")
    assert rewards["reward"] == 1.0
    assert rewards["speedup"] == 0
    assert rewards["judge_valid"] == 0


def test_missing_verdicts_are_visible_but_do_not_fail_the_run(write_reward, tmp_path) -> None:
    write_run(tmp_path, 4.0, judged=False)
    rewards = write_reward.rewards_for(tmp_path, SCENARIO_TASKS["c"], "0", "1")
    assert rewards["valid"] == 1
    assert rewards["judge_valid"] == 0
    assert rewards["contamination_detected"] == 0


def test_a_slower_valid_server_keeps_its_measured_speedup(write_reward, tmp_path) -> None:
    write_run(tmp_path, 0.066)
    rewards = write_reward.rewards_for(tmp_path, SCENARIO_TASKS["c"], "0", "0")
    assert rewards["reward"] == pytest.approx(0.5)


def test_score_reproduces_the_papers_aggregate(tmp_path: Path) -> None:
    # Claude Sonnet 4.6 per-scenario means from Table 2.
    means = {"a": 3.47, "b": 12.03, "c": 33.93, "d": 3.01}
    for scenario, value in means.items():
        for pair, offset in (("21-1337", -0.5), ("248-428", 0.0), ("999-777", 0.5)):
            trial = tmp_path / f"{scenario}{pair}"
            trial.mkdir()
            (trial / "result.json").write_text(
                json.dumps(
                    {
                        "task_name": f"aisa-group/inferencebench-{scenario}-mistral-7b-{pair}",
                        "verifier_result": {
                            "rewards": {"reward": value + offset, "valid": 1, "judge_valid": 1}
                        },
                    }
                )
            )
    summary = summarize(load_rewards(tmp_path))
    assert summary["n_trials"] == 12
    assert summary["scenarios"]["a"]["mean"] == pytest.approx(3.47)
    assert summary["scenarios"]["a"]["sem"] == pytest.approx(0.2887, abs=0.001)
    assert summary["aggregate"]["mean"] == pytest.approx(8.08, abs=0.01)
    assert len(summary["aggregate"]["seed_pairs"]) == 3


def test_score_ignores_unfinished_trials(tmp_path: Path) -> None:
    (tmp_path / "result.json").write_text(json.dumps({"task_name": "inferencebench-a-mistral-7b-21-1337"}))
    assert load_rewards(tmp_path) == []


class TestContinuationLoop:
    @pytest.fixture
    def agent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        pytest.importorskip("harbor")
        from inferencebench import agents

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        agent = agents.ClaudeCodeUntilDeadline(
            logs_dir=tmp_path, model_name="anthropic/claude-sonnet-4-6"
        )
        self.clock = 0.0
        monkeypatch.setattr(agents.time, "monotonic", lambda: self.clock)
        self.commands: list[tuple[str, dict]] = []

        async def exec_as_agent(environment, command, env=None, cwd=None, timeout_sec=None):
            self.commands.append((command, env or {}))
            return types.SimpleNamespace(stdout="600\n", return_code=0)

        monkeypatch.setattr(agent, "exec_as_agent", exec_as_agent)
        monkeypatch.setattr(agent, "_upload_base_settings", AsyncMock())
        return agent

    def claude_commands(self) -> list[tuple[str, dict]]:
        return [(c, e) for c, e in self.commands if "claude --verbose" in c]

    async def _sessions(self, agent, durations, failures=()) -> None:
        from harbor.agents.installed.claude_code import ClaudeCode

        real_run = ClaudeCode.run
        calls = iter(durations)
        count = {"n": 0}

        async def timed_run(inner, instruction, environment, context):
            index = count["n"]
            count["n"] += 1
            await real_run(inner, instruction, environment, context)
            self.clock += next(calls)
            if index in failures:
                from harbor.agents.installed.base import NonZeroAgentExitCodeError

                raise NonZeroAgentExitCodeError("boom")

        import unittest.mock as mock

        with mock.patch.object(ClaudeCode, "run", timed_run):
            await agent.run("the prompt", mock.Mock(), mock.Mock())

    @pytest.mark.asyncio
    async def test_continues_until_the_budget_ends(self, agent) -> None:
        await self._sessions(agent, [300, 200, 200])
        commands = self.claude_commands()
        assert len(commands) == 3
        assert "--continue" not in commands[0][0]
        assert all("--continue" in command for command, _ in commands[1:])
        prompts = [
            value for _, env in commands for key, value in env.items() if key.startswith("CLAUDE_CODE_INSTRUCTION")
        ]
        assert prompts[0] == "the prompt"
        assert prompts[1] == (
            "Continue where you left off and complete the task. You have approximately 5 minutes "
            "(300 seconds) of wall-clock time remaining. Make sure you utilize this time fully to "
            "achieve the best results you can. Do not ask for user feedback."
        )
        assert "claude-code.2.txt" in " ".join(command for command, _ in self.commands)

    @pytest.mark.asyncio
    async def test_stops_after_the_third_crash(self, agent) -> None:
        from harbor.agents.installed.base import NonZeroAgentExitCodeError

        with pytest.raises(NonZeroAgentExitCodeError):
            await self._sessions(agent, [10, 10, 10, 10], failures={0, 1, 2})
        assert len(self.claude_commands()) == 3

    @pytest.mark.asyncio
    async def test_does_not_restart_a_crash_near_the_deadline(self, agent) -> None:
        from harbor.agents.installed.base import NonZeroAgentExitCodeError

        with pytest.raises(NonZeroAgentExitCodeError):
            await self._sessions(agent, [550], failures={0})
        assert len(self.claude_commands()) == 1
