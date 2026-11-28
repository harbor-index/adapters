"""
Verifier for Robust-KBench tasks using the upstream SakanaAI robust_kbench
correct_cuda_kernel evaluator with multi_input_settings and multi_init_settings enabled.
"""

import json
import os
import sys
import traceback

# Allow importing bundled robust_kbench from /tests (including in subprocesses!)
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)
existing_pp = os.environ.get("PYTHONPATH", "")
os.environ["PYTHONPATH"] = f"{TESTS_DIR}{os.pathsep}{existing_pp}" if existing_pp else TESTS_DIR

import torch
from robust_kbench.primitives.evaluate import correct_cuda_kernel


def main():
    if not torch.cuda.is_available():
        print("ERROR: CUDA device is not available in evaluation environment!")
        sys.exit(1)

    cuda_code_path = "/app/kernel.cu"
    if not os.path.isfile(cuda_code_path):
        print(f"ERROR: Candidate kernel not found at {cuda_code_path}")
        sys.exit(1)

    task_files_dir = os.path.join(TESTS_DIR, "task_files")
    meta_path = os.path.join(TESTS_DIR, "meta.json")
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    is_forward = meta.get("pass_type", "forward") == "forward"

    print(
        f"Running robust_kbench.correct_cuda_kernel for {meta.get('task_name')} "
        f"({'FORWARD' if is_forward else 'BACKWARD'})..."
    )
    print("Flags: multi_input_settings=True, multi_init_settings=True, atol=1e-3, rtol=1e-3")

    try:
        results = correct_cuda_kernel(
            task_dir=task_files_dir,
            cuda_code_path=cuda_code_path,
            multi_init_settings=True,
            multi_input_settings=True,
            op_atol=1e-3,
            op_rtol=1e-3,
            num_correct_trials=5,
            gpu_id=0,
            debug=True,
            forward=is_forward,
        )
    except Exception as exc:
        print(f"Verification raised an exception: {exc}")
        traceback.print_exc()
        sys.exit(1)

    print("Evaluation summary:", json.dumps(results.get("summary", {}), indent=2))
    is_correct = bool(results.get("summary", {}).get("correct", False))
    if is_correct:
        print("SUCCESS: Candidate CUDA kernel passed all multi-input & multi-init checks!")
        sys.exit(0)
    else:
        print("FAILURE: Candidate CUDA kernel failed correctness checks.")
        sys.exit(1)


if __name__ == "__main__":
    main()
