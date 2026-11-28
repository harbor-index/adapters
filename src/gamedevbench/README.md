# GameDevBench → Harbor Adapter

## Overview

This adapter converts all **333 tasks** in Wayne Chi and colleagues’ GameDevBench into Harbor tasks. Each task pairs a starter Godot project from `tasks/` with a reference project from `tasks_gt/`. The Harbor oracle passed **333/333**. On a fixed 30-task sample, the upstream runner and Harbor both averaged **76.67% pass@1**. [Original repository](https://github.com/waynchi/gamedevbench) · [Adapted revision](https://github.com/waynchi/gamedevbench/tree/54589a477d6c1a15255707a861db6b01126797aa)

## What is GameDevBench?

An agent edits an existing Godot game to satisfy a natural-language request derived from a tutorial. Tasks cover 2D and 3D graphics, animation, user interfaces and gameplay logic. The submitted work can include scenes, GDScript, shaders and visual assets. An executable Godot test grades the resulting project. Each task earns 0 or 1, and the benchmark score is the fraction of tasks passed. [Project and leaderboard](https://waynechi.com/gamedevbench/) · [Paper](https://arxiv.org/abs/2602.11103)

## Adapter Features

| Harbor component | GameDevBench source and behavior |
| --- | --- |
| Agent workspace | The adapter generates the prompt with upstream `create_task_prompt` and applies the upstream file filter to the starter project. The agent edits the project in `/workspace`. |
| Verifier | Harbor transfers the finished workspace to a separate container. The verifier checks the formal test files against their upstream hashes, imports the project with Godot 4.4.1 and invokes the upstream validator under strict Bubblewrap confinement. |
| Oracle | Harbor installs the reference project from `tasks_gt/` into the workspace. It is graded with the same formal tests from `tasks/` used for agent submissions. Four reference projects have documented adjustments. |

## Generated Task Structure

```text
datasets/gamedevbench/
├── dataset.toml
├── generation_manifest.json
└── gamedevbench-task-0002/
    ├── task.toml
    ├── instruction.md
    ├── source_manifest.json
    ├── environment/project-task_0002/       # Agent’s starter project
    ├── environment/Dockerfile
    ├── solution/solve.sh
    ├── solution/project/                    # Oracle reference project
    ├── tests/test.sh
    ├── tests/Dockerfile
    └── tests/payload-task_0002/official/    # Upstream formal tests
```

The dataset manifest records the upstream revision and task list. Each task’s source manifest records the starter and reference archive hashes, formal test hashes and compatibility settings.

## Installation / Prerequisites

Task generation requires Python 3.11+, uv, Git LFS and the pinned upstream checkout with its task archives materialized. For evaluation, use Linux x86-64, Python 3.12+, Docker with Compose v2 and Buildx, and a host that allows nested Bubblewrap namespaces. The public Dockerfile installs Godot 4.4.1 and the upstream runtime dependencies. The recorded evaluations used Harbor 0.23.0. [Harbor revision](https://github.com/harbor-framework/harbor/tree/99218a4611e3bd76e49ce3b5f977e8c8134ad3ac)

## Usage: Create Task Directories

Run these commands from the **adapters repository root**. Replace `/path/to/gamedevbench` with the pinned upstream checkout. For all 333 tasks:

    uv run --frozen --project src/gamedevbench gamedevbench --source-dir /path/to/gamedevbench --output-dir datasets/gamedevbench

For the same 30 tasks used in the parity experiment:

    uv run --frozen --project src/gamedevbench gamedevbench --source-dir /path/to/gamedevbench --split parity --output-dir datasets/gamedevbench-parity

Use `--task-ids task_0002` for a single task, or `--limit N` for the first N tasks. The public Docker recipe is the default. Use `--runtime-image` to supply a prebuilt shared runtime. See `gamedevbench --help` for the full CLI.

## Run Evaluation / Harness

From the adapters repository root, run the oracle on the generated tasks:

    harbor run -p datasets/gamedevbench -a oracle -n 2 --max-retries 0

The oracle runs offline. Harbor allows 600 seconds for the agent and 1,320 seconds for verification, which includes project import and testing. Each environment requests two CPUs and 4 GiB of memory. Use the local dataset path while registry publication is pending. [Example job configuration](run_gamedevbench.yaml) · [Dataset registration PR](https://github.com/harbor-framework/harbor-datasets/pull/319)

### Oracle validation

| Docker runtime | Tasks passed | Execution errors |
| --- | ---: | ---: |
| Prebuilt shared runtime, full dataset | 333/333 | 0 |
| Public Dockerfile, single-task build and oracle check | 1/1 | 0 |

All 333 full-run trials earned reward 1 with the original formal test hashes. [Terminal screenshot](docs/oracle-terminal-screenshot.png) · [Run summary](docs/oracle-summary.json) · [Captured output](docs/oracle-terminal.txt)

## Comparison with Original Benchmark (Parity)

Both runners used Codex 0.171.0, model ID `gpt-6.1-sol`, xhigh reasoning effort and a 600-second agent timeout. The experiment ran on Docker with ModelRouter. Harbor used its built-in Codex agent and the original benchmark used its upstream Codex runner.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Performance | Harbor Performance |
| --- | --- | --- | --- | --- | --- | --- |
| Codex 0.171.0 | GPT-6.1 Sol (xhigh) | pass@1, mean ± sample SEM | 3 per runner | 30/333 (9.01%) | 76.67% ± 5.09 pp | 76.67% ± 0.00 pp |

The original runner passed **25, 24 and 20** tasks in successive runs. Harbor passed **23, 23 and 23**. Both runners passed 69 of 90 scored trials. SEM is calculated across the three run percentages and reported in percentage points. [Experiment metadata](parity_experiment.json) · [Per-task results](https://huggingface.co/datasets/TianshiXu/gamedevbench-parity-experiments/blob/d1e2329f2b0464cd61891b05706465c53f35dd12/adapters/gamedevbench/paired_tasks.csv)

**Sample selection.** The sample was fixed before inference. It includes both tasks that needed display settings on Linux, one task that already required a display upstream, 12 headless variants and 15 other headless tasks. Within each group, selection used a hash of the pinned revision and task ID, taking at most one task per variant lineage and three per known source repository. The packaged manifest lists all 30 IDs and the selection rules. [Parity manifest](src/gamedevbench/tasks.parity30.yaml)

**Reproduction.** Follow the linked instructions to prepare the 30 projects and start the provider gateway. From the evidence directory, start one command per runner in separate terminals. After both finish, repeat for rounds 2 and 3 with the matching round number and job name. [Setup, configuration and trajectories](https://huggingface.co/datasets/TianshiXu/gamedevbench-parity-experiments/blob/d1e2329f2b0464cd61891b05706465c53f35dd12/adapters/gamedevbench/README.md)

```bash
bash reproduce/run-original.sh 1
harbor run -c config.yaml --job-name parity-run1 --max-retries 0
```

## Notes & Caveats

| Affected tasks | Compatibility change |
| --- | --- |
| 0030, 0059 | Enable a display during validation. |
| 0051, 0052 | On case-sensitive Linux filesystems, supply a missing scene-name alias: `Main.tscn` resolves to `main.tscn` in 0051, and `player.tscn` resolves to `Player.tscn` in 0052. |
| 0082 | The oracle retains the 15 walk atlas regions and repeats two existing frames on each cyclic run timeline to meet the formal 17-frame check. |
| 0089 | The oracle retains the combined statistics label and adds the individual labels checked by the formal test. |
| 0107 | The oracle defers the shader swap until the tested method is called with the original base materials. |
| 0112 | The oracle places the wall frame on the border of the Background rectangle. |

The last four changes apply only to the reference projects. Select `--oracle-profile original` to generate the filtered references without those changes. All profiles use the upstream formal tests. [Upstream reference PR](https://github.com/waynchi/gamedevbench/pull/16)

The separate verifier uses `SYS_ADMIN`, `NET_ADMIN` and unconfined AppArmor and seccomp profiles for the upstream sandbox. A six-task public build previously timed out before grading with a 1,200-second setup budget. The public build allowance is now 3,600 seconds. Daytona support and campaign acceptance of the Docker/ModelRouter runs are pending. [Parity evidence PR](https://huggingface.co/datasets/harborframework/parity-experiments/discussions/323)

## Troubleshooting

- **Task archive checksum mismatch.** Check the upstream revision and run `git lfs pull` in that checkout. A Git LFS pointer in place of the ZIP will fail the manifest check.
- **Verifier fails at `pivot_root` or mounting `/proc`.** Check whether the host applies the verifier’s Compose permissions and supports nested namespaces. The verifier records the exception and validation details in `/logs/verifier/official_result.json`.

## Citation

```bibtex
@misc{chi2026gamedevbenchevaluatingagenticcapabilities,
  title={GameDevBench: Evaluating Agentic Capabilities Through Game Development},
  author={Wayne Chi and Yixiong Fang and Arnav Yayavaram and Siddharth Yayavaram and Seth Karten and Qiuhong Anna Wei and Runkun Chen and Alexander Wang and Valerie Chen and Ameet Talwalkar and Chris Donahue},
  year={2026},
  eprint={2602.11103},
  archivePrefix={arXiv},
  primaryClass={cs.AI},
  url={https://arxiv.org/abs/2602.11103}
}
```

## Authors & Contributions

Wayne Chi and colleagues created GameDevBench and released the benchmark under Apache-2.0. Individual task assets retain their upstream attribution. Tianshi Xu (`tianshixu@stu.pku.edu.cn`) contributed the Harbor adapter. [Original benchmark](https://github.com/waynchi/gamedevbench) · [Adapter metadata](adapter_metadata.json)
