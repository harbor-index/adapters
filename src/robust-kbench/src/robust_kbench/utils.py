"""
Utilities and dataset indexing for Robust-KBench.
"""

import os
import json
from pathlib import Path
from typing import Dict, List, Any, Optional

PACKAGE_ROOT = Path(__file__).parent
RAW_DATA_DIR = PACKAGE_ROOT / "raw_data"
TASKS_DIR = RAW_DATA_DIR / "tasks"
HIGHLIGHTED_DIR = RAW_DATA_DIR / "highlighted"


# Operators whose upstream highlighted kernel requires >100KB shared memory (H100-only)
H100_ONLY_TASKS = {"mnist_conv_relu_pool__forward"}


def get_available_tasks(include_h100_only: bool = False) -> List[Dict[str, Any]]:
    """Scan raw_data and return all available (operator, pass_type) task specifications."""
    available = []
    if not TASKS_DIR.is_dir():
        return available

    for td in sorted(os.listdir(TASKS_DIR)):
        td_path = TASKS_DIR / td
        if not td_path.is_dir():
            continue

        for ptype in ["forward", "backward"]:
            task_id = f"{td}__{ptype}"
            if not include_h100_only and task_id in H100_ONLY_TASKS:
                continue

            func_file = td_path / f"func_{ptype}.py"
            conf_file = td_path / f"config_{ptype}.json"

            # Prefer verified highlighted golden kernel over unoptimized tasks/*.cu starter
            golden_kernel = None
            cand_hl = HIGHLIGHTED_DIR / td / ptype / "kernel.cu"
            if cand_hl.is_file():
                golden_kernel = cand_hl

            if func_file.is_file() and conf_file.is_file() and golden_kernel is not None:
                available.append({
                    "task_id": task_id,
                    "task_name": td,
                    "pass_type": ptype,
                    "func_path": func_file,
                    "config_path": conf_file,
                    "golden_kernel_path": golden_kernel,
                })

    return available


def extract_docstring_or_description(func_file_path: Path) -> str:
    """Extract docstring or function signature description from PyTorch source."""
    try:
        with open(func_file_path, "r", encoding="utf-8") as f:
            code = f.read()
        if '"""' in code:
            parts = code.split('"""')
            if len(parts) >= 3:
                return parts[1].strip()
        elif "'''" in code:
            parts = code.split("'''")
            if len(parts) >= 3:
                return parts[1].strip()
    except Exception:
        pass
    return "Optimized CUDA kernel implementing forward/backward tensor transformations."
