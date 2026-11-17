"""Canonical task inventory pinned to RoboDojo's official task inventory.

Source: RoboDojo-Benchmark/RoboDojo, scripts/internal/task_inventory.py (MIT).
Generalization uses both standard and randomized configurations.
"""

UPSTREAM_REVISION = "f57e1633ffed59761bc8e568cfe7530c4b33381c"
XPOLICYLAB_REVISION = "5d0be1499634d3176258c8f2e31f5fe816497b0a"

DIMENSION_TASKS: dict[str, tuple[str, ...]] = {
    "generalization": (
        "stack_bowls",
        "push_T",
        "pack_objects_into_box",
        "fold_clothes",
        "hang_mugs",
        "sweep_blocks",
        "pour_liquid_into_cup",
        "make_toast",
        "arrange_largest_number",
        "sort_nesting_dolls_by_size",
        "store_laptop_and_headphones",
        "stack_blocks",
    ),
    "memory": (
        "cover_blocks",
        "match_and_pick_from_conveyor",
        "swap_blocks",
        "swap_T",
        "press_by_number",
        "imitate_sorting_sequence",
    ),
    "precision": (
        "fasten_screws",
        "plug_in_charger",
        "insert_tubes",
        "pour_balls_into_vase",
        "play_Xylophone",
        "deposit_coin",
        "insert_key",
        "build_tower",
    ),
    "long-horizon": (
        "fill_pen_holder",
        "classify_objects",
        "put_bottles_into_dustbin",
        "play_tic_tac_toe",
        "fill_egg_holder",
        "organize_table",
        "make_kong",
        "play_stacking_toy",
    ),
    "open": (
        "align_blocks",
        "general_pickup",
        "solve_equation",
        "stack_blocks_by_language",
        "classify_objects_by_language",
        "pick_from_conveyor_by_image",
        "store_tools_in_toolbox",
        "pour_by_language",
    ),
}

# Episodes per canonical task. Generalization splits them evenly between its
# standard and its randomized configuration.
NATIVE_EPISODES = 50


def task_spec(task_id: str, seed: int = 0) -> dict:
    """Return the immutable evaluation settings for one canonical task.

    This is the only definition of the variants and their episode counts.
    Generation writes it out, and ``runtime.load_spec`` accepts nothing else.
    """
    dimension = next((d for d, names in DIMENSION_TASKS.items() if task_id in names), None)
    if dimension is None:
        raise ValueError(f"Unknown canonical RoboDojo task: {task_id}")
    names = [task_id, f"{task_id}_random"] if dimension == "generalization" else [task_id]
    return {
        "task_id": task_id,
        "dimension": dimension,
        "seed": seed,
        "upstream_revision": UPSTREAM_REVISION,
        "variants": [{"name": name, "episodes": NATIVE_EPISODES // len(names)} for name in names],
    }


# The CPU mock is not part of the benchmark. Its identifiers are kept apart
# from the canonical ones so that a mock result can never pass for a real one.
MOCK_TASK_ID = "mock_reach_targets"
MOCK_LABEL = "mock"
MOCK_POLICY_NAME = "HarborDirectMock"
MOCK_MAX_EPISODES = 50


def mock_task_spec(seed: int = 0, episodes: int = 2) -> dict:
    """Return the settings of the mock task; ``episodes`` is free, unlike the native 50."""
    if type(episodes) is not int or not 1 <= episodes <= MOCK_MAX_EPISODES:
        raise ValueError(f"Mock episodes must be an integer from 1 through {MOCK_MAX_EPISODES}")
    return {
        "mock": True,
        "task_id": MOCK_TASK_ID,
        "dimension": MOCK_LABEL,
        "seed": seed,
        "upstream_revision": MOCK_LABEL,
        "variants": [{"name": MOCK_TASK_ID, "episodes": episodes}],
    }
