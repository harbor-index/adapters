"""Comprehensive unit, security, multi-stage controller, and native-vs-Harbor equivalence tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agencybench.adapter import AgencyBenchAdapter
from agencybench.controller import ScenarioController
from agencybench.registry import (
    PILOT_TASK_IDS,
    SCENARIO_BY_TASK_ID,
    SCENARIO_CATALOG,
    select_scenarios,
)
from agencybench.sandbox_io import (
    WorkspaceSecurityError,
    hash_directory,
    safe_copy_backend_filtered,
    safe_copy_full,
)
from agencybench.verifier import VerifierSecurityError, load_upstream_module, verify_scenario

PROJECT_ROOT = Path(__file__).resolve().parents[4]
if (PROJECT_ROOT / "tools/jdk-17/bin").is_dir():
    os.environ["PATH"] = f"{PROJECT_ROOT / 'tools/jdk-17/bin'}:" + os.environ.get("PATH", "")
if (PROJECT_ROOT / "tmp_work").is_dir():
    os.environ["TMPDIR"] = str(PROJECT_ROOT / "tmp_work")
    os.environ["AGENCYBENCH_TMPDIR"] = str(PROJECT_ROOT / "tmp_work")

UPSTREAM_ROOT = Path(
    os.environ.get(
        "AGENCYBENCH_UPSTREAM_ROOT",
        str(PROJECT_ROOT / "upstream/AgencyBench"),
    )
)


# --------------------------------------------------------------------------- #
# 1. Catalog, deterministic IDs, and idempotent generation
# --------------------------------------------------------------------------- #


def test_catalog_counts_match_v2_spec() -> None:
    assert len(SCENARIO_CATALOG) == 32
    assert sum(s.subtask_count for s in SCENARIO_CATALOG) == 138
    task_ids = [s.task_id for s in SCENARIO_CATALOG]
    assert len(task_ids) == len(set(task_ids))
    assert len(PILOT_TASK_IDS) == 3


def test_deterministic_clean_generation_twice(tmp_path: Path) -> None:
    out1 = tmp_path / "gen1"
    out2 = tmp_path / "gen2"
    a1 = AgencyBenchAdapter(out1, split="pilot", upstream_dir=UPSTREAM_ROOT, overwrite=True)
    a2 = AgencyBenchAdapter(out2, split="pilot", upstream_dir=UPSTREAM_ROOT, overwrite=True)
    p1 = a1.run()
    p2 = a2.run()
    assert [p.name for p in p1] == list(PILOT_TASK_IDS)
    assert [p.name for p in p2] == list(PILOT_TASK_IDS)
    assert hash_directory(out1) == hash_directory(out2)


def test_task_flags_limit_and_task_ids(tmp_path: Path) -> None:
    limited = select_scenarios(split="pilot", limit=2)
    assert len(limited) == 2
    by_upstream = select_scenarios(split="all", task_ids=["Backend/scenario1", "MCP/scenario2"])
    assert [s.task_id for s in by_upstream] == [
        "agencybench-backend-scenario-01",
        "agencybench-mcp-scenario-02",
    ]


# --------------------------------------------------------------------------- #
# 2. Prompt visibility, secrets/solution isolation, and symlink security
# --------------------------------------------------------------------------- #


def test_prompt_visibility_and_isolation(tmp_path: Path) -> None:
    out = tmp_path / "gen"
    AgencyBenchAdapter(out, split="pilot", upstream_dir=UPSTREAM_ROOT, overwrite=True).run()

    for tid in PILOT_TASK_IDS:
        tdir = out / tid
        spec = SCENARIO_BY_TASK_ID[tid]
        inst = (tdir / "instruction.md").read_text(encoding="utf-8")
        desc = json.loads((tdir / "tests/upstream/description.json").read_text(encoding="utf-8"))

        # Stage 1 prompt MUST appear in instruction.md; Stage 2..5 prompts MUST NOT leak!
        assert f"<!-- agencybench_task_id: {tid} -->" in inst
        sub2 = str(desc["subtask2"])
        # Check a distinctive snippet from subtask2 is NOT leaked into Stage 1 instruction
        if tid == "agencybench-backend-scenario-01":
            assert "--selftest friend-menu" not in inst
            assert "test_queue" not in inst
            assert "--version" in inst
        elif tid == "agencybench-code-scenario-01":
            assert "subtask2_rationale.md" not in inst
            assert "subtask1_rationale.md" in inst
        elif tid == "agencybench-mcp-scenario-02":
            assert "Step 2 – Smart move" not in inst
            assert "Step 1 – Build workspace_v2 shell" in inst

        # Environment directory must NEVER contain eval_task.py, description.json, .env, or solutions
        env_files = {p.name for p in (tdir / "environment").rglob("*")}
        assert "eval_task.py" not in env_files
        assert "description.json" not in env_files
        assert ".env" not in env_files
        assert "meta_eval.json" not in env_files
        assert "solve.sh" not in env_files
        assert "__pycache__" not in env_files


def test_copy_filters_and_symlink_escape_rejection(tmp_path: Path) -> None:
    src = tmp_path / "ws"
    dst = tmp_path / "eval"
    src.mkdir()
    (src / "Makefile").write_text("all:\n", encoding="utf-8")
    (src / "meta_eval.json").write_text('{"score": 10}', encoding="utf-8")
    (src / "notes.txt").write_text("excluded", encoding="utf-8")
    (src / "logs").mkdir()
    (src / "logs/foo.log").write_text("excluded", encoding="utf-8")
    (src / "bin").mkdir()
    (src / "bin/chat_app").write_text("excluded", encoding="utf-8")

    copied = safe_copy_backend_filtered(src, dst)
    assert copied == ["Makefile"]
    assert not (dst / "meta_eval.json").exists()
    assert not (dst / "notes.txt").exists()
    assert not (dst / "logs").exists()
    assert not (dst / "bin").exists()

    # Symlink escaping workspace root must raise WorkspaceSecurityError
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("TOP_SECRET", encoding="utf-8")
    (src / "leak.cpp").symlink_to(secret)
    with pytest.raises(WorkspaceSecurityError):
        safe_copy_backend_filtered(src, dst)
    with pytest.raises(WorkspaceSecurityError):
        safe_copy_full(src, dst)


# --------------------------------------------------------------------------- #
# 3. Real Stage-by-Stage Oracle, NOP, Adversarial & Native Equivalence Tests
# --------------------------------------------------------------------------- #


def _run_solve_sh(task_dir: Path, workspace: Path, stages_dir: Path) -> None:
    env = os.environ.copy()
    env["AGENCYBENCH_WORKSPACE"] = str(workspace)
    env["AGENCYBENCH_STAGES_DIR"] = str(stages_dir)
    env["AGENCYBENCH_PYTHON"] = shutil.which("python3") or sys.executable
    subprocess.run(
        ["bash", str(task_dir / "solution/solve.sh")],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_backend_s1_oracle_nop_spoof_and_native_equivalence(tmp_path: Path) -> None:
    gen = tmp_path / "gen"
    AgencyBenchAdapter(
        gen,
        task_ids=["agencybench-backend-scenario-01"],
        upstream_dir=UPSTREAM_ROOT,
        overwrite=True,
    ).run()
    tdir = gen / "agencybench-backend-scenario-01"
    eval_script = tdir / "tests/upstream/eval_task.py"
    desc_path = tdir / "tests/upstream/description.json"

    # A. Oracle (stage-by-stage) -> 10/10 on all 5 subtasks -> reward == 1.0
    ws = tmp_path / "oracle_ws"
    stages = tmp_path / "oracle_stages"
    ws.mkdir()
    _run_solve_sh(tdir, ws, stages)

    harbor_res = verify_scenario(
        runner_family="backend_cpp_rubric",
        upstream_eval_script=eval_script,
        description_path=desc_path,
        workspace=ws,
        stages_dir=stages,
    )
    assert harbor_res["reward"] == 1.0
    assert harbor_res["subtask_scores_raw"] == [10.0, 10.0, 10.0, 10.0, 10.0]

    # Native upstream direct replay on identical oracle stage snapshots -> EXACT score equality
    mod = load_upstream_module(eval_script, "native_backend_s1")
    rubric = mod.RubricEvaluator()
    native_scores = []
    for i in range(1, 6):
        ev = tmp_path / f"native_ev_{i}"
        mod.copy_workspace_filtered(stages / f"subtask{i}", ev)
        runner = mod.CommandRunner(mod.ensure_directory(ev / "logs"))
        cmds = runner.capture(f"subtask{i}", ev)
        native_scores.append(float(rubric.evaluate(f"subtask{i}", ev, cmds).score))
    assert native_scores == harbor_res["subtask_scores_raw"]

    # B. NOP negative control -> 0.0
    nop_ws = tmp_path / "nop_ws"
    nop_ws.mkdir()
    nop_res = verify_scenario(
        runner_family="backend_cpp_rubric",
        upstream_eval_script=eval_script,
        description_path=desc_path,
        workspace=nop_ws,
        stages_dir=None,
    )
    assert nop_res["reward"] == 0.0
    assert nop_res["subtask_scores_raw"] == [0.0, 0.0, 0.0, 0.0, 0.0]

    # C. Adversarial: spoofed meta_eval.json & fake reward file in workspace MUST still score 0.0
    spoof_ws = tmp_path / "spoof_ws"
    spoof_ws.mkdir()
    (spoof_ws / "meta_eval.json").write_text('{"final_score": 10, "subtasks": [10,10,10,10,10]}')
    spoof_res = verify_scenario(
        runner_family="backend_cpp_rubric",
        upstream_eval_script=eval_script,
        description_path=desc_path,
        workspace=spoof_ws,
        stages_dir=None,
    )
    assert spoof_res["reward"] == 0.0

    # D. Adversarial: escaping symlink fails closed with VerifierSecurityError
    sym_ws = tmp_path / "sym_ws"
    sym_ws.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    (sym_ws / "evil.cpp").symlink_to(outside)
    with pytest.raises(VerifierSecurityError):
        verify_scenario(
            runner_family="backend_cpp_rubric",
            upstream_eval_script=eval_script,
            description_path=desc_path,
            workspace=sym_ws,
            stages_dir=None,
        )


def test_code_s1_oracle_nop_partial_and_native_equivalence(tmp_path: Path) -> None:
    gen = tmp_path / "gen"
    AgencyBenchAdapter(
        gen,
        task_ids=["agencybench-code-scenario-01"],
        upstream_dir=UPSTREAM_ROOT,
        overwrite=True,
    ).run()
    tdir = gen / "agencybench-code-scenario-01"
    eval_script = tdir / "tests/upstream/eval_task.py"
    desc_path = tdir / "tests/upstream/description.json"

    # A. Oracle (stage-by-stage) -> 10.0 on all 5 subtasks -> reward == 1.0
    ws = tmp_path / "code_ws"
    stages = tmp_path / "code_stages"
    safe_copy_full(tdir / "environment/source", ws)
    _run_solve_sh(tdir, ws, stages)

    harbor_res = verify_scenario(
        runner_family="code_mse_rubric",
        upstream_eval_script=eval_script,
        description_path=desc_path,
        workspace=ws,
        stages_dir=stages,
    )
    assert harbor_res["reward"] == 1.0
    assert harbor_res["subtask_scores_raw"] == [10.0, 10.0, 10.0, 10.0, 10.0]

    # Prove final snapshot alone FAILS subtask1 (nonlinear fails _is_first_order_linear)
    final_only_res = verify_scenario(
        runner_family="code_mse_rubric",
        upstream_eval_script=eval_script,
        description_path=desc_path,
        workspace=ws,
        stages_dir=None,
    )
    assert final_only_res["subtask_scores_raw"][0] == 0.0
    assert final_only_res["reward"] < 1.0

    # Native upstream direct replay -> exact score equality
    mod = load_upstream_module(eval_script, "native_code_s1")
    rubric = mod.RubricEvaluator()
    native_scores = []
    for i in range(1, 6):
        ev = tmp_path / f"native_code_ev_{i}"
        mod.copy_workspace(stages / f"subtask{i}", ev)
        runner = mod.CommandRunner(mod.ensure_directory(ev / "logs"))
        cmds = runner.capture(f"subtask{i}", ev)
        native_scores.append(float(rubric.evaluate(f"subtask{i}", ev, cmds).score))
    assert native_scores == harbor_res["subtask_scores_raw"]

    # B. NOP negative control (untouched source/ files):
    # Initial linear equation.py already reaches MSE=6.17e-4 <= 1e-1 on subtask1,
    # missing only docs/subtask1_rationale.md (-5 penalty) -> legitimate baseline 5.0/10 on subtask1 (reward 0.1).
    # Must match native upstream scoring identically (never force NOP to 0 by altering scoring!).
    nop_ws = tmp_path / "code_nop"
    safe_copy_full(tdir / "environment/source", nop_ws)
    nop_res = verify_scenario(
        runner_family="code_mse_rubric",
        upstream_eval_script=eval_script,
        description_path=desc_path,
        workspace=nop_ws,
        stages_dir=None,
    )
    assert nop_res["subtask_scores_raw"] == [5.0, 0.0, 0.0, 0.0, 0.0]
    assert nop_res["reward"] == pytest.approx(0.1)
    assert nop_res["reward"] < 1.0
    # Verify exact native upstream equivalence on NOP as well
    native_nop_scores = []
    for i in range(1, 6):
        ev = tmp_path / f"native_code_nop_{i}"
        mod.copy_workspace(nop_ws, ev)
        runner = mod.CommandRunner(mod.ensure_directory(ev / "logs"))
        cmds = runner.capture(f"subtask{i}", ev)
        native_nop_scores.append(float(rubric.evaluate(f"subtask{i}", ev, cmds).score))
    assert native_nop_scores == nop_res["subtask_scores_raw"]


def test_mcp_s2_oracle_nop_partial_halt_and_controller(tmp_path: Path) -> None:
    gen = tmp_path / "gen"
    AgencyBenchAdapter(
        gen,
        task_ids=["agencybench-mcp-scenario-02"],
        upstream_dir=UPSTREAM_ROOT,
        overwrite=True,
    ).run()
    tdir = gen / "agencybench-mcp-scenario-02"
    eval_script = tdir / "tests/upstream/eval_task.py"
    desc_path = tdir / "tests/upstream/description.json"

    # A. Oracle (stage-by-stage) -> 10.0 on all 5 subtasks -> reward == 1.0
    ws = tmp_path / "mcp_ws"
    stages = tmp_path / "mcp_stages"
    safe_copy_full(tdir / "environment/desktop", ws / "desktop")
    (ws / "desktop_2").mkdir(parents=True, exist_ok=True)
    _run_solve_sh(tdir, ws, stages)

    harbor_res = verify_scenario(
        runner_family="mcp_filesystem",
        upstream_eval_script=eval_script,
        description_path=desc_path,
        workspace=ws,
        stages_dir=stages,
    )
    assert harbor_res["reward"] == 1.0
    assert harbor_res["subtask_scores_raw"] == [10.0, 10.0, 10.0, 10.0, 10.0]

    # B. NOP negative control -> 0.0 (fails subtask1 and halts remaining)
    nop_ws = tmp_path / "mcp_nop"
    safe_copy_full(tdir / "environment/desktop", nop_ws / "desktop")
    (nop_ws / "desktop_2").mkdir(parents=True, exist_ok=True)
    nop_res = verify_scenario(
        runner_family="mcp_filesystem",
        upstream_eval_script=eval_script,
        description_path=desc_path,
        workspace=nop_ws,
        stages_dir=None,
    )
    assert nop_res["reward"] == 0.0
    assert nop_res["subtasks"][1]["source_kind"] == "skipped_after_halt"

    # C. Multi-stage ScenarioController test: fail attempt 1 of subtask1, succeed on attempt 2 with feedback,
    #    then fail subtask2 on both attempts -> halts subtasks 3..5!
    seen_prompts: list[tuple[str, int, str]] = []

    class PartialMcpTransport:
        def send(self, *, subtask: str, attempt: int, prompt: str, workspace_notice: str, workspace: Path) -> str:
            seen_prompts.append((subtask, attempt, prompt))
            if subtask == "subtask1" and attempt == 2:
                # Feedback from attempt 1 must be present!
                assert "Previous attempt feedback:" in prompt
                assert "Missing directories" in prompt
                for rel in (
                    "desktop_2/workspace_v2/dev_bundle/tests",
                    "desktop_2/workspace_v2/dev_bundle/source",
                    "desktop_2/workspace_v2/data_warehouse/legacy_archives",
                    "desktop_2/workspace_v2/data_warehouse/active_datasets",
                    "desktop_2/workspace_v2/knowledge_base",
                ):
                    (workspace / rel).mkdir(parents=True, exist_ok=True)
            return f"ran {subtask} attempt {attempt}"

    spec = SCENARIO_BY_TASK_ID["agencybench-mcp-scenario-02"]
    ctrl = ScenarioController(
        spec=spec,
        upstream_scenario_dir=UPSTREAM_ROOT / spec.upstream_rel_path,
        run_root=tmp_path / "ctrl_mcp",
        max_attempts=2,
    )
    ctrl_res = ctrl.run(PartialMcpTransport())
    # Subtask 1 passed on attempt 2 -> 10.0; Subtask 2 failed -> 0.0; Subtasks 3..5 skipped -> 0.0
    assert [s.best_score for s in ctrl_res.subtasks] == [10.0, 0.0, 0.0, 0.0, 0.0]
    assert [s.skipped for s in ctrl_res.subtasks] == [False, False, True, True, True]
    assert ctrl_res.reward == pytest.approx(0.2)
    assert ctrl_res.pass_at_1 == pytest.approx(0.0)
    assert ctrl_res.pass_at_2 == pytest.approx(0.2)
    # Prompt visibility: Stage 1 prompt must NOT mention Step 2
    assert "Step 2 – Smart move" not in seen_prompts[0][2]

    # Independent verifier replay on controller's stages_dir must match controller reward EXACTLY!
    replayed = verify_scenario(
        runner_family="mcp_filesystem",
        upstream_eval_script=eval_script,
        description_path=desc_path,
        workspace=tmp_path / "ctrl_mcp/workspace",
        stages_dir=ctrl_res.stages_dir,
    )
    assert replayed["reward"] == ctrl_res.reward
    assert replayed["subtask_scores_raw"] == [s.best_score for s in ctrl_res.subtasks]


def test_backend_controller_preserves_cache_first_quirk(tmp_path: Path) -> None:
    """Verify Backend/scenario1 preserves exact upstream cache_workspace retention quirk."""
    spec = SCENARIO_BY_TASK_ID["agencybench-backend-scenario-01"]

    class TwoAttemptPartialTransport:
        def send(self, *, subtask: str, attempt: int, prompt: str, workspace_notice: str, workspace: Path) -> str:
            if subtask == "subtask1":
                (workspace / "marker.txt").write_text(f"attempt_{attempt}", encoding="utf-8")
                (workspace / "attempt_tag.cpp").write_text(f"// attempt_{attempt}\n", encoding="utf-8")
            elif subtask == "subtask2" and attempt == 1:
                # Because attempt 1 & attempt 2 both scored 0 (<10), Backend/scenario1's
                # `cache_workspace = cache_workspace or workspace` MUST carry attempt_01 forward!
                assert (workspace / "attempt_tag.cpp").read_text().strip() == "// attempt_1"
            return "ok"

    ctrl = ScenarioController(
        spec=spec,
        upstream_scenario_dir=UPSTREAM_ROOT / spec.upstream_rel_path,
        run_root=tmp_path / "ctrl_backend_quirk",
        max_attempts=2,
    )
    res = ctrl.run(TwoAttemptPartialTransport())
    assert res.subtasks[0].selected_workspace is not None
    assert res.subtasks[0].selected_workspace.name == "attempt_01"



def test_all_9_implemented_scenarios_generation_and_no_rubric_leakage(tmp_path: Path) -> None:
    """Verify all 9 deterministic scenarios (37 subtasks across 4 categories) generate cleanly and never leak rubrics."""
    out = tmp_path / "gen_implemented"
    paths = AgencyBenchAdapter(out, split="implemented", upstream_dir=UPSTREAM_ROOT, overwrite=True).run()
    assert len(paths) == 9
    specs = select_scenarios(split="implemented")
    assert sum(s.subtask_count for s in specs) == 37

    # Research/scenario2 description.json contains ground-truth answers in 'rubric'; verify instruction.md NEVER leaks them!
    r2_inst = (out / "agencybench-research-scenario-02" / "instruction.md").read_text(encoding="utf-8")
    assert "Paul George" not in r2_inst
    assert "Shai Gilgeous-Alexander" not in r2_inst
    assert "subtask1.md" in r2_inst


def test_code_loss_single_anti_spoofing_and_oracle(tmp_path: Path) -> None:
    """Verify Code/scenario6 & 07 oracle scores 1.0, NOP scores 0.0, and spoofing subtask1_result.json without modifying equation.py scores 0.0."""
    out = tmp_path / "gen"
    AgencyBenchAdapter(
        out,
        split="implemented",
        task_ids=["agencybench-code-scenario-06", "agencybench-code-scenario-07"],
        upstream_dir=UPSTREAM_ROOT,
        overwrite=True,
    ).run()

    for tid in ("agencybench-code-scenario-06", "agencybench-code-scenario-07"):
        tdir = out / tid
        eval_script = tdir / "tests/upstream/eval_task.py"
        desc_path = tdir / "tests/upstream/description.json"

        # 1. Spoofing test: untouched baseline equation.py + fake loss=1e-12 in subtask1_result.json
        spoof_ws = tmp_path / f"{tid}_spoof"
        safe_copy_full(tdir / "environment/workspace", spoof_ws)
        (spoof_ws / "subtask1_result.json").write_text(
            json.dumps({"loss": 1e-12, "equation": "spoofed", "comment": "spoofed"}),
            encoding="utf-8",
        )
        spoof_res = verify_scenario(
            runner_family="code_loss_single",
            upstream_eval_script=eval_script,
            description_path=desc_path,
            workspace=spoof_ws,
            stages_dir=None,
        )
        assert spoof_res["reward"] == 0.0
        assert spoof_res["subtasks"][0]["measured_loss"] > 1e-3

        # 2. Oracle test -> 1.0
        ws = tmp_path / f"{tid}_oracle_ws"
        stages = tmp_path / f"{tid}_oracle_stages"
        safe_copy_full(tdir / "environment/workspace", ws)
        _run_solve_sh(tdir, ws, stages)
        oracle_res = verify_scenario(
            runner_family="code_loss_single",
            upstream_eval_script=eval_script,
            description_path=desc_path,
            workspace=ws,
            stages_dir=stages,
        )
        assert oracle_res["reward"] == 1.0
        assert oracle_res["subtasks"][0]["effective_loss"] <= 1e-7


def test_phase2_backend_and_research_oracles_and_nops(tmp_path: Path) -> None:
    """Verify Backend/scenario2, Backend/scenario3, Research/scenario2, Research/scenario3 Oracles (1.0) and NOPs (0.0)."""
    target_ids = [
        "agencybench-backend-scenario-02",
        "agencybench-backend-scenario-03",
        "agencybench-research-scenario-02",
        "agencybench-research-scenario-03",
    ]
    out = tmp_path / "gen_p2"
    AgencyBenchAdapter(
        out,
        split="implemented",
        task_ids=target_ids,
        upstream_dir=UPSTREAM_ROOT,
        overwrite=True,
    ).run()

    for tid in target_ids:
        spec = SCENARIO_BY_TASK_ID[tid]
        tdir = out / tid
        eval_script = tdir / "tests/upstream/eval_task.py"
        desc_path = tdir / "tests/upstream/description.json"

        nop_ws = tmp_path / f"{tid}_nop"
        nop_ws.mkdir(parents=True, exist_ok=True)
        nop_res = verify_scenario(
            runner_family=spec.runner_family,
            upstream_eval_script=eval_script,
            description_path=desc_path,
            workspace=nop_ws,
            stages_dir=None,
        )
        assert nop_res["reward"] == 0.0

        ws = tmp_path / f"{tid}_ws"
        stages = tmp_path / f"{tid}_stages"
        ws.mkdir(parents=True, exist_ok=True)
        _run_solve_sh(tdir, ws, stages)
        oracle_res = verify_scenario(
            runner_family=spec.runner_family,
            upstream_eval_script=eval_script,
            description_path=desc_path,
            workspace=ws,
            stages_dir=stages,
        )
        assert oracle_res["reward"] == 1.0
        assert oracle_res["subtask_scores_raw"] == [10.0, 10.0, 10.0, 10.0, 10.0]


# --------------------------------------------------------------------------- #
# 4. AgencyBenchAgent & HarborContainerTransport contract tests
# --------------------------------------------------------------------------- #


def test_generated_test_sh_passes_report_json(tmp_path: Path) -> None:
    out = tmp_path / "gen_test_sh"
    AgencyBenchAdapter(out, split="pilot", upstream_dir=UPSTREAM_ROOT, overwrite=True).run()
    for tid in PILOT_TASK_IDS:
        script = (out / tid / "tests/test.sh").read_text(encoding="utf-8")
        assert "--report-json /logs/verifier/agencybench_report.json" in script


def test_harbor_container_transport_clears_deleted_files_and_supports_fixture(tmp_path: Path) -> None:
    import asyncio
    from types import SimpleNamespace
    from agencybench.agent import HarborContainerTransport

    container_ws = tmp_path / "container_ws"
    container_ws.mkdir()
    host_ws = tmp_path / "host_ws"
    host_ws.mkdir()
    (host_ws / "stale_file.txt").write_text("should be deleted", encoding="utf-8")
    (host_ws / "keep.txt").write_text("keep", encoding="utf-8")

    recorded_envs: list[dict[str, str] | None] = []
    recorded_cmds: list[str] = []

    class FakeEnv:
        async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None, **kwargs):
            recorded_cmds.append(command)
            if command.startswith("rm -rf /workspace"):
                for entry in container_ws.iterdir():
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
            elif command == "echo ok && rm -f stale_file.txt":
                recorded_envs.append(env)
                (container_ws / "stale_file.txt").unlink(missing_ok=True)
                (container_ws / "created.txt").write_text("new", encoding="utf-8")
            return SimpleNamespace(stdout="ok", stderr="", return_code=0)

        async def upload_dir(self, source_dir: str, target_dir: str):
            safe_copy_full(Path(source_dir), container_ws)

        async def upload_file(self, source_path: str, target_path: str):
            return None

        async def download_dir(self, source_dir: str, target_dir: str):
            safe_copy_full(container_ws, Path(target_dir))

    async def _run() -> str:
        loop = asyncio.get_running_loop()
        transport = HarborContainerTransport(
            loop=loop,
            environment=FakeEnv(),
            inner_agent="fixture",
            model_name="fixture-model",
            extra_env={"OPENAI_API_KEY": "test-dummy"},
            fixture_cmd="echo ok && rm -f stale_file.txt",
        )
        return await asyncio.to_thread(
            transport.send,
            subtask="subtask1",
            attempt=1,
            prompt="hello",
            workspace_notice="notice",
            workspace=host_ws,
        )

    out_text = asyncio.run(_run())
    assert "ok" in out_text
    assert not (host_ws / "stale_file.txt").exists(), "Deleted in-container file must not linger on host"
    assert (host_ws / "keep.txt").exists()
    assert (host_ws / "created.txt").read_text(encoding="utf-8") == "new"
    assert recorded_envs == [{"OPENAI_API_KEY": "test-dummy"}]


def test_agencybench_agent_end_to_end_with_mock_environment(tmp_path: Path) -> None:
    import asyncio
    from types import SimpleNamespace
    from agencybench.agent import AgencyBenchAgent

    out = tmp_path / "gen"
    AgencyBenchAdapter(
        out,
        split="pilot",
        task_ids=["agencybench-mcp-scenario-02"],
        upstream_dir=UPSTREAM_ROOT,
        overwrite=True,
    ).run()
    task_dir = out / "agencybench-mcp-scenario-02"
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")

    container_ws = tmp_path / "container_ws"
    container_stages = tmp_path / "container_stages"
    container_ws.mkdir()
    container_stages.mkdir()

    class MockHarborEnv:
        async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None, **kwargs):
            if command.startswith("rm -rf /workspace"):
                for entry in container_ws.iterdir():
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
            elif command.startswith("rm -rf /tmp/.agencybench_stages"):
                if container_stages.exists():
                    shutil.rmtree(container_stages)
                container_stages.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(stdout="", stderr="", return_code=0)

        async def upload_dir(self, source_dir: str, target_dir: str):
            dst = container_stages if target_dir == "/tmp/.agencybench_stages" else container_ws
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(source_dir, dst)

        async def upload_file(self, source_path: str, target_path: str):
            return None

        async def download_dir(self, source_dir: str, target_dir: str):
            dst = Path(target_dir)
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(container_ws, dst)

    class DummyAgent(AgencyBenchAgent):
        def __init__(self, logs_dir: Path, **kwargs):
            self.logs_dir = logs_dir
            self.model_name = "fixture-nop"
            self._extra_env = {}
            self._upstream_root = UPSTREAM_ROOT
            self._inner_agent = "fixture"
            self._max_attempts = 1
            self._fixture_cmd = "true"

    context = SimpleNamespace(metadata=None)
    agent = DummyAgent(logs_dir=tmp_path / "agent_logs")
    asyncio.run(agent.run(instruction, MockHarborEnv(), context))

    assert isinstance(context.metadata, dict)
    ctrl_meta = context.metadata["agencybench_controller"]
    assert ctrl_meta["task_id"] == "agencybench-mcp-scenario-02"
    assert ctrl_meta["reward"] == 0.0
    assert (container_stages / "subtask1").is_dir()

    # Independent Harbor verifier replay must match controller reward identically
    verify_res = verify_scenario(
        runner_family="mcp_filesystem",
        upstream_eval_script=task_dir / "tests/upstream/eval_task.py",
        description_path=task_dir / "tests/upstream/description.json",
        workspace=container_ws,
        stages_dir=container_stages,
    )
    assert verify_res["reward"] == ctrl_meta["reward"] == 0.0
