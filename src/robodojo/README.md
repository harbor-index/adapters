# RoboDojo → Harbor Adapter

## Overview

This draft adapts the [RoboDojo](https://robodojo-benchmark.com/) simulation track to Harbor. It generates 42 canonical task directories and evaluates an already running native XPolicyLab policy server. GPU execution, a passing oracle, parity experiments, and registry publication are pending.

The simulator is pinned to [RoboDojo `f57e1633ffed59761bc8e568cfe7530c4b33381c`](https://github.com/RoboDojo-Benchmark/RoboDojo/tree/f57e1633ffed59761bc8e568cfe7530c4b33381c), whose XPolicyLab gitlink is `5d0be1499634d3176258c8f2e31f5fe816497b0a`. This is an endpoint integration prototype; standard Harbor CLI agents do not yet have an embodied control interface.

## What is RoboDojo?

[RoboDojo](https://arxiv.org/abs/2607.04434) evaluates generalist robot manipulation policies in simulation and on physical robots. Its simulation track contains 42 bimanual tasks on ARX X5: Generalization (12), Memory (6), Precision (8), Long-Horizon (8), and Open (8). All 18 real-world tasks are excluded from this adapter.

The simulation uses Isaac Sim, Isaac Lab, CuRobo, and XPolicyLab. Evaluation reports success rate and a partial-progress score. Generalization tasks combine 25 standard and 25 randomized episodes; each other task uses 50 episodes. The 12 randomized counterparts bring the runtime inventory to 54 configurations, not 54 canonical tasks. The overall benchmark score averages the five dimensions equally.

## Adapter Features

- Generates stable Harbor task names with task selection, limits, and overwrite control.
- Invokes the original simulator client, task instructions, observations, deployment modules, and reward checks. Each Generalization verifier runs both upstream variants.
- Writes `reward.json` with `success_rate` and `score`, both in `[0, 1]`. The native percentage score is divided by 100; no partial-progress threshold is introduced.
- Requires complete native episode results before reporting a reward. Submitted scores and incomplete evaluations are not accepted.
- Provides `robodojo.policy_agent:EndpointPolicyAgent`, which writes the external policy endpoint submission. It does not start, train, or host a robot policy.
- Provides aggregation across all 42 tasks with equal weighting of the five dimensions.

The submission is `/workspace/submission.json`:

```json
{"policy_name": "GR00T_N17", "host": "policy-server.example.invalid", "port": 9999}
```

`policy_name` selects an existing module under the pinned `XPolicyLab/policy/` tree. The already running server must implement that deployment module's native WebSocket interface. Replace the example hostname with an address reachable from the simulator container. Separately record the checkpoint, dependencies, policy RNG seed, and server configuration; an endpoint alone does not identify a model reproducibly.

## Generated Task Structure

```text
datasets/robodojo/
└── <canonical-task-id>/
    ├── task.toml
    ├── instruction.md
    ├── environment/
    │   ├── Dockerfile
    │   └── docker-compose.yaml
    ├── solution/
    │   └── solve.sh
    └── tests/
        └── test.sh
```

The environment uses a locally prepared pinned simulator image and mounts existing assets read-only. The verifier runs the original simulator client with the generated task's fixed seed and native episode counts. `solution/solve.sh` exits with an explicit message because no passing oracle has been provided.

## Run Evaluation / Harness

Registry execution is unavailable: this draft has not been published. Use local generated tasks during development.

From the adapters repository root, install this package with its optional Harbor dependency:

```bash
uv sync --project src/robodojo --extra harbor
```

Use the project environment for execution so Harbor can import the custom agent. Record the actual Harbor revision used for experiments; the adapter contract's compatibility baseline is `99218a4611e3bd76e49ce3b5f977e8c8134ad3ac`.

Start the policy server using the pinned XPolicyLab policy's own instructions. Edit `src/robodojo/run_robodojo.yaml` to set its `policy_name`, `host`, and `port`, then run:

```bash
uv run --project src/robodojo --extra harbor harbor run -c src/robodojo/run_robodojo.yaml
```

The supplied hostname is an example and must be replaced. The configuration uses one concurrent trial because the native policy server is stateful. Its default is the endpoint agent rather than oracle: no validated oracle is available, so the standard oracle-default template cannot honestly be used.

After a complete run, aggregate the Harbor job directory from this adapter directory:

```bash
cd src/robodojo
uv run python -m robodojo.aggregate /path/to/harbor-job --output /path/to/summary.json
```

Aggregation requires exactly 42 unique completed tasks with the same seed and rejects failed or incomplete trials. It does not report a full-benchmark score for a subset. Aggregate repeated runs separately before calculating parity mean and sample SEM. A flat average of 42 Harbor task rewards is not the official overall RoboDojo score.

## Usage: Create Task Directories

Run from `src/robodojo`. `--assets-dir` must be an absolute path to an existing local `Assets/` directory:

```bash
uv sync
uv run robodojo \
  --output-dir ../../datasets/robodojo \
  --assets-dir /absolute/path/to/RoboDojo/Assets
```

The default image is `robodojo-harbor:f57e1633ffed`, and the default seed is `0`. For a subset:

```bash
uv run robodojo \
  --output-dir /path/to/task-output \
  --assets-dir /absolute/path/to/RoboDojo/Assets \
  --image robodojo-harbor:f57e1633ffed \
  --seed 1 \
  --task-ids stack_bowls play_Xylophone \
  --limit 2 \
  --overwrite
```

`--task-ids` accepts canonical upstream identifiers, preserving their original case and underscores. Randomized variants belong to their Generalization task and are not selected separately. The seed must be nonnegative and have matching layout assets. Regenerate tasks when changing the image, seed, or absolute asset path.

## Comparison with Original Benchmark (Parity)

No parity experiments have run. `parity_experiment.json` is an empty array, not estimated results. There are no matching-score claims, oracle screenshots, dataset PRs, or uploaded parity artifacts.

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|

The proposed comparison uses the same pinned simulator and deployment module, checkpoint, policy environment, policy RNG seed, layout seed, episode counts, and GPU setup on both sides. Coordinate the agents, models, subset, and repeated runs with the Harbor team before running parity. The adapter contract requires at least two runs per side.

For the original side, use an installed checkout of the [pinned upstream repository](https://github.com/RoboDojo-Benchmark/RoboDojo/tree/f57e1633ffed59761bc8e568cfe7530c4b33381c), prepared assets, and the same policy server. One Generalization task requires both commands:

```bash
env -u EVAL_NUM bash scripts/robodojo.sh client \
  --task stack_bowls --policy-name GR00T_N17 \
  --policy-host POLICY_HOST --policy-port 9999 \
  --env-cfg arx_x5 --seed 0 --ckpt external --action-type ee --eval-num native

env -u EVAL_NUM bash scripts/robodojo.sh client \
  --task stack_bowls_random --policy-name GR00T_N17 \
  --policy-host POLICY_HOST --policy-port 9999 \
  --env-cfg arx_x5 --seed 0 --ckpt external --action-type ee --eval-num native
```

The full original runtime inventory can be run through its batch client:

```bash
env -u EVAL_NUM bash scripts/robodojo.sh client \
  --policy-name GR00T_N17 --policy-host POLICY_HOST --policy-port 9999 \
  --env-gpu-ids 0 --dimension all --env-cfg arx_x5 --seed 0 \
  --ckpt external --action-type ee --eval-num native
python scripts/internal/summarize_result.py
```

Replace the policy and host consistently on both sides. Preserve raw per-episode results. The upstream summary uses standard deviation across its seeds, while Harbor parity requires sample SEM across repeated run scores. For the Harbor side, regenerate tasks with the matching seed, run `run_robodojo.yaml`, and use `robodojo.aggregate`. Repeat both sides under the agreed plan, then populate parity JSON, raw run arrays, the comparison table, and artifact links.

## Notes & Caveats

- This is a draft, not a review-ready adapter. A passing oracle, actual GPU execution, parity evidence, and dataset registration remain outstanding. The upstream `demo_policy` is a zero-action transport test and must not be described as a passing oracle.
- All 18 real-world tasks are excluded: physical hardware, scene resets, and the remote evaluation service are not packaged.
- The custom agent configures evaluation of an external native policy. Standard CLI agents do not yet have an embodied control tool or image-observation loop here. Their original affordances and comparable performance are not claimed.
- Native per-episode instructions and observations still come from the simulator and pinned deployment code. The Harbor endpoint-submission instruction is not substituted for the robot policy instruction.
- The simulation seed selects layout assets. It does not set the external policy server's RNG seed or checkpoint; pin and record these separately for parity.
- No upstream prompt, reward, physics, action-interface, or episode-count modifications are intended. Joining standard and randomized variants preserves the 42-task benchmark definition. Overall aggregation weights the five dimensions equally.
- The operator supplies existing assets and a running policy server. This adapter does not download assets or checkpoints, install model dependencies, or accept asset terms on the operator's behalf.
- The optional image script builds the upstream Dockerfile, including its upstream license-consent environment variables. Review the pinned Dockerfile and applicable terms before building.
- Generated tasks embed an absolute host asset path. Assets are mounted both at `/workspace/RoboDojo/Assets` and their original host path to resolve CuRobo configurations with embedded absolute paths. Regenerate tasks for another host.
- The image preparation script adds revision markers and the OCI source-revision label. These support consistency checks; they are not a secure provenance attestation or an immutable image digest.
- Simulator resources and the 75,600-second verifier timeout are provisional. They require calibration on the target GPU; no measured completion-time claim is made.
- Agent/verifier environment sharing is not a hardened security boundary. Further isolation and a standard embodied agent interface require review before publication for adversarial agents.

## Installation / Prerequisites

Task generation needs Python 3.12+ and `uv`. Simulator execution needs Linux x86_64, an NVIDIA GPU with driver 570 or newer, Docker, and NVIDIA Container Toolkit. The upstream image contains Isaac Sim 5.1, Isaac Lab, and CuRobo. Upstream estimates an approximately 200 GB image and recommends 300 GB free disk including build cache. A Mac can generate tasks but cannot run this CUDA simulator locally.

On the GPU host, run from `src/robodojo`:

```bash
bash scripts/prepare_image.sh
# Optional image tag and new checkout directory:
bash scripts/prepare_image.sh robodojo-harbor:f57e1633ffed /path/to/new/RoboDojo-checkout
```

The script clones the pinned source and submodules, builds the upstream image, and adds revision markers in a separate layer. Existing checkout directories must be clean and at the exact pinned revision; they are never reset. It does not download `Assets/`.

Prepare assets separately using the [pinned upstream asset instructions](https://github.com/RoboDojo-Benchmark/RoboDojo/blob/f57e1633ffed59761bc8e568cfe7530c4b33381c/scripts/init_assets.sh). Record the dataset revision and path preparation. Assets must include robots, objects, materials, and evaluation layouts for the chosen seed. Prepare the external policy environment and checkpoint using the pinned XPolicyLab instructions.

Run the local adapter checks from `src/robodojo`:

```bash
uv run --extra harbor python -m unittest discover -s tests -v
```

These checks exercise generation and result handling without proving that the GPU simulator runs or that an oracle passes.

## Troubleshooting

- **Missing Docker or GPU:** generate locally, then regenerate and execute on a supported Linux NVIDIA host. Check `nvidia-smi` and NVIDIA Container Toolkit.
- **Server unreachable:** use an address reachable from inside the task container. Container `localhost` does not refer to the host. Set the server binding, hostname, and port consistently.
- **Missing deployment module:** select a `policy_name` available under pinned `XPolicyLab/policy/`. The simulator requires its `deploy.py` even when the model is remote.
- **Missing assets or robot paths:** verify the asset directory, selected seed's layouts, and absolute CuRobo configuration paths. Regenerate tasks after changing the host asset path.
- **Slow first startup:** Isaac extensions, shaders, and CuRobo kernels may initialize slowly. Consult upstream Docker documentation before changing execution settings.
- **Incomplete results:** inspect verifier logs and native episode details. Infrastructure failures are not completed evaluations; do not fill missing episodes with successes or present partial suites as complete.
- **Oracle fails immediately:** expected until a real reference policy is provided and validated. The zero-action demo is not a replacement oracle.

## Citation

```bibtex
@article{chen2026robodojo,
  title={{RoboDojo}: A Unified Sim-and-Real Benchmark for Comprehensive Evaluation of Generalist Robot Manipulation Policies},
  author={Chen, Tianxing and Chen, Yue and Li, Zixuan and Tang, Junyuan and Su, Kailun and Wan, Weijie and Chen, Baijun and Lu, Haoran and Yan, Haowen and Su, Honghao and others},
  journal={arXiv preprint arXiv:2607.04434},
  year={2026}
}
```

## Authors & Contributions

The [original RoboDojo authors](https://github.com/RoboDojo-Benchmark/RoboDojo) provide the benchmark, task definitions, simulator, and scoring. XPolicyLab provides the native policy deployment interface. Adapter builder: Jia Guo (`jia@green.microsoft.com`). This draft contributes Harbor packaging, endpoint submission, native-evaluation orchestration, result validation, and dimension-aware aggregation; no benchmark results are claimed.
