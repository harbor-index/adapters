"""Constants shared by the CoT-Control adapter, its agent and its tooling."""

from __future__ import annotations

from dataclasses import dataclass

ADAPTER_NAME = "cot-control"
TASK_ORG = "cot-control"

UPSTREAM_REPO = "https://github.com/YuehHanChen/CoTControl"
UPSTREAM_COMMIT = "5d78aeffe0152ba087c2d31cd07712d029c64785"
UPSTREAM_RAW_BASE = (
    f"https://raw.githubusercontent.com/YuehHanChen/CoTControl/{UPSTREAM_COMMIT}"
)
UPSTREAM_QA_DIR = "CoT-Control-QA"

# SHA-256 of the upstream files this adapter vendors or mirrors, at UPSTREAM_COMMIT.
UPSTREAM_FILE_SHA256 = {
    "CoT-Control-QA/prompts.py": "342779fb10b4d1aa1c6f824151569427e335dfb5cf1e4f273bd90ad5149ddf5a",
    "CoT-Control-QA/grading.py": "57f03552b0bee673a578b76ac09d139a8f28c2b25d2925610808b189b0dfd911",
    "CoT-Control-QA/grade_compliance_csv.py": "85de937a809f85a94e38e42177971db3e9989434d7e8720e91e1769cd69cac2f",
    "CoT-Control-QA/llm.py": "aa66295cb3a61c5084529219454cafbd5e96522c9c3f505bfa3e97cdb66e977a",
    "CoT-Control-QA/run_cceval.py": "f0a01e09311277753b874ead083566be47006c7ca237c5ef656b54b5321ffaf5",
}

# Where the reasoning trace and the final response of a run are stored inside the task
# container. The custom agent and the oracle write here; the verifier reads from here.
TRACE_DIR_IN_ENV = "/logs/agent/cot_control"
REASONING_FILENAME = "reasoning.txt"
RESPONSE_FILENAME = "response.txt"


@dataclass(frozen=True)
class DatasetSpec:
    """One upstream QA dataset file."""

    key: str  # used in task ids
    filename: str  # file under CoT-Control-QA/datasets/
    sha256: str
    n_rows: int
    display_name: str  # name used in the paper


DATASETS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        key="gpqa",
        filename="gpqa_w_keyword.csv",
        sha256="a81c8211160e15d35ed471322ab52f4f7e8998b60a102446a878d542b8952c95",
        n_rows=445,
        display_name="GPQA",
    ),
    DatasetSpec(
        key="hle",
        filename="hle_w_keyword.csv",
        sha256="895eb33084896c173bbd3b03007ff50fa321310fc5063460aaf73baea3b4fcb2",
        n_rows=469,
        display_name="HLE",
    ),
    DatasetSpec(
        key="mmlu-pro",
        filename="mmlu_pro_mini_w_keyword.csv",
        sha256="1bcec9049127099acad9ca29d0c1a5e505b171edb63ffce5f273feb8e55dc830",
        n_rows=300,
        display_name="MMLU-Pro",
    ),
)
DATASETS_BY_KEY = {spec.key: spec for spec in DATASETS}


@dataclass(frozen=True)
class ModeSpec:
    """One CoT controllability instruction type (Table 1 of the paper)."""

    name: str  # upstream mode name, e.g. "word_suppression"
    category: str
    difficulty: str
    needs_keywords: bool = False
    llm_judged: bool = False

    @property
    def slug(self) -> str:
        return self.name.replace("_", "-")


MODES: tuple[ModeSpec, ...] = (
    ModeSpec(
        "word_suppression", "information_suppression", "easy", needs_keywords=True
    ),
    ModeSpec(
        "multiple_word_suppression",
        "information_suppression",
        "medium",
        needs_keywords=True,
    ),
    ModeSpec("ignore_question", "information_suppression", "hard", llm_judged=True),
    ModeSpec("repeat_sentences", "information_addition", "easy"),
    ModeSpec("end_of_sentence", "information_addition", "medium"),
    ModeSpec("meow_between_words", "information_addition", "hard"),
    ModeSpec("lowercase_thinking", "stylistic_modification", "easy"),
    ModeSpec("uppercase_thinking", "stylistic_modification", "medium"),
    ModeSpec("alternating_case", "stylistic_modification", "hard"),
)
MODES_BY_NAME = {spec.name: spec for spec in MODES}

# Provisional parity subset: PARITY_ROWS_PER_DATASET questions per dataset, drawn with
# PARITY_SEED from the rows that have keyword data (so that all nine instruction types
# exist for every selected question). 3 datasets x 25 questions x 9 types = 675 tasks.
PARITY_ROWS_PER_DATASET = 25
PARITY_SEED = 42


def task_id_for(dataset_key: str, row_index: int, mode_name: str) -> str:
    """Stable local task id, e.g. ``gpqa-0007-word-suppression``.

    Upstream has no question identifiers, so the id is the zero-based row position in
    the pinned upstream CSV (the same value upstream stores as ``sample["id"]``).
    """
    return f"{dataset_key}-{row_index:04d}-{mode_name.replace('_', '-')}"


def task_name_for(task_id: str) -> str:
    """Registry task name for a local task id."""
    return f"{TASK_ORG}/{ADAPTER_NAME}__{task_id}"
