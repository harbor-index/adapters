# Harbor Adapter for Robust-KBench

## Overview
This package is the official Harbor adapter for **Robust-KBench** ([Sakana AI](https://github.com/SakanaAI/robust-kbench), [arXiv:2509.14279](https://arxiv.org/abs/2509.14279)), a benchmark designed to evaluate LLMs and AI agents on generating robust, highly-optimized CUDA kernels for neural network building blocks and PyTorch operations.

## What is Robust-KBench?
Existing CUDA kernel generation benchmarks (such as KernelBench) evaluate kernels only on a single fixed input shape and default initialization, allowing brittle kernels, memory-bounds violations, or overfitting to pass. Robust-KBench introduces rigorous **multi-input tensor shape checks** and **multi-initialization variance checks** across forward and backward operators before measuring speedups, ensuring only numerically sound, generalizable kernels are rewarded.

## Adapter Features
- **Strict Robustness Settings**: Explicitly activates `multi_input_configs` and `multi_init_configs` for all tasks, adhering to Harbor's strict parity specifications.
- **Offline & Bundled Assets**: Bundles core operator specifications and golden solutions so tasks can be generated completely offline.
- **Ground Truth Verification (Oracle)**: Provides reference CUDA kernels from upstream Sakana AI, achieving 100% pass on all supported tasks.
- **Isolated GPU Evaluation**: Configures containerized execution with `gpus = 1`, running PyTorch 2.6 with CUDA 12.8 (`nvcr.io/nvidia/pytorch:25.01-py3`), `NVIDIA_TF32_OVERRIDE=0`, and Ninja JIT compilation.

## Generated Task Structure
Each generated Harbor task directory contains:
- `task.toml`: Task metadata, hardware limits (1 GPU, 4 CPUs, 16GB RAM), and verifier timeouts.
- `instruction.md`: Instruction prompt describing the operator mathematical definition, signature, and constraints.
- `environment/Dockerfile`: Container environment based on `nvcr.io/nvidia/pytorch:25.01-py3` (CUDA 12.8) with build dependencies and `NVIDIA_TF32_OVERRIDE=0`.
- `tests/test.sh`: Execution harness invoking `verifier.py` to compile and validate the agent's `/app/kernel.cu`.
- `tests/eval_verifier.py`: Automated tester comparing outputs across multi-shape inputs and writing reward to `/logs/verifier/reward.txt`.
- `solution/solve.sh`: Golden reference solution copying verified upstream CUDA kernel to `/app/kernel.cu`.

## Run Evaluation
To run the evaluation locally with Harbor:
```bash
# 1. Generate the task set
uv run robust-kbench --output-dir ./tasks_output

# 2. Run the oracle agent to verify 100% reward
harbor run --dataset ./tasks_output --agent oracle
```

## Usage
Inside the adapter package:
```bash
# Sync dependencies
uv sync

# Generate all tasks
uv run robust-kbench --output-dir /path/to/output

# Generate specific task
uv run robust-kbench --output-dir /path/to/output --limit 5
```

## Comparison with Original Benchmark
Below is the parity comparison between the original benchmark harness and the Harbor adapter:

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Performance | Harbor Performance |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: |
| oracle | ground_truth | correctness_rate | 2 | 11 | 1.0 +/- 0.0 | 1.0 +/- 0.0 |

To reproduce the parity results:
```bash
# 1. Generate the evaluation tasks
uv run robust-kbench --output-dir ./eval_tasks

# 2. Execute the oracle run
harbor run --dataset ./eval_tasks --agent oracle --output-dir ./parity_results
```

## Notes & Caveats
- Requires an NVIDIA GPU supporting CUDA Compute Capability 8.0+ (Ampere, Hopper, Ada, or Blackwell) for compilation and execution.
- In accordance with the Harbor spreadsheet instructions, correctness is graded before speedup, and KernelBench-derived duplicate tasks are omitted in favor of custom robust operators.
- `mnist_conv_relu_pool__forward` is excluded by default because its upstream highlighted kernel allocates >100KB shared memory (H100-specific); pass `--include-h100-only` to generate it on H100 GPUs.
- `NVIDIA_TF32_OVERRIDE=0` is set in the container so PyTorch reference outputs retain full FP32 precision on Ampere/Ada/Hopper/Blackwell GPUs during numerical parity checks (`atol=1e-3, rtol=1e-3`).

## Troubleshooting
- **CUDA Device Not Found**: Ensure the NVIDIA Container Toolkit is installed on the host and Docker can access GPUs via `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.
- **Ninja JIT Build Timeout**: If the kernel compilation exceeds the default timeout, increase `timeout_sec` in `task.toml`.
- **Memory Limit / OOM**: For larger tensor dimensions in custom tasks, ensure Docker is allocated at least 16GB of system memory and 12GB of GPU VRAM.

## Prerequisites
- Python >= 3.10
- NVIDIA GPU with driver installed and CUDA >= 12.0
- Docker with NVIDIA Container Toolkit configured (`docker run --gpus all ...`)
- `uv` package manager

## Citation
```bibtex
@article{sakana2025robustkbench,
  title={Robust-KBench: A Robustness-focused CUDA Kernel Generation Benchmark},
  author={Sakana AI Team},
  journal={arXiv preprint arXiv:2509.14279},
  year={2025}
}
```

## Authors & Contributions
- **Original Benchmark**: Sakana AI
- **Harbor Adapter Author**: zlanta (zlanta@users.noreply.github.com)
