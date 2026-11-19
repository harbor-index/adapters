## DataSpace → Harbor Adapter

## Overview

[DataSpace](https://github.com/HKUSTDial/DataSpace) evaluates data agents on
analytical questions over self-contained, task-local workspaces. Each workspace
mixes CSV, JSON, SQLite, Markdown, PDF, and video artifacts; questions and data
are in Chinese and English. The agent must find and combine the relevant
evidence and return the complete answer as a table. DataSpace is the official
benchmark of the [KDD Cup 2026 Data Agent Track](https://dataagent.top) and is
released under the MIT license.

The release has 410 tasks. All inputs are public, but gold results and frozen
evaluation configs are published for only **60 public-reference tasks**
(`metadata/public_reference_tasks.json`); the other 350 are withheld for
official evaluation. This adapter therefore generates **60 Harbor tasks**, one
per public-reference task, keeping the upstream task IDs (`task_10`, `task_18`, ...).
Of the 60 questions, 34 are in English and 26 in Chinese; 28 workspaces include
a video.

Adaptation changes only packaging and paths. The agent prompt is the upstream
native-harness prompt verbatim, the environment reproduces the upstream Data
Workbench Runtime, and scoring uses the unmodified upstream evaluator.

## What is DataSpace?

DataSpace ([paper](https://arxiv.org/abs/2608.03451),
[leaderboard](https://dataspace-bench.github.io/)) targets verifiable analytics
over heterogeneous data. Tasks cover evidence discovery, structured extraction,
joins across representations, filtering, aggregation, ranking, temporal
reasoning, and document or video understanding. The primary metric is **Task
Accuracy**: a task is correct only when the predicted table equals the gold
table under that task's frozen config. Header text and column order are not
scored; values are compared with per-column types (text, number, date,
datetime, Boolean), numeric precision and unit rules, and either ordered or
unordered (multiset) row semantics. The paper reports a best accuracy of
66.34% and a spread of up to 15.36 points across harnesses with a fixed
backbone.

## Adapter Features

- Pinned inputs: data from Hugging Face dataset `HKUSTDial/DataSpace` at
  revision `96be19257d7aa251dbce4771d46a26fc36f6dc52`, archive SHA-256
  `68f221fafcf0b50396eca55f8298af1ad73d586a6cac6995916afca535a560af`;
  code from `HKUSTDial/DataSpace@6491caa4c70cc06cacb6103ba73cefb00746abfe`.
- Automatic download, checksum verification, and extraction (or reuse of an
  existing extracted release via `--benchmark-root`).
- Instruction identical to upstream `relation_task_prompt` (prompt contract
  `relation-v2`) with the upstream native-harness paths: workspace
  `/mnt/workspace`, output `/mnt/output/prediction.csv`.
- Environment reproduces the upstream Data Workbench Runtime 1.0: same pinned
  base image digest, apt packages, pinned Python packages, `/opt/dataspace/TOOLS.md`,
  and single-threaded BLAS settings. Each task image contains only that task's
  workspace.
- Verifier runs the upstream `evaluation/evaluate.py` byte-for-byte with a
  config root containing only the current task; reward is 1 for a full table
  match and 0 otherwise.
- Gold results and configs live only in `tests/` and `solution/`, which are not
  part of the image build context.

## Generated Task Structure

```
dataspace/
├── task_10/
│   ├── task.toml                     # name = "hkustdial/dataspace__task_10"
│   ├── instruction.md                # upstream prompt with question
│   ├── environment/
│   │   ├── Dockerfile                # Data Workbench Runtime 1.0
│   │   ├── TOOLS.md                  # upstream tool manifest
│   │   ├── capabilities.json
│   │   └── workspace/                # upstream input/task_10/context/
│   ├── solution/
│   │   ├── solve.sh                  # copies gold.csv to the prediction path
│   │   └── gold.csv
│   └── tests/
│       ├── test.sh                   # runs the evaluator, writes reward.txt
│       ├── evaluate.py               # upstream evaluator, unmodified
│       ├── configs/task_10.json      # frozen upstream config
│       └── gold/task_10/gold.csv
└── ...
```

Adapter code layout:

```
src/dataspace/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_dataspace.yaml
└── src/dataspace/
    ├── __init__.py
    ├── adapter.py
    ├── main.py
    └── task-template/
        ├── task.toml
        ├── instruction.md
        ├── environment/{Dockerfile,TOOLS.md,capabilities.json}
        ├── solution/solve.sh
        └── tests/{test.sh,evaluate.py}
```

## Run Evaluation / Harness

### Running with Datasets Registry

After the dataset is published:

```bash
harbor run -d hkustdial/dataspace
harbor run -d hkustdial/dataspace -a <agent_name> -m "<model_name>"
```

Registry commands work only after the `harbor-datasets` PR is merged. Use the
local methods below until then.

### Using Job Configurations

`run_dataspace.yaml` runs the oracle by default; other agents are listed as
commented alternatives. Run from the directory that contains
`datasets/dataspace`:

```bash
harbor run -c src/dataspace/run_dataspace.yaml
harbor run -c src/dataspace/run_dataspace.yaml -a <agent_name> -m "<model_name>"

# Or point at the generated dataset directly
harbor run -p datasets/dataspace -a <agent_name> -m "<model_name>"
```

### Running Individual Trial

```bash
harbor trial start -p datasets/dataspace/task_10
harbor trial start -p datasets/dataspace/task_10 -a <agent_name> -m "<model_name>"
```

## Usage: Create Task Directories

```bash
cd src/dataspace
uv run dataspace --output-dir ../../datasets/dataspace
```

Available flags:

- `--output-dir`: directory to write generated tasks (default `datasets/dataspace`).
- `--limit`: generate only the first N tasks (by numeric task ID).
- `--overwrite`: replace existing task directories.
- `--task-ids`: generate only these tasks (`task_10` or `10`).
- `--benchmark-root`: use an already extracted `DataSpace-Benchmark/` directory.
- `--cache-dir`: download and extraction cache (default
  `~/.cache/harbor-dataspace`, or `$DATASPACE_CACHE_DIR`).

The first run downloads a 2.9 GB archive (15 GB extracted, of which the 60
public-reference workspaces are 2.2 GB).

## Comparison with Original Benchmark (Parity)

Parity has not been run yet. The planned setup is Scenario 1 (the original
benchmark already supports Harbor-compatible agents): Claude Code and Codex on
all 60 public-reference tasks, at least three runs per side, with the same
model and the upstream 1,800-second wall-clock limit. The original side uses
the upstream `dataspace-baselines batch` runner; the Harbor side uses
`harbor run`. The plan is pending approval by the Harbor team.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|------------------|--------------|------------------------------|----------------------------|
| claude-code@TBD | TBD | task_accuracy | TBD | 60 (100%) | TBD | TBD |
| codex@TBD | TBD | task_accuracy | TBD | 60 (100%) | TBD | TBD |

Oracle verification: 60/60 tasks reach reward 1.0 with the oracle agent, and
the nop agent scores 0.0 (`missing_prediction`), confirming the verifier does
not pass without a prediction.

Reproduction on the original side (from a DataSpace checkout at the pinned commit):

```bash
cd baseline
python3 -m venv .venv && .venv/bin/pip install -e .
./scripts/build_harness_images.sh
./scripts/install_harness_runtimes.sh
.venv/bin/dataspace-baselines batch --harness claude-code \
  --input-root /path/to/public-reference-input \
  --config configs/harness.yaml --run-root runs/claude-code/run_1
python3 ../evaluation/evaluate.py --prediction-root runs/claude-code/run_1/predictions \
  --gold-root /path/to/DataSpace-Benchmark/output \
  --config-root /path/to/DataSpace-Benchmark/evaluation/configs
```

Harbor side:

```bash
harbor run -p datasets/dataspace -a claude-code -m "<model>"
```

## Notes & Caveats

- Only the 60 public-reference tasks are adapted; the 350 withheld tasks have
  no public gold or config.
- Upstream runs native CLIs inside a Bubblewrap jail with a read-only
  workspace bind and no network for generated commands. In Harbor the agent
  runs inside the task container; the workspace is made non-writable with
  `chmod`, but an agent running as root can bypass this, and the container has
  network access because the agent must reach its model API. Neither affects
  scoring: the verifier reads only `/mnt/output/prediction.csv`, and the gold
  files are not in the image.
- Upstream accepts a prediction only when the harness exits cleanly; a file
  left after a timeout counts as missing. Harbor still runs the verifier after
  an agent timeout, so a prediction written before the timeout is scored.
  This will be checked during parity.
- The verifier rejects a symlinked `prediction.csv`, because `tests/` (with
  the gold file) is uploaded into the same container at verification time.
- Several public tasks share identical gold tables (for example, three tasks
  have 12,962-row answers); this is upstream data, not an adapter artifact.

## Installation / Prerequisites

- Docker installed and running.
- Harbor CLI (`uv tool install harbor`); verified with Harbor 0.26.0.
- Python 3.12+ and `uv`:
  ```bash
  cd src/dataspace
  uv sync
  ```
- About 20 GB of free disk for the archive, extraction, and generated tasks,
  plus Docker images (the shared base layers are built once; each task adds its
  own workspace layer).
- Agent API keys exported as environment variables for non-oracle runs.

## Troubleshooting

- `... is not an extracted DataSpace release`: `--benchmark-root` must point at
  the `DataSpace-Benchmark/` directory that contains `input/`, `output/`,
  `evaluation/`, and `metadata/`.
- `SHA-256 mismatch`: delete the cached archive under `--cache-dir` and rerun.
- The first image build installs ffmpeg and Tesseract and takes a few minutes;
  later tasks reuse the cached layers.

## Citation

```bibtex
@article{li2026dataspace,
  title        = {DataSpace: Benchmarking Data Agents for Verifiable Analytics
                  over Heterogeneous Workspaces},
  author       = {Boyan Li and Zhuowen Liang and Yupeng Xie and Xiaotian Lin and
                  Tianqi Luo and Xinyu Liu and Yizhang Zhu and Zhangyang Peng and
                  Yuan Li and Zhengxuan Zhang and Jiayi Zhang and Nan Tang and
                  Guoliang Li and Yuyu Luo},
  journal      = {arXiv preprint arXiv:2608.03451},
  year         = {2026},
  url          = {https://arxiv.org/abs/2608.03451}
}
```

## Authors & Contributions

This adapter is developed and maintained by
[Houdong Liang](mailto:houdongliang84@gmail.com).

**Issues and Contributions:**

- Submit issues and pull requests to [harbor-framework/adapters](https://github.com/harbor-framework/adapters).
- Follow the repository contribution and coding-style guidelines.

## Acknowledgement

No Harbor-provided API credentials have been used yet. If parity runs use them,
the 2077AI acknowledgement required by the adapter guide will be added here.
