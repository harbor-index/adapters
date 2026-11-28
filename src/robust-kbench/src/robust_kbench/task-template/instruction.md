# Task: Write Optimized & Robust CUDA Kernel for {{task_name}} ({{pass_type}} pass)

## Overview
You are tasked with writing a custom CUDA kernel in C++ for the PyTorch operation defined in `/app/func.py`.

Your implementation must be written to:
`/app/kernel.cu`

## Requirements
1. **Target File**: You must write your complete CUDA C++ implementation into `/app/kernel.cu`.
2. **Interface**: Your CUDA file must export the required PyTorch C++ extension bindings using `PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) { ... }` or provide a function callable via PyTorch's `load_inline`. The primary entrypoint function should match the signature of the PyTorch reference function in `/app/func.py`.
3. **Correctness**: Your kernel will be compiled with PyTorch `ninja` JIT and evaluated against the reference PyTorch implementation across **multiple tensor batch sizes, shapes, and initialization distributions** specified in `/app/config.json`.
4. **Tolerance**: Absolute tolerance `atol = 1e-3` and relative tolerance `rtol = 1e-3`. Any divergence beyond tolerance or memory violation (segfault / out-of-bounds) will result in a zero reward.

## Files in Workspace
- `/app/func.py`: Reference PyTorch implementation for this operator.
- `/app/config.json`: Configuration settings including input dimensions and test variants.
- `/app/kernel.cu`: Where your CUDA kernel solution should be written.
