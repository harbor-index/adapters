## JAXBench → Harbor Adapter

## Overview

JAXBench evaluates whether an agent can make a JAX workload run faster on a TPU while keeping its outputs
numerically identical. Each task gives the agent an XLA/JAX reference implementation (`baseline.py`) and asks
for a drop-in replacement, typically a Pallas kernel, written to `/workspace/kernel.py`. The grader is the
upstream JAXBench harness: it checks correctness against the baseline on bf16 inputs and measures the speedup
over XLA with device-side profiling on a TPU v6e chip.

- **Source:** [AI-Hypercomputer/accelerator-agents/JAXBench](https://github.com/AI-Hypercomputer/accelerator-agents/tree/main/JAXBench),
  pinned to commit [`7afeb78`](https://github.com/AI-Hypercomputer/accelerator-agents/tree/7afeb78188383e857f8fe12d98d308eb1fd30435/JAXBench)
  (`UPSTREAM_COMMIT` in `src/jaxbench/adapter.py`); paper: [arXiv:2607.20466](https://arxiv.org/abs/2607.20466).
- **Tasks: 49.** Upstream level1 has 50 workloads: 17 priority kernels from MaxText LLMs (`1p`–`17p`) and
  33 fused operators adapted from KernelBench level 2 (`18k`–`50k`). One (`11p_Megablox_GMM`) is excluded
  because its upstream baseline cannot be compiled (see [Notes & Caveats](#notes--caveats)).
- **Splits:** `full` (49 tasks, default) and `parity` (15 tasks used for the parity experiment, `--split parity`).
- **Hardware:** every task needs one TPU v6e chip (`[environment.tpu] type = "v6e"`, `topology = "1x1"`),
  which Harbor provides through its GKE environment.
- **Main adaptations:** the paper's one-shot preamble is kept verbatim except rule 2, which asks the agent to
  write `/workspace/kernel.py` instead of replying with a code block (the agent works in a shell, not a chat);
  the instruction adds how to run the evaluator. Grading, tolerances and timing are the upstream harness,
  unchanged.

## What is JAXBench?

JAXBench (Tschand et al., 2026) is a TPU-native benchmark for autonomous kernel optimization: 50 JAX reference
workloads sized to saturate TPU v6e MXUs, an evaluation harness that profiles device-side time with
`jax.profiler` (Perfetto traces, median over 50 iterations), and correctness checks with
`np.allclose(atol=1e-2, rtol=1e-2)` in float32. The paper evaluates best-of-N, iterative-refinement and
Autocomp agents with Gemini models under a 144-sample budget and reports, per method, the best-so-far
**speedup over XLA floored at 1×** (an incorrect or slower kernel counts as 1×), aggregated as a geometric
mean, plus **fast₁** (fraction of benchmarks with a correct kernel faster than XLA).

## Adapter Features

- **Upstream harness as the grader.** Each task ships a pristine copy of the JAXBench harness and the
  workload's `baseline.py` (and upstream `optimized.py`, used only for the harness's informational
  `speedup_vs_pallas` report) under `tests/`, which the agent never sees; `tests/test.sh` runs
  `python -m JAXBench evaluate --workload <w> --kernel /workspace/kernel.py --tpu v6e --json`.
- **Reward = the paper's floored speedup.** `tests/compute_reward.py` writes
  `reward = max(speedup, 1)` if the kernel is correct, else `1`, plus `speedup`, `correct` and `fast_1` to
  `/logs/verifier/reward.json`. A missing kernel, crash, timeout or incorrect output scores 1×.
- **Robust grading on a single TPU.** Before grading, `test.sh` stops leftover agent processes that still hold
  the TPU (`/dev/vfio`), and it retries the evaluator (same kernel) while the TPU is still being released.
- **Agent workspace.** `/workspace` contains `baseline.py` and the JAXBench harness with only this workload's
  baseline, so the agent can run the same `evaluate` command the grader uses.
- **Oracles.** Upstream's hand-written Pallas kernel where it passes the upstream harness, otherwise the
  baseline itself. A `--oracle baseline` variant uses the baseline everywhere (XLA oracle).
- **Metric.** `metrics/geomean.py` aggregates the floored speedups into a geometric mean (missing results
  count as 1×), alongside Harbor's built-in `mean` (which counts a trial with no reward as 0, so it can fall
  below 1× when trials error out).
- **Infrastructure failures are not scored.** If the TPU never becomes usable for grading, `test.sh` removes
  the reward file, so Harbor reports the trial as an error instead of a 1× score.
- **Stable task names.** `ai-hypercomputer/jaxbench__level1-<workload>`, lower-cased and sanitized (e.g.
  `ai-hypercomputer/jaxbench__level1-8p-gemm`).

## Generated Task Structure

```
jaxbench/
├── level1-8p-gemm/
│   ├── task.toml                 # name, authors, TPU v6e 1x1, 8 CPU / 32 GiB, timeouts, [metadata] jaxbench_oracle
│   ├── instruction.md            # paper preamble + benchmark id + inlined baseline + how to evaluate
│   ├── environment/
│   │   ├── Dockerfile            # python:3.12-slim + jax[tpu] + numpy; copies workspace/
│   │   └── workspace/            # becomes /workspace: baseline.py + JAXBench harness (this workload's baseline only)
│   ├── solution/
│   │   ├── kernel.py             # oracle kernel (upstream optimized.py, or baseline.py)
│   │   └── solve.sh              # copies kernel.py to /workspace/kernel.py
│   └── tests/
│       ├── JAXBench/             # pristine upstream harness + this workload's baseline.py / optimized.py
│       ├── compute_reward.py     # floored-speedup reward
│       └── test.sh               # grades /workspace/kernel.py, writes /logs/verifier/reward.json
```

Adapter package layout:

```
src/jaxbench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── metrics/geomean.py
├── run_jaxbench_gke_tpu_v6e.yaml               # full dataset (oracle by default)
├── run_jaxbench_gke_tpu_v6e_xla_oracle.yaml    # XLA-baseline oracle on all 49 tasks
├── run_jaxbench_gke_tpu_v6e_pallas_oracle.yaml # upstream Pallas oracles (3p, 4p, 8p)
├── run_jaxbench_gke_tpu_v6e_parity.yaml        # parity split with mini-swe-agent
└── src/jaxbench/
    ├── __init__.py
    ├── adapter.py                # JAXBenchAdapter: clone upstream at the pinned commit, render tasks
    ├── main.py                   # CLI
    └── task-template/
```

## Run Evaluation / Harness

All commands run from the repository root. Every task needs a TPU v6e chip, so jobs use Harbor's GKE
environment (see [Installation / Prerequisites](#installation--prerequisites)); edit the `environment.kwargs`
(project, cluster, region, Artifact Registry) in the config to match your project.

Commands that do not use a config file must pass the same GKE settings on the command line; the examples
below use this variable:

```bash
GKE=(-e gke --ek project_id=<gcp_project> --ek cluster_name=<cluster> --ek region=<region>
     --ek registry_location=<region> --ek registry_name=<artifact_registry_repo>)
```

### Running with Datasets Registry

```bash
# Oracle on the published dataset
harbor run -d ai-hypercomputer/jaxbench "${GKE[@]}"

# A specific agent and model
harbor run -d ai-hypercomputer/jaxbench "${GKE[@]}" -a <agent_name> -m "<model_name>"

# Parity subset (15 tasks), once published under the `parity` tag
harbor run -d ai-hypercomputer/jaxbench@parity "${GKE[@]}" -a <agent_name> -m "<model_name>"
```

### Using Job Configurations

```bash
# Full dataset, oracle agent (other agents are commented out in the file)
harbor run -c src/jaxbench/run_jaxbench_gke_tpu_v6e.yaml

# XLA-baseline oracle on all 49 tasks / upstream Pallas oracles
harbor run -c src/jaxbench/run_jaxbench_gke_tpu_v6e_xla_oracle.yaml
harbor run -c src/jaxbench/run_jaxbench_gke_tpu_v6e_pallas_oracle.yaml

# Locally generated dataset with any agent
harbor run -p datasets/jaxbench "${GKE[@]}" -a <agent_name> -m "<model_name>"
```

Results are written to `jobs/`. Each trial's `verifier/reward.json` holds `reward`, `speedup`, `correct` and
`fast_1`, and `verifier/evaluation.json` holds the harness's full report.

### Running Individual Trial

`harbor trial start` has no flag for environment kwargs in Harbor 0.23.0, so run a single task as a
one-task job:

```bash
harbor run -p datasets/jaxbench -i level1-8p-gemm "${GKE[@]}"
harbor run -p datasets/jaxbench -i level1-8p-gemm "${GKE[@]}" -a <agent_name> -m "<model_name>"
```

## Usage: Create Task Directories

```bash
cd src/jaxbench
uv sync
uv run jaxbench --output-dir ../../datasets/jaxbench                                    # 49 tasks, Pallas/baseline oracle
uv run jaxbench --output-dir ../../datasets/jaxbench-xla-oracle --oracle baseline       # XLA oracle everywhere
uv run jaxbench --output-dir ../../datasets/jaxbench-parity --split parity              # 15-task parity split
```

Flags:
- `--output-dir`: where to write tasks (default `datasets/jaxbench` at the repo root)
- `--limit N`: only the first N tasks
- `--overwrite`: overwrite existing task directories
- `--task-ids ...`: only these task ids (e.g. `level1-8p-gemm`)
- `--source-dir`: use an existing JAXBench checkout instead of cloning upstream at the pinned commit
- `--levels`: benchmark levels to include (default `level1`)
- `--oracle {pallas,baseline}`: oracle source (default `pallas`, with the documented baseline fallbacks)
- `--split {full,parity}`: full set or the 15-task parity split

## Comparison with Original Benchmark (Parity)

Upstream JAXBench ships an evaluation harness but no agent runner, so parity follows Scenario 2: the
upstream repository is forked and a runner for the same Harbor-compatible agent is added there
([`liulehui/accelerator-agents@harbor-parity`, `JAXBench/harbor_parity/`](https://github.com/liulehui/accelerator-agents/tree/harbor-parity/JAXBench/harbor_parity)).
Both sides run mini-swe-agent with the same model, prompt, configuration and hardware, and both grade the
agent's final `/workspace/kernel.py` with upstream `python -m JAXBench evaluate`. Parity uses the
15-task `parity` split (7 priority kernels: `1p 3p 4p 8p 12p 15p 16p`; 8 KernelBench-fused:
`18k 22k 26k 30k 34k 38k 42k 46k`), 3 runs per side. Scores are **mean ± sample SEM** over the 3 runs; full
per-run values are in [`parity_experiment.json`](parity_experiment.json).

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|----------------|--------------|--------------------------------|----------------------------|
| mini-swe-agent@2.4.6 | deepseek-v4-1-flash-260910 | Geomean speedup, floored (×) | 3 | 15 of 49 (31%) | 1.80 ± 0.16 | 1.60 ± 0.15 |
| mini-swe-agent@2.4.6 | deepseek-v4-1-flash-260910 | Correct rate (%) | 3 | 15 of 49 (31%) | 71.11 ± 12.37 | 64.44 ± 8.89 |
| mini-swe-agent@2.4.6 | deepseek-v4-1-flash-260910 | fast₁ (%) | 3 | 15 of 49 (31%) | 55.56 ± 8.01 | 53.33 ± 7.70 |
| mini-swe-agent@2.4.6 | deepseek-v4-1-flash-260910 | Mean reward (×) | 3 | 15 of 49 (31%) | 2.74 ± 0.39 | 2.45 ± 0.34 |

The run-score ranges overlap on every metric (e.g. geomean: original 1.96 / 1.96 / 1.47, Harbor
1.31 / 1.83 / 1.66), and per-task disagreements move in both directions across runs (`3p`, `4p`, `16p`,
`22k`, `46k` each flip between correct and incorrect on both sides), which points to sampling variance rather
than an adaptation difference. Deterministic tasks match closely, e.g. `8p` 1.18–1.21× and `12p` 1.46–1.47×
on every run of both sides.

**Configuration (identical on both sides).**

| Item | Value |
|------|-------|
| Agent | mini-swe-agent 2.4.6, installed with `uv tool install --python 3.12 mini-swe-agent==2.4.6 --with litellm --with orjson --with fastapi`, packages resolved with `UV_EXCLUDE_NEWER=2026-10-17T00:00:00Z` |
| Agent flags | `--yolo --model=openai/deepseek-v4.1-flash --cost-limit 0 -c mini -c model.model_kwargs.timeout=3600 -c <config> -c model.model_kwargs.extra_headers.X-Session-ID=<trial> --exit-immediately`, run from `/workspace` |
| Agent config | `step_limit: 150`, `temperature: 1.0`, `top_p: 0.95` (DeepSeek's recommended sampling for agentic use); default reasoning effort |
| Model | DeepSeek V4.1 Flash through the PP API OpenAI-compatible endpoint (`https://app-us.ppapi.ai/v1`); requested as `deepseek-v4.1-flash`, served as `deepseek-v4-1-flash-260910` |
| Prompt | this adapter's `instruction.md`, rendered identically on both sides |
| Time limit | 3600 s per agent run; verifier 1800 s |
| Hardware | 1 TPU v6e chip (GKE `ct6e-standard-1t`, spot), 8 CPU / 32 GiB memory request, 20 GiB ephemeral storage |
| Software | `python:3.12-slim`, jax 0.12.0, jaxlib 0.12.0, libtpu 0.0.50 (pinned on the original side; the Harbor image resolved the same versions during the experiment and now pins them) |
| Harbor | `harbor[gke]==0.23.0` |
| Grading | upstream `python -m JAXBench evaluate --tpu v6e` at `7afeb78`, with the same leftover-process cleanup and busy-TPU retry on both sides |

**Reproduce (Harbor side).** Requires a GKE cluster with a TPU v6e 1x1 node pool and an Artifact Registry
repository (see [Installation / Prerequisites](#installation--prerequisites)); set `environment.kwargs` in
the config to your project.

```bash
cd src/jaxbench && uv run jaxbench --output-dir ../../datasets/jaxbench-parity --split parity && cd ../..
export LLM_API_KEY=...   # PP API key; the config passes it to the agent as OPENAI_API_KEY
for i in 1 2 3; do
  uvx --from 'harbor[gke]==0.23.0' harbor run -c src/jaxbench/run_jaxbench_gke_tpu_v6e_parity.yaml
done
```

**Reproduce (original side).** See the fork's
[`JAXBench/harbor_parity/README.md`](https://github.com/liulehui/accelerator-agents/blob/harbor-parity/JAXBench/harbor_parity/README.md):

```bash
git clone -b harbor-parity https://github.com/liulehui/accelerator-agents && cd accelerator-agents
gcloud builds submit JAXBench/harbor_parity --project PROJECT --region us-central1 \
  --tag us-central1-docker.pkg.dev/PROJECT/REPO/jaxbench-original:v1
export LLM_API_KEY=...
python3 JAXBench/harbor_parity/launch.py --context gke_PROJECT_us-central1_CLUSTER \
  --image us-central1-docker.pkg.dev/PROJECT/REPO/jaxbench-original:v1 --runs 3 --out parity_results/original
```

**Reading the results.** Per run, each of the 15 tasks contributes its floored speedup
(`max(speedup, 1)` if correct, else 1); the geomean and mean of these are the speedup metrics, correct rate is
the fraction of tasks with a correct kernel, and fast₁ the fraction with a correct kernel faster than XLA.

**Infrastructure failures were rerun, not scored.** Spot preemptions, Harbor agent sessions cut off by a GKE
networking event (below), and grading attempts that found the TPU still held by a just-stopped process were
all rerun until each task had one valid result per run on each side (90 valid results in total).

**Links:** original benchmark: [AI-Hypercomputer/accelerator-agents](https://github.com/AI-Hypercomputer/accelerator-agents/tree/main/JAXBench) ·
parity fork: [liulehui/accelerator-agents@harbor-parity](https://github.com/liulehui/accelerator-agents/tree/harbor-parity/JAXBench/harbor_parity) ·
adapter PR: [harbor-framework/adapters#6](https://github.com/harbor-framework/adapters/pull/6) ·
dataset PR: [harbor-framework/harbor-datasets#268](https://github.com/harbor-framework/harbor-datasets/pull/268) · parity results (Hugging Face): [harborframework/parity-experiments#273](https://huggingface.co/datasets/harborframework/parity-experiments/discussions/273)

## Notes & Caveats

**Task count: 49 of 50.** The adapter converts JAXBench level1 at upstream commit
[`7afeb78`](https://github.com/AI-Hypercomputer/accelerator-agents/tree/7afeb78188383e857f8fe12d98d308eb1fd30435/JAXBench)
(50 workloads) and excludes one:

| Excluded workload | Reason |
|-------------------|--------|
| `11p_Megablox_GMM` | Upstream `baseline.py` cannot be compiled by the upstream harness: `workload()` receives `max_expert_size` as a positional argument, which `jax.jit` traces, but uses it as a static array shape (`baseline.py:42`). `python -m JAXBench evaluate` therefore errors while running the baseline, before any candidate kernel is graded, so no agent can solve this task. |

The exclusion list lives in `EXCLUDED_WORKLOADS` in `src/jaxbench/adapter.py`.

**Level 2 is not adapted.** After `7afeb78`, upstream added `benchmark/level2/` (23 workloads whose baselines
are hand-written Pallas kernels). It ships only `baseline.py` per workload, the upstream harness cannot
discover it, and the paper does not evaluate it, so it is out of scope. Level 1 and the harness are unchanged
between `7afeb78` and upstream `main` (checked 2026-10-27).

**Upstream path bug.** At `7afeb78` the workloads live in `benchmark/level1/<name>/`, but the harness only
discovers `benchmark/<name>/`, so upstream `list`/`evaluate` find no workloads. The adapter copies each
workload flat into `JAXBench/benchmark/<name>/` inside the task (no harness code changes); the parity fork
fixes discovery to also search `benchmark/level*/`.

**Oracle solutions.** For tasks where upstream ships a hand-written Pallas kernel (`optimized.py`), the
oracle is that kernel. For four tasks, the upstream Pallas kernel fails the upstream harness at the pinned
commit, so the oracle falls back to the baseline itself (expected speedup ≈1.0×), the same as the 42 tasks
without a Pallas kernel. These tasks remain valid for agents: the baseline compiles and grades correctly.

| Workload | Upstream `optimized.py` failure |
|----------|---------------------------------|
| `1p_Flash_Attention` | Output differs from baseline (max diff 0.0625 > 1e-2 tolerance) |
| `2p_GQA_Attention` | Output differs from baseline (max diff 0.0625 > 1e-2 tolerance) |
| `6p_Paged_Attention` | `workload()` takes 5 arguments but `create_inputs()` returns 6 |
| `7p_Ragged_Paged_Attention` | Scoped VMEM OOM at compile time on TPU v6e (34.39 MiB > 32 MiB limit) |

The fallback list lives in `BROKEN_PALLAS_ORACLES` in `src/jaxbench/adapter.py`; each task records its
oracle source in `task.toml` (`[metadata] jaxbench_oracle = "pallas" | "baseline"`). The Pallas kernels are
still copied into `tests/` because the upstream harness benchmarks `optimized.py` (when present) for its
`speedup_vs_pallas` report; that step does not affect correctness or the reward.

**Oracle verification.** The oracle is judged on correctness (`correct = 1` on every task), not on the
reward being exactly 1.0: speedup is a device-timing ratio, so a baseline oracle scores ≈1.0× with small
noise (e.g. 1.01×), and a Pallas oracle scores its real speedup over XLA. The same convention is used by the
`algotune` adapter, which also uses the baseline as its oracle. Two oracle configs are provided:

| Config | Dataset | Oracle | Tasks |
|--------|---------|--------|-------|
| `run_jaxbench_gke_tpu_v6e_xla_oracle.yaml` | `datasets/jaxbench-xla-oracle` (`--oracle baseline`) | XLA baseline for every task | 49 |
| `run_jaxbench_gke_tpu_v6e_pallas_oracle.yaml` | `datasets/jaxbench` (default) | Upstream Pallas kernel | 3 (`3p`, `4p`, `8p`) |

The XLA oracle is correct on 49/49 tasks.

**Final kernel vs. best-so-far.** The paper scores the best kernel found within a 144-sample budget; Harbor
grades the agent's final `/workspace/kernel.py` after a single agent run. Scores from this adapter are
therefore not directly comparable to the paper's tables. The parity comparison is unaffected: both sides
grade the final kernel.

**Prompt.** The paper's preamble rule 2 ("output a single complete Python file inside one code block; no
prose outside it") becomes "write a single complete, self-contained Python file to `/workspace/kernel.py`"
because the agent works in a shell; rule 3 is dropped for the same reason. No TPU/Pallas documentation is
added to the prompt (the paper's "+context" variants inject curated docs). The same prompt is used on both
parity sides.

**Pinned JAX.** Upstream JAXBench does not pin JAX. The task image pins `jax[tpu]==0.12.0`,
`jaxlib==0.12.0`, `libtpu==0.0.50` and `numpy==2.5.4`, the versions the parity experiment ran with on both
sides; other JAX releases may change baseline timings and therefore speedups.

**Hardware.** Every task requires one TPU v6e chip (`[environment.tpu] type = "v6e"`, `topology = "1x1"`),
which Harbor provides only through the GKE environment. A TPU can be opened by only one process, which is why
`test.sh` stops leftover agent processes before grading. The harness's `--tpu auto` does not recognize a v6e
(it reports `device_kind` "TPU v6 lite"), so grading passes `--tpu v6e`; this only affects the harness's
TFLOPS/utilization report, not correctness or speedup.

## Installation / Prerequisites

- **Harbor** with GKE support: `uv tool install 'harbor[gke]'` (parity used `harbor[gke]==0.23.0`).
- **Adapter environment:** `cd src/jaxbench && uv sync`.
- **Google Cloud:** `gcloud` authenticated, `kubectl` and `gke-gcloud-auth-plugin` on `PATH`, and:
  - a GKE cluster with a TPU v6e node pool, machine type `ct6e-standard-1t` (topology 1x1);
  - an Artifact Registry Docker repository for task images (`registry_location`, `registry_name` in the
    config); images are built with Cloud Build, so the Compute Engine default service account needs
    `artifactregistry.writer` on the repository, `storage.objectViewer` on the Cloud Build bucket, and
    `logging.logWriter`.
  - Harbor uses `kubectl`'s **current context**, so point it at the target cluster before starting a job.
- **Model API keys** for the agent you run (for parity, `LLM_API_KEY` for the PP API endpoint).

## Troubleshooting

- **`TPU initialization failed: ... /dev/vfio/0: Device or resource busy`** inside a trial: another process in
  the pod holds the TPU. `test.sh` handles this at grading time; during agent runs, background processes the
  agent started can cause it.
- **Spot preemptions** kill running trials. Add a `retry` block to the config with
  `include_exceptions: [NonZeroAgentExitCodeError, RuntimeError, GKEExecStreamClosedError]` (Harbor matches
  exception names exactly, not base classes), as in `run_jaxbench_gke_tpu_v6e_parity.yaml`.
- **Agent run ends early with no error on GKE.** When any node is removed (e.g. a spot preemption elsewhere in
  the cluster), GKE may scale down its `konnectivity-agent` pods, dropping long-running exec sessions, and
  some agents report this as a normal exit. Keeping a fixed `konnectivity-agent` replica count (the
  `nodesToReplicas` ladder in the `kube-system/konnectivity-agent-autoscaler-config` ConfigMap) avoids it.
- **`gke-gcloud-auth-plugin` not found:** add the Google Cloud SDK `bin` directory to `PATH`.
- **TPU capacity:** v6e on-demand capacity can be scarce; spot node pools with retries are the practical option.

## Citation

```bibtex
@article{tschand2026jaxbench,
  title={JAXBench: Benchmarking Autonomous TPU Kernel Optimization},
  author={Tschand, Arya and Hong, Charles and Walker, Julian and Cai, Nina and Wang, Shangkun and Subramanian, Suvinay and Dev, Sundar and Janapa Reddi, Vijay and Yazdanbakhsh, Amir and Sankaran, Sethu},
  journal={arXiv preprint arXiv:2607.20466},
  year={2026},
  url={https://arxiv.org/abs/2607.20466}
}
```

## Authors & Contributions

This adapter is developed and maintained by [Lehui Liu](mailto:imliulehui@gmail.com).

**Issues and Contributions:**
- Submit issues and pull requests to [harbor-framework/adapters](https://github.com/harbor-framework/adapters)
- Follow the project's coding style and commit guidelines

## Acknowledgement

API inference compute for running parity tests is generously supported by [2077AI](https://www.2077ai.com/) (https://www.2077ai.com/).
