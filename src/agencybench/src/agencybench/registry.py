"""Pinned AgencyBench V2 scenario catalog, deterministic IDs, and split definitions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

UPSTREAM_REPO_URL = "https://github.com/GAIR-NLP/AgencyBench"
UPSTREAM_COMMIT = "ec65324be69e81bd4fe394ef6a86d48b8fa5da56"
UPSTREAM_HF_DATASET = "GAIR/AgencyBench"
UPSTREAM_HF_REVISION = "ea9008235da1864d00380f56e48c18270e84518c"
TASK_ORG = "gair-nlp"

RunnerFamily = Literal[
    "backend_cpp_rubric",
    "backend_java_rubric",
    "backend_py_rubric",
    "code_mse_rubric",
    "code_loss_single",
    "code_dind_patch",
    "code_llm_judge",
    "code_mongo_service",
    "mcp_filesystem",
    "mcp_github",
    "research_rubric",
    "research_bench",
    "visual_browser_sandbox",
]


@dataclass(frozen=True)
class ScenarioSpec:
    """Immutable metadata for one AgencyBench V2 scenario (= one Harbor task)."""

    scenario_id: str  # e.g. "Backend/scenario1"
    category: str  # Backend | Code | Frontend | Game | MCP | Research
    scenario_num: int
    subtask_count: int
    runner_family: RunnerFamily
    difficulty: str
    default_max_attempts: int
    early_break_rule: str
    carryover_rule: str
    copy_mode: Literal["filtered", "full", "in_place"]
    has_oracle: bool
    implemented: bool
    external_requirements: tuple[str, ...] = ()

    @property
    def task_id(self) -> str:
        return f"agencybench-{self.category.lower()}-scenario-{self.scenario_num:02d}"

    @property
    def registry_name(self) -> str:
        return f"{TASK_ORG}/{self.task_id}"

    @property
    def upstream_rel_path(self) -> Path:
        return Path("AgencyBench-v2") / self.category / f"scenario{self.scenario_num}"


# Full 32-scenario AgencyBench V2 catalog (138 subtasks total), deterministic order.
SCENARIO_CATALOG: tuple[ScenarioSpec, ...] = (
    # Backend (3 scenarios, 15 subtasks)
    ScenarioSpec(
        scenario_id="Backend/scenario1",
        category="Backend",
        scenario_num=1,
        subtask_count=5,
        runner_family="backend_cpp_rubric",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="score == 10",
        carryover_rule="cache_first_unless_perfect",
        copy_mode="filtered",
        has_oracle=True,
        implemented=True,
    ),
    ScenarioSpec(
        scenario_id="Backend/scenario2",
        category="Backend",
        scenario_num=2,
        subtask_count=5,
        runner_family="backend_java_rubric",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="score == 10",
        carryover_rule="cache_first_unless_perfect",
        copy_mode="filtered",
        has_oracle=True,
        implemented=True,
    ),
    ScenarioSpec(
        scenario_id="Backend/scenario3",
        category="Backend",
        scenario_num=3,
        subtask_count=5,
        runner_family="backend_py_rubric",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="score >= 0",
        carryover_rule="cache_first_unless_perfect",
        copy_mode="filtered",
        has_oracle=True,
        implemented=True,
    ),
    # Code (9 scenarios, 29 subtasks)
    ScenarioSpec(
        scenario_id="Code/scenario1",
        category="Code",
        scenario_num=1,
        subtask_count=5,
        runner_family="code_mse_rubric",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="score >= 9.9",
        carryover_rule="max_score_first_tie",
        copy_mode="full",
        has_oracle=True,
        implemented=True,
    ),
    ScenarioSpec(
        scenario_id="Code/scenario2",
        category="Code",
        scenario_num=2,
        subtask_count=1,
        runner_family="code_dind_patch",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="passed",
        carryover_rule="single_subtask",
        copy_mode="in_place",
        has_oracle=False,
        implemented=False,
        external_requirements=("nested_docker", "text_llm_judge"),
    ),
    ScenarioSpec(
        scenario_id="Code/scenario3",
        category="Code",
        scenario_num=3,
        subtask_count=1,
        runner_family="code_dind_patch",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="passed",
        carryover_rule="single_subtask",
        copy_mode="in_place",
        has_oracle=False,
        implemented=False,
        external_requirements=("nested_docker", "text_llm_judge"),
    ),
    ScenarioSpec(
        scenario_id="Code/scenario4",
        category="Code",
        scenario_num=4,
        subtask_count=5,
        runner_family="code_llm_judge",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="per_script",
        carryover_rule="per_script",
        copy_mode="full",
        has_oracle=False,
        implemented=False,
        external_requirements=("text_llm_judge",),
    ),
    ScenarioSpec(
        scenario_id="Code/scenario5",
        category="Code",
        scenario_num=5,
        subtask_count=5,
        runner_family="code_llm_judge",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="per_script",
        carryover_rule="per_script",
        copy_mode="full",
        has_oracle=False,
        implemented=False,
        external_requirements=("text_llm_judge",),
    ),
    ScenarioSpec(
        scenario_id="Code/scenario6",
        category="Code",
        scenario_num=6,
        subtask_count=1,
        runner_family="code_loss_single",
        difficulty="medium",
        default_max_attempts=2,
        early_break_rule="loss <= threshold",
        carryover_rule="in_place",
        copy_mode="in_place",
        has_oracle=True,
        implemented=True,
    ),
    ScenarioSpec(
        scenario_id="Code/scenario7",
        category="Code",
        scenario_num=7,
        subtask_count=1,
        runner_family="code_loss_single",
        difficulty="medium",
        default_max_attempts=2,
        early_break_rule="loss <= threshold",
        carryover_rule="in_place",
        copy_mode="in_place",
        has_oracle=True,
        implemented=True,
    ),
    ScenarioSpec(
        scenario_id="Code/scenario8",
        category="Code",
        scenario_num=8,
        subtask_count=5,
        runner_family="code_mongo_service",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="per_script",
        carryover_rule="per_script",
        copy_mode="filtered",
        has_oracle=False,
        implemented=False,
        external_requirements=("mongodb",),
    ),
    ScenarioSpec(
        scenario_id="Code/scenario9",
        category="Code",
        scenario_num=9,
        subtask_count=5,
        runner_family="code_mongo_service",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="per_script",
        carryover_rule="per_script",
        copy_mode="filtered",
        has_oracle=False,
        implemented=False,
        external_requirements=("mongodb", "nested_docker"),
    ),
    # Frontend (3 scenarios, 15 subtasks)
    *(
        ScenarioSpec(
            scenario_id=f"Frontend/scenario{n}",
            category="Frontend",
            scenario_num=n,
            subtask_count=5,
            runner_family="visual_browser_sandbox",
            difficulty="hard",
            default_max_attempts=2,
            early_break_rule="per_script",
            carryover_rule="seeded_prev_stage",
            copy_mode="full",
            has_oracle=False,
            implemented=False,
            external_requirements=("agent_sandbox_vm", "vision_judge", "text_llm_judge"),
        )
        for n in range(1, 4)
    ),
    # Game (10 scenarios, 50 subtasks)
    *(
        ScenarioSpec(
            scenario_id=f"Game/scenario{n}",
            category="Game",
            scenario_num=n,
            subtask_count=5,
            runner_family="visual_browser_sandbox",
            difficulty="hard",
            default_max_attempts=2,
            early_break_rule="per_script",
            carryover_rule="seeded_prev_stage",
            copy_mode="full",
            has_oracle=False,
            implemented=False,
            external_requirements=("agent_sandbox_vm", "vision_judge", "text_llm_judge"),
        )
        for n in range(1, 11)
    ),
    # MCP (2 scenarios, 10 subtasks)
    ScenarioSpec(
        scenario_id="MCP/scenario1",
        category="MCP",
        scenario_num=1,
        subtask_count=5,
        runner_family="mcp_github",
        difficulty="medium",
        default_max_attempts=2,
        early_break_rule="success == True",
        carryover_rule="in_place_halt_on_fail",
        copy_mode="in_place",
        has_oracle=False,
        implemented=False,
        external_requirements=("github_mcp_token", "isolated_github_repo"),
    ),
    ScenarioSpec(
        scenario_id="MCP/scenario2",
        category="MCP",
        scenario_num=2,
        subtask_count=5,
        runner_family="mcp_filesystem",
        difficulty="medium",
        default_max_attempts=2,
        early_break_rule="success == True",
        carryover_rule="in_place_halt_on_fail",
        copy_mode="in_place",
        has_oracle=True,
        implemented=True,
    ),
    # Research (5 scenarios, 19 subtasks)
    ScenarioSpec(
        scenario_id="Research/scenario1",
        category="Research",
        scenario_num=1,
        subtask_count=5,
        runner_family="research_rubric",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="per_script",
        carryover_rule="per_script",
        copy_mode="full",
        has_oracle=False,
        implemented=False,
        external_requirements=("live_web_hf", "text_llm_judge"),
    ),
    ScenarioSpec(
        scenario_id="Research/scenario2",
        category="Research",
        scenario_num=2,
        subtask_count=5,
        runner_family="research_rubric",
        difficulty="medium",
        default_max_attempts=2,
        early_break_rule="score == 10",
        carryover_rule="per_script",
        copy_mode="full",
        has_oracle=True,
        implemented=True,
        external_requirements=("live_web",),
    ),
    ScenarioSpec(
        scenario_id="Research/scenario3",
        category="Research",
        scenario_num=3,
        subtask_count=5,
        runner_family="research_rubric",
        difficulty="medium",
        default_max_attempts=2,
        early_break_rule="score >= 9.9",
        carryover_rule="per_script",
        copy_mode="full",
        has_oracle=True,
        implemented=True,
        external_requirements=("live_web",),
    ),
    ScenarioSpec(
        scenario_id="Research/scenario4",
        category="Research",
        scenario_num=4,
        subtask_count=2,
        runner_family="research_bench",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="deliverable_check",
        carryover_rule="in_place",
        copy_mode="in_place",
        has_oracle=False,
        implemented=False,
        external_requirements=("live_web", "text_llm_judge"),
    ),
    ScenarioSpec(
        scenario_id="Research/scenario5",
        category="Research",
        scenario_num=5,
        subtask_count=2,
        runner_family="research_bench",
        difficulty="hard",
        default_max_attempts=2,
        early_break_rule="deliverable_check",
        carryover_rule="in_place",
        copy_mode="in_place",
        has_oracle=False,
        implemented=False,
        external_requirements=("live_web", "text_llm_judge"),
    ),
)

SCENARIO_BY_TASK_ID: dict[str, ScenarioSpec] = {s.task_id: s for s in SCENARIO_CATALOG}
SCENARIO_BY_UPSTREAM_ID: dict[str, ScenarioSpec] = {s.scenario_id: s for s in SCENARIO_CATALOG}

# Stage B pilot scenarios: 3 distinct runner families, 15 subtasks total.
PILOT_TASK_IDS: tuple[str, ...] = (
    "agencybench-backend-scenario-01",
    "agencybench-code-scenario-01",
    "agencybench-mcp-scenario-02",
)

# Predeclared stratified parity subset proposal (subject to Stage D maintainer approval).
PROPOSED_PARITY_TASK_IDS: tuple[str, ...] = (
    "agencybench-backend-scenario-01",
    "agencybench-backend-scenario-02",
    "agencybench-backend-scenario-03",
    "agencybench-code-scenario-01",
    "agencybench-code-scenario-06",
    "agencybench-code-scenario-07",
    "agencybench-mcp-scenario-02",
    "agencybench-research-scenario-02",
    "agencybench-research-scenario-03",
)


def select_scenarios(
    split: str = "pilot",
    task_ids: list[str] | None = None,
    limit: int | None = None,
) -> list[ScenarioSpec]:
    """Deterministically select scenarios by split, explicit task IDs, and limit."""
    if split == "pilot":
        pool = [SCENARIO_BY_TASK_ID[tid] for tid in PILOT_TASK_IDS]
    elif split == "parity":
        pool = [SCENARIO_BY_TASK_ID[tid] for tid in PROPOSED_PARITY_TASK_IDS]
    elif split == "implemented":
        pool = [s for s in SCENARIO_CATALOG if s.implemented]
    elif split == "all":
        pool = list(SCENARIO_CATALOG)
    else:
        raise ValueError(f"Unknown split {split!r}; expected one of: pilot, parity, implemented, all")

    if task_ids:
        requested = set(task_ids)
        # Allow matching by Harbor task_id or upstream scenario_id
        selected = [
            s
            for s in SCENARIO_CATALOG
            if s.task_id in requested or s.scenario_id in requested
        ]
        missing = requested - {s.task_id for s in selected} - {s.scenario_id for s in selected}
        if missing:
            raise ValueError(f"Unknown task_ids: {sorted(missing)}")
        pool = selected

    if limit is not None:
        if limit < 0:
            raise ValueError("--limit must be non-negative")
        pool = pool[:limit]

    return pool
