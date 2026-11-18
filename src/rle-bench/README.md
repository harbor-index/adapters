# RLE-Bench → Harbor

## Overview

This adapter exports a default CPU split and an opt-in GPU split from RLE-Bench
into independently buildable Harbor task directories. It preserves native
instructions, reference submissions, verifier code, numeric scoring, hidden
scenarios and resource budgets.

- Upstream: [RLE-Bench/RLE-Bench](https://github.com/RLE-Bench/RLE-Bench).
- Source revision: `c6bf572283b5453c0ee76eaa3054f23d691e433d`.
- Harness: `harbor==0.21.0`, matching that upstream revision.
- Dataset identifiers `rlebench/rle-bench-cpu` and `rlebench/rle-bench-gpu`.
- Oracle equivalence validated locally for the tasks listed below. Registry
  publication remains pending.

## What is RLE-Bench?

RLE-Bench evaluates coding agents acting as robotics engineers. Agents develop
perception algorithms, robot policies, mechanical designs and controllers;
simulators evaluate their submitted behavior. The splits select tasks from the
nine-family, 51-task upstream matrix.

| Exported task ID | Native selector | Agent work | Native oracle |
| --- | --- | --- | --- |
| `task06-rgb-only` | `task06/rgb-only` | Estimate shape and planar pose from RGB | RGB reference estimator |
| `task06-rgb-depth` | `task06/rgb-depth` | Estimate shape and pose from RGB-D | Fused reference estimator |
| `task07` | `task07` | Clear a bin with a magnet-tipped Panda | Public depth baseline policy |
| `task08` | `task08` | Design a common mobile base and shelf controller | Reference chassis and controller |
| `task09` | `task09` | Co-design three GELLO lead arms and compensation | Three pinned hardware/software submissions |
| `task02-<group>` | `task02/<group>` (GPU) | Develop reusable robot skills on three training tasks and transfer them to five held-out trials in each of 15 groups | Upstream protocol smoke Oracle |
| `task03-01-tower-max-height` | `task03/01-tower-max-height` (GPU) | Build the tallest stable tower | Replay of upstream recorded controls |
| `task03-05-hidden-center-of-mass` | `task03/05-hidden-center-of-mass` (GPU) | Identify each box's hidden center-of-mass quadrant | RGB tilt estimate |
| `task06-method-agnostic` | `task06/method-agnostic` (GPU) | Estimate shape and pose from RGB-D with any method | RGB-D fusion reference estimator |
| `task06-rgb-depth-model-training` | `task06/rgb-depth-model-training` (GPU) | Train a TorchScript RGB-D pose estimator from scratch | Upstream CNN trained with deterministic CUDA |

## Adapter Features

- Fetches an exact source commit, or reads a clean checkout at that commit.
  Asset generation runs in an owned temporary snapshot; the supplied checkout is
  never used as a staging directory.
- Copies instructions, reference training code and verifier code verbatim.
  A per-task SHA-256 manifest records the copied files.
- Runs the task06 training Oracle through a launcher that enables deterministic
  CUDA operations and calls the upstream trainer.
- Removes local prebuilt-image references so Harbor builds from the task context.
  Task02 and Task03 package the pinned simulator and task recipe as Docker stages.
- Preserves upstream verifier Dockerfile `COPY` paths by bundling the necessary
  repository-root files inside the task's `tests/` build context.
- Preserves native verifier permissions and environment mode, artifact paths,
  resource limits, timeouts, image dimensions, episode counts and reward metrics.
- Uses standard Harbor agents; no new agent implementation is introduced.
- Uses `--split gpu` to select the GPU catalog and protects non-generated output
  directories.

## Generated Task Structure

```text
datasets/rle-bench-cpu/
├── dataset.toml
├── rle-bench-source.json
├── LICENSE
├── THIRD_PARTY_NOTICES.md
├── task06-rgb-only/
├── task06-rgb-depth/
├── task07/
├── task08/
└── task09/
    ├── task.toml
    ├── instruction.md
    ├── environment/          # original Dockerfile and public assets
    ├── solution/             # original solve.sh and generated payload
    └── tests/                # independent verifier Docker build context
        ├── Dockerfile
        ├── test.sh
        ├── rle-bench-provenance.json
        ├── rlebench/         # original shared verifier utilities
        └── tasks/task09/tests/
            ├── harness/
            ├── models/
            ├── score_task.py
            └── test.sh
```

Task07 retains its native separate verifier layout. Task02 and Task03 run the
native root-owned simulator and verifier in the same container as the
unprivileged agent, with the original private directories and control socket
permissions. Task02 keeps the
`develop` and five `eval_01`–`eval_05` steps, with their collect hooks and final-step reward.
The hooks freeze `/workspace/agent_harness` after development and restore the
same files for each evaluation agent.
The other tasks use the nested separate verifier context above. The package's
`task-template/` contains the native task08 layout for adapter tooling.

## Run Evaluation / Harness

Registry execution will be available after publication. Until then, generate the
tasks and run them locally from this adapter directory:

```bash
uv run harbor run -c run_rle-bench.yaml

# One task, with the original scoring and resource limits:
uv run harbor run -p datasets/rle-bench-cpu/task08 -a oracle -n 1 \
  --ve RLEBENCH_MEDIA=0 --yes

# All five tasks:
uv run harbor run -p datasets/rle-bench-cpu -a oracle -n 5 \
  -o jobs/harbor --job-name oracle --ve RLEBENCH_MEDIA=0 --yes
```

Oracle execution needs no model API key. The media switch disables informational
recordings. Each task writes `verifier/reward.json`. Task02 and Task03 also write
`verifier/diagnosis.json`. Task02 places both files under the respective
`steps/<step-name>/` directory.
The remaining tasks write a detailed `verifier/report.json`.

Run the GPU split on one NVIDIA GPU with the original Compose passthrough. Use
`-n 1` to run serially on one card. `--override-gpus 0` lets the task's Compose
file handle device attachment in Harbor 0.21.0.

### Task02 RoboCasa assets

Prepare the pinned RoboCasa assets in the upstream checkout for Task02.

```bash
upstream=/path/to/pinned/RLE-Bench
(cd "$upstream" && make sim-robocasa)
export ROBOCASA_ASSET_DIR="$upstream/third_party/robocasa/robocasa/models/assets"
```

Compose mounts the RoboCasa assets read-only.

### Run the GPU split

```bash
uv run rle-bench --split gpu --output-dir datasets/rle-bench-gpu
RLEBENCH_GPU=0 uv run harbor run -p datasets/rle-bench-gpu -a oracle -n 1 \
  --override-gpus 0 --ve RLEBENCH_MEDIA=0 --max-retries 0 --yes
```

For `rgb-depth-model-training`, the generated task sets
`CUBLAS_WORKSPACE_CONFIG=:4096:8` and `RLEBENCH_ASSETS=/workspace` under
`[solution.env]`. Harbor applies both settings to the Oracle process. The first
supports PyTorch deterministic operations, and the second lets the reference
trainer import its public geometry helpers. The root renderer uses its original
private asset path.

For model agents, provide credentials and match upstream's provider configuration.
For example, the native default Codex API lane uses `OPENAI_API_KEY` and these
installation/API hosts:

```bash
uv run harbor run -p datasets/rle-bench-cpu/task08 \
  -a codex -m "openai/MODEL_ID" \
  --allow-environment-host deb.debian.org \
  --allow-environment-host raw.githubusercontent.com \
  --allow-environment-host github.com \
  --allow-environment-host objects.githubusercontent.com \
  --allow-environment-host nodejs.org \
  --allow-environment-host registry.npmjs.org \
  --allow-environment-host api.openai.com \
  --allow-environment-host auth.openai.com \
  --ak web_search=disabled
```

Replace `MODEL_ID` with an available model. Other providers need the corresponding
credentials and host allowlists from upstream `rlebench/providers.py`. Model-agent
evaluations have not been run as part of this oracle-only validation.

## Usage: Create Task Directories

```bash
cd src/rle-bench
uv sync --locked --group dev

# Fetch the pinned source and generate all five CPU tasks:
uv run rle-bench --output-dir datasets/rle-bench-cpu

# Reuse a clean checkout at the pinned commit without modifying that checkout:
uv run rle-bench --source-dir /path/to/RLE-Bench \
  --output-dir datasets/rle-bench-cpu

# Generate only RGB-only perception and GELLO co-design:
uv run rle-bench --task-ids task06/rgb-only task09 \
  --output-dir datasets/rle-bench-priority-cpu
uv run harbor run -p datasets/rle-bench-priority-cpu -a oracle -n 2 \
  --ve RLEBENCH_MEDIA=0 --yes

# List available tasks or regenerate owned task folders:
uv run rle-bench --list
uv run rle-bench --overwrite --output-dir datasets/rle-bench-cpu
```

Native selectors and exported task IDs are accepted. `--limit N` keeps the first
N tasks in the stable selection order.
`--overwrite` replaces only selected, provenance-marked tasks and preserves
unrelated files at the output root. Dataset manifests include all existing owned
tasks for their split, with Harbor's content digests.

## Comparison with Original Benchmark (Parity)

This release validates **oracle equivalence**, with one complete native run and
one complete adapted run per task. It does not claim LLM-agent parity or estimate
sample SEM from a single run. Organizer coordination reported by the contributor
on 2026-11-16 permits upstream baseline/reference oracles without requiring full
reward. The acceptance criterion is preservation of the original measurement.

All five CPU tasks passed the agreed equivalence criterion. Single observations are
shown below; sample SEM is undefined for `n=1`. The complete comparison is in
[oracle_equivalence.json](oracle_equivalence.json).

| Agent | Model | Metric | Runs per side | Dataset Size | Original observation | Harbor observation |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| oracle@harbor-0.21.0 | None | task06/rgb-only accuracy_reward | 1 | 1 | 0.2974174159335837 | 0.2974174159335837 |
| oracle@harbor-0.21.0 | None | task06/rgb-depth accuracy_reward | 1 | 1 | 0.292222651588023 | 0.292222651588023 |
| oracle@harbor-0.21.0 | None | task07 reward | 1 | 1 | 0.0572 | 0.0572 |
| oracle@harbor-0.21.0 | None | task08 reward | 1 | 1 | 0.7564 | 0.7564 |
| oracle@harbor-0.21.0 | None | task09 reward | 1 | 1 | 0.7154333333333334 | 0.7154333333333334 |

Raw verifier rewards (task06 includes its original real-time deduction):

| Task | Native reward | Native accuracy reward | Adapted oracle |
| --- | ---: | ---: | --- |
| task06/rgb-only | 0.1081 | 0.2974174159335837 | 0.1082; deterministic metrics matched |
| task06/rgb-depth | 0.0964 | 0.292222651588023 | 0.0966; deterministic metrics matched |
| task07 | 0.0572 | — | 0.0572; deterministic metrics matched |
| task08 | 0.7564 | — | 0.7564; full report matched |
| task09 | 0.7154333333333334 | — | 0.7154333333333334; full report matched |

The upstream task09 manifest records the exact full-battery Oracle scores. The
native and adapted runs both match the total and all three variant scores.
Task06 supplies a reference estimator for each CPU modality and pose-error and
tracking regressions. Its final reward includes measured CPU inference time.
Task07 ships a public depth policy. Its `BASELINE_REF_SCORE` is a development
calibration over two direct-policy episodes. The benchmark runs the policy in a
sandbox over eight scored episodes and uses a separate two-run determinism probe.
Task08 ships a reference chassis and shelf controller. Its chassis-only golden
test requires reward at least 0.64. The full Oracle also evaluates shelf targets
for all three arms. The recorded full-run results above use the original verifier
on both sides.

Reproduce the original side in a separate checkout after `make install`. From this
adapter directory, `bash scripts/prepare_native_cpu.sh /path/to/RLE-Bench` prepares
only the five CPU tasks. It runs the native asset builders and original Dockerfiles;
it does not build task06's CUDA variants.

```bash
# From the original RLE-Bench checkout, with its environment activated:
rlebench run task06/rgb-only task06/rgb-depth task07 task08 task09 \
  -a oracle --device cpu --jobs 5 -o /path/to/original-jobs \
  -- --ve RLEBENCH_MEDIA=0 --yes -n 1

# From this adapter directory, after generation:
uv run harbor run -p datasets/rle-bench-cpu -a oracle -n 5 \
  -o /path/to/harbor-jobs --job-name oracle --ve RLEBENCH_MEDIA=0 --yes

uv run rle-bench-compare --original-jobs /path/to/original-jobs \
  --harbor-jobs /path/to/harbor-jobs --output-file oracle_equivalence.json
```

The comparator requires exact equality of complete reports after excluding only
measured wall-clock diagnostics. This includes task07's eight episode outcomes,
task08's controller outcomes, simulation times and action digests, and task09's
per-variant diagnostics. Task08 and task09 reports matched in full. For task06 it
compares accuracy and all other deterministic metrics exactly, then independently
checks the original inference-time deduction and raw reward arithmetic. It never
overrides or normalizes a verifier reward.

### GPU split Oracle results

On one RTX 3080 Ti, each of the four task03 and task06 Oracles below was run twice
with the upstream images and once with the adapter. The training variant used
deterministic CUDA settings on both sides. Task03 reward JSON matched exactly,
including trial counts, interaction steps and zero infrastructure failures. Both environments passed the
upstream agent-isolation check. The hidden-CoM Oracle answers were also checked
against the official scene ground truth.

| Native selector | Metric | Original observation | Harbor observation |
| --- | --- | ---: | ---: |
| `task03/01-tower-max-height` | Reward | 0.26419826409000635 | 0.26419826409000635 |
| `task03/05-hidden-center-of-mass` | Reward | 1.0 (3/3) | 1.0 (3/3) |
| `task06/method-agnostic` | Accuracy reward | 0.292222651588023 | 0.292222651588023 |
| `task06/rgb-depth-model-training` | Accuracy reward | 0.0041042114936177525 | 0.0041042114936177525 |

The `method-agnostic` task reserves a GPU for agent use. Its upstream reference
estimator runs on the CPU verifier. All deterministic metrics matched across both
native runs and the Harbor run. The second native run took 557.43 s for inference
across 700 frames and scored 0.0979 after the native timing deduction. The Harbor
run took 551.63 s and scored 0.0982. Both deductions were checked against the same
upstream formula.

The training task matched all 38 model state tensors and every deterministic
verifier field across both native diagnostic runs and the adapted run. Using
one verifier at a time, the original model replay and the adapted run took
69.25 s and 69.74 s for 700 frames respectively. Both exceeded 10 Hz and scored
0.0041 with zero time deduction.

Run the first three tasks from the pinned upstream checkout after building its images.

```bash
RLEBENCH_MEDIA=0 rlebench run task03/01-tower-max-height \
  task03/05-hidden-center-of-mass task06/method-agnostic \
  -a oracle --device cuda:0 -o /path/to/original-gpu-jobs \
  -- --ve RLEBENCH_MEDIA=0 --max-retries 0
```

To reproduce the deterministic training reference with the native images, copy
the built upstream task to a scratch directory and apply the same Oracle launcher
and process environment used in the adapter.

```bash
training_task=rgb-depth-model-training
scratch=$(mktemp -d)
cp -a "/path/to/RLE-Bench/tasks/task06/$training_task" "$scratch/"
cp "datasets/rle-bench-gpu/task06-$training_task/solution/"{solve.sh,train_deterministic.py} \
  "$scratch/$training_task/solution/"
uv run harbor run -p "$scratch/$training_task" -a oracle -n 1 --override-gpus 0 \
  --ae RLEBENCH_ASSETS=/workspace --ae CUBLAS_WORKSPACE_CONFIG=:4096:8 \
  --ve RLEBENCH_MEDIA=0 --max-retries 0 --yes -o /path/to/native-training-jobs
```

Use `rle-bench-compare --split gpu` with the original and Harbor job roots.
`--task-ids` restricts comparison to specified tasks. Task03 comparison requires
the upstream ledger and handoff checks to pass before comparing scores.
Task02 comparison requires all six step verifiers to succeed, compares their
cumulative reward and diagnosis files exactly, and checks that the Harbor
aggregate equals the final step's reward.
For the training task, select one native run with the matching deterministic CUDA
settings as the original side.

### Task02 kitchen skill transfer

Use the RoboCasa asset path above. Run the upstream and adapted protocol Oracles,
then compare the six step results for each group.

```bash
(cd "$upstream" && make task02)
uv run rle-bench --split gpu --output-dir datasets/rle-bench-task02 --task-ids \
  $(uv run rle-bench --split gpu --list | awk '$1 ~ /^task02-/ {print $1}')
RLEBENCH_GPU=0 RLEBENCH_MEDIA=0 uv run harbor run -p "$upstream/tasks/task02" -a oracle \
  -n 1 --override-gpus 0 --max-retries 0 --yes -o jobs/task02-native
RLEBENCH_GPU=0 RLEBENCH_MEDIA=0 uv run harbor run -p datasets/rle-bench-task02 -a oracle \
  -n 1 --override-gpus 0 --max-retries 0 --yes -o jobs/task02-harbor
uv run rle-bench-compare --split gpu --original-jobs jobs/task02-native \
  --harbor-jobs jobs/task02-harbor \
  --task-ids $(uv run rle-bench --split gpu --list | awk '$1 ~ /^task02-/ {print $1}')
```

All 15 Task02 groups matched on an RTX 3080 Ti. The native and adapted runs
produced identical reward and diagnosis files for all 90 paired steps, with zero
infrastructure failures. The Oracle resets and takes one neutral action during
development, then ends each of the five evaluation trials without acting. Its zero
reward is expected. Model-agent scores are available on the
[upstream leaderboard](https://rle-bench.github.io/).

An additional scripted comparison covered washing dishes, setting the table and
storing leftovers. All five rounds in each group received the original frozen
manual and nested skill file. Edits from earlier rounds were discarded. Each
round returned three RGB and three depth views and recorded a neutral action on
both sides. This verifies the handoff and simulator interface for those groups.

Both sides use the pinned upstream verifier.
[Draft PR #15](https://github.com/harbor-index/adapters/pull/15) covers five CPU
tasks and 19 GPU tasks. Registry and full experiment artifact publication
remain pending.

## Notes & Caveats

- The upstream training Oracle uses a fixed seed, and its default cuDNN
  convolution backward pass can still yield different gradients. The training
  equivalence comparison uses deterministic CUDA settings on both sides.
- These oracles are reference implementations and baselines, not optimal answers.
  Low valid rewards are preserved. In the native run, task08's base passes the
  dynamics battery but its controller misses some shelf targets; task06's moving,
  occluded scenes and inference-time deduction limit the reference score.
- Task06 scores contain real wall-clock inference measurements. Its Gaussian
  accuracy kernels award full credit only at zero error. Native runs can produce
  different total rewards even with identical deterministic predictions. The
  contributor explicitly accepted exact deterministic-score matching with timing
  accounting checked separately.
- Harbor 0.21.0 and the source commit are pinned. Later Harbor/RLE-Bench upgrades
  can change networking, sandbox behavior and GPU handling and require new checks.
- Dockerfiles retain upstream base tags and numerical package pins. Record image
  digests for long-term reproduction because base tags and transitive dependencies
  can change across builds.
- No benchmark instructions, scoring formulas, hidden seeds, budgets or oracle
  algorithms have been patched. The local prebuilt image references are removed
  only to enable builds from the exported directories.
- Oracle equivalence is separate from model-agent parity. Paid model experiments
  and registry/Hugging Face publication are outside the current validation.
- Upstream MIT and third-party notices are copied into the generated dataset;
  vendored robot assets retain their original license files.

## Installation / Prerequisites

- Linux, Git, Python 3.12, uv and a running Docker daemon with Compose support.
- Internet access for the initial source, dependency and image downloads. The GPU
  split requires an NVIDIA GPU, working host drivers and NVIDIA Container Toolkit.
- Per task, native CPU environments request four CPUs and 4–6 GiB RAM. Running
  five tasks concurrently needs resources for their combined containers.
- Install with `uv sync --locked --group dev`; the lock includes Harbor 0.21.0
  and the original numerical library versions used to generate assets.

## Troubleshooting

- **Wrong source revision / dirty checkout:** use a clean checkout at the pinned
  commit, or omit `--source-dir` to fetch an owned snapshot automatically.
- **Non-empty unowned output:** choose a new output directory. `--overwrite`
  intentionally does not erase unrelated research directories.
- **Missing Docker COPY input:** regenerate the complete task; copied verifier
  contexts include their original repository-relative input paths.
- **Model API cannot be reached:** supply provider credentials and the matching
  upstream host allowlist. Keep verifier isolation and scoring unchanged.
- **Small task06 total-score differences:** inspect accuracy, inference wall time
  and deduction separately with `rle-bench-compare`.
- **Long oracle run:** task07 performs eight fully rendered hidden episodes;
  task08 evaluates three arms and their shelf targets. Keep original budgets for
  fidelity and inspect the trial logs before interpreting a timeout as a score.

Run adapter tests with `uv run pytest -q`. Validate the adapter structure from the
adapters repository root with `python scripts/validate_adapter.py rle-bench`.

## Citation

```bibtex
@article{ma2026rle,
  title={RLE-Bench: A Qualifying Exam for Coding Agents as Robot Learning Engineers},
  author={Ma, Haitong and Gao, Chenxiao and Qiang, Rushi and Li, Na and Dai, Bo},
  journal={arXiv preprint arXiv:2609.34210},
  year={2026}
}
```

## Authors & Contributions

RLE-Bench and its reference implementations are the work of Haitong Ma and the
RLE-Bench contributors. The adapter preserves upstream task author attribution.
Adapter packaging and validation: Dongzhikang (darrendong@qq.com).
