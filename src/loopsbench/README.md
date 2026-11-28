# LoopsBench → Harbor Adapter

## Overview

This adapter converts LoopsBench tasks into Harbor tasks. The upstream manifest for `loopsbench-tasks-c47e13a.tar.zst` lists **112 upstream tasks**. The adapter selects **104 tasks** after eight exclusions. On oahu, the Harbor 0.27.0 oracle ran all 104 final task packages with separate verifiers. **97/104 scored 1.0** and 7 scored 0.0. [Full oracle result](evidence/oracle-harbor-0.27.0.png) · [Per-task rewards, package digests and execution environments at `74e0a92`](evidence/oracle-74e0a92.csv)

The adapter uses the upstream LoopsBench checkout at `f44331c` and task bundle revision `c47e13a`. The upstream repository is MIT-licensed. [Upstream repository](https://github.com/microsoft/Loopsbench)

## What is LoopsBench?

LoopsBench evaluates coding agents on long-horizon terminal tasks. An agent works in a task-specific workspace where it builds, modifies, or completes software. The upstream harness runs task-specific tests against the resulting workspace and uses the terminal test output to decide whether the task is resolved. [LoopsBench paper](https://arxiv.org/abs/2608.00267)

The source covers real-world project maintenance and coursework in domains such as databases, compilers, operating systems, graphics, and machine learning.

## Adapter Features

- Converts selected tasks from a local input directory, carrying over their instructions and mapping upstream IDs to stable `loopsbench/<task-id>` names.
- Packages the Docker build context, reference solution, and test assets for Harbor. The oracle runs the packaged upstream solution.
- Evaluates tasks with the packaged upstream test runner and a standard-library port of the upstream result parser. Harbor records a binary reward in `/logs/verifier/reward.txt`.

## Generated Task Structure

```text
datasets/loopsbench-reviewed/
└── <task-id>/
    ├── task.toml
    ├── instruction.md
    ├── environment/
    │   └── Dockerfile
    ├── solution/
    │   └── solve.sh
    └── tests/
        ├── test.sh
        └── _loopsbench/
```

The environment, solution, and test directories also contain the assets needed by each task. Tasks that require Compose capability overrides include `environment/docker-compose.yaml`.

## Run Evaluation / Harness

From the adapters repository root, use the reference config after reviewing the generated subset in `datasets/loopsbench-reviewed`. The config runs the oracle agent with Docker and reads only that directory. This is the local oracle workflow for the reviewed subset.

```bash
DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1 harbor run -c src/loopsbench/run_loopsbench.yaml
```

Job results are written to `jobs/loopsbench-reviewed/`. Use BuildKit for all tasks, including CS61B and Spring, whose Dockerfiles contain heredocs. The generator gives each set of local Dockerfile `COPY` inputs a content-derived path. This keeps BuildKit from reusing a different segment’s input when its relative path, size and timestamp match. The files copied into the image retain their upstream names and timestamps.

On oahu, `mlir-circuit-ir` scored 1.0 with the Harbor 0.27.0 oracle and 0.0 with Harbor’s `nop` agent, which submits the starting workspace. The staged upstream control resolved the same task (3 tests passed).

### Oracle results on oahu

The full run used adapter commit `74e0a92`, Docker Engine 24.0.2 and Compose 2.18.1. The physical host graded 62 tasks. A KVM guest on oahu graded the other 42 with Docker storage on a larger disk. Both used BuildKit and the same task-specific CPU, memory, network and timeout settings. Each reported score matched the final package digest, native Harbor trial result and verifier reward file.

Two host trials hit the disk-space limit before grading. They were rescheduled with their original package locks. A guest startup check and an image build also required retries. For `rails-seg09`, Compose stalled during cleanup after the verifier wrote the reward. Repeating cleanup let Harbor save the trial.

Seven tasks scored 0.0 in the full run.

| Task | Full run | Independent check |
| --- | --- | --- |
| `net-os-labs` | Two pipe tests timed out at 120 seconds each. The other 28 passed. | The upstream oracle failed the same two tests on oahu. [Control screenshot](evidence/upstream-net-os-labs.png) |
| `java-ds-coursework` | The 300-second test limit expired after pytest collected 52 cases and reported seven passes. | Reward 1.0 (52 pytest cases passed). |
| `jpf-symbolic-execution` | The 600-second test limit expired during the third test group. | Reward 1.0 (15 pytest cases passed). |
| `os-c-fs-labs` | APT retried the Ubuntu mirrors on the host. The 300-second test limit expired during the package-index update. | Reward 0.0. The package-index update timed out again. An earlier Harbor 0.27.0 run with the identical task digest passed all 42 tests. |
| `riscv-os-multiproject` | The 300-second test limit expired while the guest was installing system packages. | Reward 1.0 (134 pytest cases passed). |
| `spring-course-mgmt-fullstack` | Both test fixtures exceeded their 30-second limit starting MySQL. All 60 tests reported setup errors. | Reward 1.0 (60 pytest cases passed). |
| `x86-os-four-labs` | The 300-second test limit expired while the guest was unpacking system dependencies, before the test cases started. | Reward 1.0 (81 pytest cases passed). |

The standalone checks ran sequentially on the physical host after the full suite, using the same final packages and time limits. The full-run score above retains the original rewards. [Standalone results](evidence/standalone-oracle-checks.png)

On November 24, the unchanged `os-c-fs-labs` task passed all 42 pytest cases on both the Harbor oracle and the staged upstream oracle on oahu. The Harbor verifier fetched the Ubuntu indexes in two seconds. Both runs kept the original 300-second test limit and used the same verifier allowlist. Across the full run and the separate checks, 103 distinct final packages have a passing Harbor oracle result. The remaining task is `net-os-labs`. [Paired follow-up](evidence/os-c-fs-labs-control.png)

The earlier Harbor 0.26.0 run scored 103/104.

## Usage: Create Task Directories

Prepare a separate input directory containing only the upstream task directories selected for local review. Review each task’s instructions, Docker build context, dependencies, reference solution, and verifier code before including it. The local subset excludes offensive and security-lab tasks. Before running Harbor, review the generated directories too, including their environment settings and bundled scripts.

From the adapters repository root, set `REVIEWED_TASKS_DIR` to the absolute path of that reviewed input directory. The output directory must be new for this example so it contains only tasks from this generation.

```bash
cd src/loopsbench
REVIEWED_TASKS_DIR=/absolute/path/to/reviewed-loopsbench-inputs
test -d "$REVIEWED_TASKS_DIR" &&
  test ! -e ../../datasets/loopsbench-reviewed &&
  uv run loopsbench --tasks-dir "$REVIEWED_TASKS_DIR" \
    --output-dir ../../datasets/loopsbench-reviewed
```

`--tasks-dir` is required. `--task-ids` narrows selection within the reviewed input and accepts upstream or Harbor IDs. `--limit` caps the number of generated tasks, and `--overwrite` replaces complete selected tasks. An interrupted build can be retried with the original command. Incomplete outputs are rebuilt; the previous task stays in place until its replacement passes completeness checks. A directory containing an excluded task is rejected. Use a new output directory when migrating an older dataset. `--bundle` records the source bundle revision. `--cpus`, `--memory-mb`, and `--storage-mb` override the generated resource settings.

## Comparison with Original Benchmark (Parity)

### Proposed experiment

We propose Codex on both sides with the same pinned CLI release and GPT-5 mini snapshot. The Harbor team still needs to confirm the model, API provisioning, and run count. We propose a five-task smoke test (distributed-fs-chfs, evosuite-test-generation, navidrome-seg02, numpy-dl-aics-style, cocos2dx-physics2d-medium), followed by one paired run on all ten selected tasks and then two additional runs per side. Each run uses one attempt per task. Both sides must use the same prompt, model settings, tools, time limits and model-endpoint access.

Generate the proposed ten-task set from the reviewed inputs in a fresh output directory:

```bash
uv run --project src/loopsbench loopsbench --split parity \
  --tasks-dir "$REVIEWED_TASKS_DIR" --output-dir datasets/loopsbench-parity
```

The sample is fixed for bundle `c47e13a`. We grouped the 104 reviewed tasks by upstream difficulty and whether the Harbor ID ends in `-seg` followed by digits. We allocated ten places proportionally using largest remainders and took the lowest SHA-256 hashes of `c47e13a/<harbor-id>` in each group. Selection uses task metadata only.

| Group | Available | Selected tasks |
| --- | ---: | --- |
| Easy | 17 | distributed-fs-chfs, architecture-riscv-labs |
| Medium | 42 | numpy-dl-aics-style, cocos2dx-physics2d-medium, deep-learning-three-assignments, ligra-graph-evolution |
| Hard, unsegmented | 21 | evosuite-test-generation, boogie-dafny |
| Hard, segmented | 24 | navidrome-seg02, echarts-seg05 |

### Prepare and run paired Codex jobs

From the adapters repository root, use the ten generated Harbor packages above and the same reviewed source bundle. Set `PARITY_MODEL` to the agreed `openai/MODEL` snapshot, `OPENAI_BASE_URL` to the agreed HTTPS Responses API base URL, `CODEX_VERSION` to the exact npm release, and `CODEX_BIN_HOST` to the matching native Linux executable on the upstream run host. Use Harbor 0.27.0 and upstream f44331c. Preparation only writes files and does not require a key.

```bash
export PLAN="$PWD/parity-smoke"
uv run --project src/loopsbench python -m loopsbench.parity \
  --source "$REVIEWED_TASKS_DIR" --harbor datasets/loopsbench-parity \
  --checkout "$UPSTREAM_CHECKOUT" --split smoke --output "$PLAN" --model "$PARITY_MODEL" \
  --base-url "$OPENAI_BASE_URL" --version "$CODEX_VERSION" --binary "$CODEX_BIN_HOST"
```

For the two source tasks that request host networking, staging removes that setting so the native agent can use the same model-endpoint allowlist as Harbor. The plan copies each Harbor task and renders its first-round instruction with the pinned upstream harness and a fresh trial patch history. The Harbor job uses these plan-local copies. Each upstream job stages one matching task with its own verifier network policy and the generated Harbor build context. Both sides use one attempt, the same model endpoint and disabled web search. Upstream uses one outer-loop round. The original task and native grader are retained. Check the actual prompt, CLI version, time limits and model route in both smoke traces before running the full set.

To compare the exact UTF-8 task argument of both Codex command builders offline, run the following with Harbor 0.27.0's Python. The check intercepts the command before execution, uses a dummy credential and prints the byte length and SHA-256 of each matching pair.

```bash
env -i PATH=/usr/bin:/bin HOME=/tmp OPENAI_API_KEY=offline-placeholder \
  "$HARBOR_PYTHON" -I src/loopsbench/tests/offline_prompt_check.py "$PLAN" "$UPSTREAM_CHECKOUT"
```

The [offline comparison for the ten-task plan](evidence/parity-prompt-check.txt) records each matching command argument’s UTF-8 byte length and SHA-256 digest on Harbor 0.27.0 and upstream f44331c. It covers prompt preparation only.

After obtaining the parity allocation, export `OPENAI_API_KEY` in the launching shell. Run one paired round as follows. Use the executable from the pinned upstream checkout to avoid confusion with the generator CLI.

```bash
(
  set -eu
  uv run --project src/loopsbench python -m loopsbench.parity check-version "$PLAN"
  export DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1
  for cfg in "$PLAN"/*/run.yaml; do
    "$UPSTREAM_CHECKOUT/.venv/bin/loopsbench" run -c "$cfg"
  done
  harbor run -c "$PLAN/harbor.yaml"
)
```

For each of the three formal rounds, prepare a fresh plan directory with `--split full`. Here `full` means all ten parity tasks. Finish and check both sides of the first round before preparing the next. A fresh plan gives each round separate results directories.

### Collect a completed round

Keep the plan and raw outputs together. In each upstream per-task run, read the trial in `results.json` under the configured `output_path`. Record its `task_id`, `failure_mode` and Boolean `is_resolved`. In each Harbor trial's `result.json` under the plan's `harbor-results`, record the task identity, `exception_info` and `verifier_result.rewards.reward`. Also retain the Harbor job summary. Normalize upstream IDs to the corresponding plan task IDs. Review exceptions, missing outcomes and repeat attempts against the trial logs before scoring.

For each formal round, check that each side has exactly one graded binary outcome for each of the same ten tasks. Keep the five-task smoke run separate. Each side's round score is its number of resolved tasks divided by ten. For three rounds, report the three raw scores per side, their arithmetic mean and the sample standard deviation divided by the square root of three. Record whether the two observed score ranges overlap. Keep the same units in the README and the results JSON.

### Results

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
| --- | --- | --- | --- | --- | --- | --- |
| Pending | Pending | Resolved rate | — | — | — | — |

Both sides will use the same reviewed tasks and matched settings, with at least two runs per side. The reported scores will use mean ± sample SEM from the raw runs in `parity_experiment.json`, with matching assessed by overlap of the run-score ranges.

For a single upstream oracle comparison, stage the original task with the generated Harbor build context, verifier network policy, CPU limit and memory limit. The helper creates a new directory for each comparison and writes its native LoopsBench run configuration. The upstream harness still runs its own oracle and grader. Run these commands from the adapters repository root with the pinned LoopsBench checkout and reviewed source directory available.

```bash
export UPSTREAM_CHECKOUT=/absolute/path/to/Loopsbench
export REVIEWED_TASKS_DIR=/absolute/path/to/reviewed-loopsbench-inputs
staging_parent=$(mktemp -d)
uv run --project src/loopsbench python -m loopsbench.verifier_network prepare-upstream \
  --source-task "$REVIEWED_TASKS_DIR/task_mlir_circuit_ir" \
  --harbor-task datasets/loopsbench-reviewed/mlir-circuit-ir \
  --output-dir "$staging_parent/comparison"
DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1 "$UPSTREAM_CHECKOUT/.venv/bin/loopsbench" run \
  -c "$staging_parent/comparison/run.yaml"
```

## Notes & Caveats

- `angr-concolic-execution` and `security-labs` are excluded from task generation. They require vulnerability-trigger generation or attack implementation.
- `django-seg11`, `llvm-alive-verification`, and `typescript-seg01` are excluded because the reference solution fails the same tests in Harbor and the upstream harness.
- `framework-seg04` is excluded after Composer blocked its Laravel requirement on security advisories. `jenkins-seg02` and `jenkins-seg05` are excluded after their Harbor builds failed during Maven archive extraction. All three upstream comparisons ended at the Docker build time bound.
- The adapter removes Dockerfile instructions that copy reference patches into the agent image for upstream tasks `mlir_circuit_ir` and `mlsys_autodiff_pa1`. The reference patches remain in the oracle solution assets. A parity comparison needs the same correction on both sides.
- Harbor snapshots `/workspace` after the agent phase, then restores it in a fresh verifier container from the same image. The archive preserves ignored build outputs, file timestamps, and workspace links. Verifier assets and reward files are created outside the restored workspace. For bundle `c47e13a`, 35 verifiers run offline and 69 use task-specific host allowlists for dependency installation. Harbor and the upstream comparison helper read the same [policy file](src/loopsbench/verifier_network.toml).
- The upstream images for `mlir-circuit-ir` and `deep-learning-three-assignments` expose tests to the agent. The MLIR instructions direct the agent to run them. The verifier receives a fresh copy of the grading tests.
- The adapter carries over Compose capabilities where needed. Upstream host networking and custom bridge networks are omitted.
- The Dockerfiles for `navidrome-seg02` and `node-seg03` restore the original cyclic symlinks inside the image. This lets Harbor fingerprint each task directory while preserving its in-container layout.
- The `node-seg03` Dockerfile uses the official Debian security archive for its Bullseye security packages after the original mirror began returning HTTP 404. APT signature verification stays enabled.
- The `db-nanodb` image sets a one-second mtime on the packaged `test_sql.props` resource. Maven copies it to the test classpath with this timestamp, and the Harbor oracle passes all 18 tests.
- The `nodebb-seg05` image supplies Mocha 9.1.3 for the verifier's `npx mocha` command. This version supports the task's Node 14 runtime and keeps the workspace's dependencies intact.
- The `perses-program-reduction` image installs `patch` for the upstream oracle script.
- The `spring-course-mgmt-fullstack` image normalizes epoch-zero Maven resource mtimes so the existing build copies the packaged configuration files.
- On oahu the upstream `net-os-labs` oracle at `f44331c` and the Harbor 0.27.0 oracle each passed 28 of 30 tests. The named-pipe and unnamed-pipe tests each exceeded their original 120-second limit in both runs, giving the task a score of zero. The upstream control matched the earlier Harbor 0.26.0 run with 4 CPUs, 8 GiB, CPU set 2–5 on NUMA node 0 and public verifier networking. It used the original task Dockerfile and tests. The 0.27.0 verifier ran offline. [Upstream control result](evidence/upstream-net-os-labs.png)
- The two Panda3D images normalize epoch-zero source mtimes because `makepanda` treats those timestamps as missing files. They default to 8 CPUs and limit `makepanda` to 6 threads. During verification, `makepanda` installs the pinned `panda3d-interrogate` wheel from PyPI.
- `compiler-rustlike-rv32` defaults to 16 CPUs. `ns3-network-simulation` and `rdma-ns3-evolution` default to 16 CPUs and 16 GiB. All three complete within their upstream test limits with those allocations. The standard allocation is 4 CPUs and 8 GiB. Explicit `--cpus` and `--memory-mb` values override task defaults.

## Installation / Prerequisites

- Python 3.12 or later and `uv` for the adapter package. Run `uv sync` from `src/loopsbench` to install the locked dependencies.
- Harbor CLI `0.27.0` (install with `uv tool install harbor==0.27.0`), Docker Compose, and Docker BuildKit. The generated task configuration uses `version = "1.0"`.
- A running Docker engine, adequate disk space for task images and build caches, and a prepared local directory containing the reviewed upstream task subset.

## Troubleshooting

- **Dataset path missing.** Run the reference config from the adapters repository root and confirm the reviewed output exists at `datasets/loopsbench-reviewed`.
- **Symlink recursion.** Regenerate older outputs for `navidrome-seg02` and `node-seg03` in a new directory so Harbor can fingerprint them with the updated Dockerfiles.
- **Build or verification failure.** Check the Harbor job logs for Docker build errors, dependency installation failures, or verifier timeouts. Record any confirmed upstream failure and its eventual disposition before finalizing the adapted task count.

## Citation

The title and author list below follow the upstream `CITATION.cff`.

```bibtex
@article{li2026loopsbench,
  title = {LoopsBench: From Harness Engineering to Loop Engineering in Coding Agent Evaluation},
  author = {Li, Han and Fang, Zhemin and Feng, Rili and Zhao, Yingqi and Liu, Jiaheng and Gao, Pengfei and Ye, He and Lin, Dayi and Lin, Qingwei and Rajmohan, Saravan and Zhang, Dongmei},
  journal = {arXiv preprint arXiv:2608.00267},
  year = {2026},
  url = {https://arxiv.org/abs/2608.00267}
}
```

## Authors & Contributions

LoopsBench was authored by Han Li, Zhemin Fang, Rili Feng, Yingqi Zhao, Jiaheng Liu, Pengfei Gao, He Ye, Dayi Lin, Qingwei Lin, Saravan Rajmohan, and Dongmei Zhang. Boyuan Zheng contributes the Harbor adapter. [Upstream citation metadata](https://github.com/microsoft/Loopsbench/blob/f44331c/CITATION.cff)
