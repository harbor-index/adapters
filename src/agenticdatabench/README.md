# AgenticDataBench → Harbor Adapter

## Overview

AgenticDataBench tests data agents on real-world data-science work. Each task puts the agent in a folder of real data files (CSV, JSON, shapefiles and others) with a business question that takes several steps of loading, cleaning, joining, statistics, modelling or plotting. The agent saves output files, and the grader compares them with gold files.

- **Source:** 246 public tasks (`testbed/tasks/dev.jsonl`) in 15 domains: agriculture, ecommerce, energy, entertainment, financial, healthcare, loan_model, loan_risk, marketing, real_estate, social_network, sports, strategy, tourism and transportation. Upstream also holds 98 private tasks, which are not available.
- **This adapter:** 229 tasks. 17 are excluded: 16 whose grader only reads metrics the agent reports about itself, and one whose metric the grader doesn't implement (see [Notes & Caveats](#notes--caveats)).
- **Outputs graded:** 190 tasks write CSV files, 40 PNG charts, 39 JSON, 9 text files and 3 SQLite databases (a task can write several kinds). There are 316 checks across the 229 tasks, 1 to 5 per task.
- **Sources:** tasks, gold files and grader from GitHub `AgenticDataBench/AgenticDataBench` at `e81dda8`; input data from Hugging Face `shawnzzzh/AgenticDataBench` at `3b0ac3f`. Both are Apache-2.0.
- **Prompt changes for agents:** none. The instruction is upstream's own prompt for its Claude Code and Codex runners, with the work folder at `/workspace`, as upstream uses.

## What is AgenticDataBench?

AgenticDataBench ([paper](https://arxiv.org/abs/2607.01647), VLDB 2026; [code](https://github.com/AgenticDataBench/AgenticDataBench)) is a benchmark from Tsinghua University and Ant Group for LLM data agents. Tasks are written around reusable data-science skills (each task lists the skills it exercises) and mix curated real-world analysis requests with generated ones. Upstream runs four agents (its own DA-Agent, smolagents, Claude Code and Codex) in a Docker container whose work dir holds the task's domain data, then grades the saved files with deterministic checks: `compare_csv` (numeric tolerance, row order optional), `compare_json`, `compare_image` (plot data and figure properties), `compare_text`, `compare_sqlite` and `compare_model` (prediction quality against gold labels). A task's score is the mean of its checks, so partial credit is possible. No LLM judge is involved.

## Adapter Features

- **Pinned sources.** Tasks, gold files and grader at GitHub commit `e81dda8`; data at Hugging Face revision `3b0ac3f`.
- **Same environment as upstream's runners.** `python:3.10-bullseye` (pinned by digest), the same system packages and the same Python packages. Upstream installs those unpinned, so they are pinned here to the versions current at upstream's last commit (2026-10-13). The work dir is `/workspace` and holds the contents of the task's domain folder, as upstream copies it.
- **Data checked on download.** The image build fetches the domain folder file by file from the pinned revision and checks each file's size and hash (sha256 for LFS files, the git blob hash otherwise). The fetch step is identical for every task of a domain, so Docker's layer cache keeps one copy per domain.
- **Chart tasks get upstream's plot helper.** As in upstream, tasks graded by `compare_image` get `image.py` in the work dir, and the instruction asks the agent to call `Plotprocess.plot_process` before saving the figure. This writes the plot data the grader compares (see [Notes & Caveats](#notes--caveats) for why the current upstream task file misses this).
- **Upstream's grader.** `tests/evaluate.py` runs each task's own `eval_func` strings with upstream's metric functions (vendored in `tests/metrics/`). It binds file paths and averages scores exactly as upstream's `Evaluator` does, within the same 120 s evaluation timeout. Three grader bugs are fixed; see below.
- **Grading in a separate container.** As upstream grades outside the agent's container, the verifier here runs in a fresh container from the same image (`[verifier] environment_mode = "separate"`). It receives only the files listed in `task.toml`'s `artifacts`: the outputs the task asks for, plus the plot data next to each graded chart. The gold files never enter the agent's container, and nothing the agent changes there (the grader's packages, Python itself) reaches the grader. Check: a solve.sh that does no work but replaces the grader's Python with a script that writes reward 1.0 scored 1.0 with the grader in the agent's container, and 0.0 with it in its own container (financial_52, Harbor 0.27.0).
- **Oracle.** `solution/solve.sh` copies upstream's gold files into `/workspace` under the output names the task asks for, plus the plot data for chart tasks.

## Generated Task Structure

```
agenticdatabench/
├── agriculture_14/
│   ├── task.toml                 # Separate verifier and its artifacts; 2 CPUs; memory and storage sized from the domain data; 3600 s agent / 600 s verifier timeouts
│   ├── instruction.md            # Upstream's Claude Code / Codex prompt, with the plotting block for chart tasks
│   ├── environment/
│   │   ├── Dockerfile            # python:3.10-bullseye with upstream's packages and the domain data in /workspace
│   │   ├── fetch_data.py         # Downloads and checks the domain's files from the pinned HF revision
│   │   ├── data_manifest.tsv     # The domain's files: path, size, hash
│   │   ├── image.py              # Upstream's plot helper (chart tasks only)
│   │   ├── requirements-agent.txt
│   │   └── requirements-verifier.txt
│   ├── solution/
│   │   ├── solve.sh              # Copies the gold files to /workspace under the output names
│   │   └── output/               # Those files (and output.json / output.npy plot data for charts)
│   └── tests/
│       ├── test.sh               # Runs evaluate.py in the verifier venv (in its own container)
│       ├── evaluate.py           # Upstream's scoring loop for one task
│       ├── task_config.json      # The task's eval_func, output and gold file names
│       ├── gold/                 # Upstream's gold files
│       ├── metrics/              # Upstream's metric functions (SOURCE.md lists the changes)
│       └── da_utils.py           # Upstream's timeout helper
```

Adapter code layout:

```
src/agenticdatabench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_agenticdatabench.yaml
├── src/agenticdatabench/
│   ├── adapter.py                # Task generation
│   ├── main.py                   # CLI
│   ├── excluded_tasks.json       # The 17 excluded tasks and why
│   └── task-template/
└── tests/
    └── test_adapter.py           # Generation tests (no network)
```

## Run Evaluation / Harness

Until the dataset is published to the Harbor registry, generate the tasks locally (see [Usage](#usage-create-task-directories)) and run them from the repository root.

```bash
# Oracle on the full dataset (default job config)
harbor run -c src/agenticdatabench/run_agenticdatabench.yaml

# Any Harbor agent on a locally generated dataset
harbor run -p datasets/agenticdatabench -a claude-code -m claude-haiku-4-5

# One task
harbor run -p datasets/agenticdatabench -i agriculture_14 -a claude-code -m claude-haiku-4-5

# Resume a job
harbor job resume -p /path/to/jobs/directory
```

Results go to `jobs/` by default.

## Usage: Create Task Directories

```bash
cd src/agenticdatabench
uv run agenticdatabench --output-dir ../../datasets/agenticdatabench
```

Available flags:
- `--output-dir`: directory to write generated tasks
- `--task-ids`: only these tasks (upstream ids, e.g. `agriculture_14`)
- `--limit`: only the first N tasks
- `--overwrite`: overwrite existing tasks
- `--source-dir`: a local checkout of the AgenticDataBench GitHub repo at the pinned commit, instead of fetching it
- `--include-excluded`: also generate the 17 excluded tasks (for checking the exclusions)
- `--split parity`: only the 50-task parity subset (see Parity)

Generation needs network access to GitHub (the repo at the pinned commit) and to the Hugging Face API (the file list of the pinned revision). The generated tasks take about 480 MB, mostly gold files. The images share about 4.6 GB of base layers (Python, system and data-science packages, the verifier venv) and add the task's domain data: under 0.6 GB for 10 of the 15 domains, 1.0 GB for entertainment, 1.3 GB for transportation, 3.1 GB for ecommerce, and up to 1.8 GB for a single loan_risk task. All 229 tasks together use 19.5 GB of input data.

## Comparison with Original Benchmark (Parity)

TODO: parity has not been run yet. The plan below is proposed on the adapter PR for the Harbor team's approval.
- **Scenario 1.** Upstream already runs Claude Code (`testbed/run.py --agent claude-code`).
- **Original side:** upstream's own harness and grader from the [fork's `harbor-adapter` branch](https://github.com/Pranavv-dev/AgenticDataBench/tree/harbor-adapter): upstream at `e81dda8` with the three grader fixes and three harness fixes described below. Its runner starts Claude Code exactly as Harbor 0.27.0's `claude-code` agent does: in this adapter's task image (same base image digest, system and Python packages) with Claude Code 2.1.311 installed by Harbor's own install commands, as root, with the prompt piped on stdin, logs and `CLAUDE_CONFIG_DIR` under `/logs/agent`, and the same environment variables. Credentials come from the environment, as on Harbor. Upstream ran `claude -p` as a non-root user, installed everything unpinned and hard-codes DashScope's endpoint. `testbed/harbor_parity/test_identical_launch.py` on the fork checks the launch against Harbor's own `ClaudeCode` class. On one task run against a stand-in model API (no model calls), both sides sent identical requests: every header and body field, including the system prompt and tools, apart from per-run ids.
- **Harbor side:** `claude-code@2.1.311` with `claude-haiku-4-5` on the parity subset.
- **The same on both sides:** upstream's prompt with the plotting block for chart tasks (the fork's prompt equals `instruction.md` byte for byte for all 229 tasks), the Claude Code launch and requests above, the 3600 s agent timeout, and upstream's grader with the same pinned packages.
- **Kept from upstream's harness on the original side:** no CPU or memory limits, and the work dir is a host folder mounted into the container, so file timestamps and Python's unsorted directory listing order differ from Harbor's. Shell `ls` output is the same.
- **Subset:** 50 of the 229 tasks (`--split parity`), spread over the domains in proportion to their size with at least one each, drawn with seed 0 before any run. It covers all 14 domains and every check type: 37 of the 177 CSV checks, 7 of 40 chart, 8 of 39 JSON, 4 of 14 model, 3 of 9 text and 1 of 3 SQLite.
- **Runs and metric:** 3 runs per side. A run's score is the mean task score (0–1) over the 50 tasks; reported as mean ± sample SEM. The sides match if their ranges overlap.

```bash
# Harbor side
uv run agenticdatabench --split parity --output-dir ../../datasets/agenticdatabench-parity
harbor run -p ../../datasets/agenticdatabench-parity -a claude-code -m claude-haiku-4-5 --ak version=2.1.311 -n 4

# Original side (fork, branch harbor-adapter), from testbed/, with the same
# ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL in the environment as the Harbor side
docker build -t claude-code da_agent/images/claude-code
python run.py --agent claude-code --model claude-haiku-4-5 --suffix parity --example_names <the 50 task ids>
python evaluate.py --output_dir output/<experiment id>
```

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|------------------|--------------|--------------------------------|----------------------------|
| claude-code@2.1.311 | claude-haiku-4-5 | Mean score | 3 | 50 of 229 | TODO | TODO |

## Notes & Caveats

**Excluded tasks (17).** Each task and its reason is in `excluded_tasks.json`.
- **16 self-reported-metric tasks** (loan_model_2, 3, 5, 8, 9, 13, 14; marketing_2, 3, 4, 5, 6, 9, 12, 13, 14). Their only check is `compare_json_normalized` with range thresholds such as `"test_auc": [0.5, 1.0]`. It reads only the agent's `output.json`, with no gold labels or predictions, and maps each reported number onto [0, 1]. A fabricated `output.json` that trains nothing scores 1.0 on all 16; in Harbor, a solve.sh that only writes `{"best_test_auc": 1.0}` got reward 1.0 on loan_model_13. Honest answers can't reach 1.0 either: upstream's own answers score 0.29–0.63 on the loan_model tasks, and the marketing tasks' gold files are empty. These tasks can't be reliably verified, so they are excluded, as Harbor's adapter guide says to do.
- **real_estate_17.** Its check is `compare_model(..., metric='pearson')`, and the grader has no `pearson` metric, so every answer scores 0, the gold included. Every published run scored 0 on it as well. Adding the metric would not make the task gradeable. The check lines rows up by position, scores only the rows whose state and year happen to match, and doesn't penalise the rest. Two matched rows already give a correlation of 1. In a test with the metric added, 12% of 500 made-up answers that read no data scored 1.0. So it is excluded.

**Grader fixes (3).** These are in `tests/metrics/myjson.py` and `tests/metrics/image.py`, listed in `tests/metrics/SOURCE.md`, and in commit `9ced93d` on the fork's `harbor-adapter` branch, which is used on both sides of parity. TODO: link the upstream issue/PR. Each fix reaches only the two tasks named. Graded against their own gold files, those six tasks now score 1.0, and no other task's score changed. The DA-Code adapter, whose grader this one descends from, fixed the same kind of empty-list bug in its copy (its `ISSUES.md`, item 6).
1. `compare_json` scored an empty gold list or dict as 0 even against an identical empty output (loan_model_18, loan_model_20: their gold files scored 0.8 and 0.9 against themselves). It now scores 1.
2. `compare_json` with no thresholds compared no fields and always returned 0 (real_estate_29, ecommerce_35). It now compares every top-level field of the gold JSON with the default rule (1% relative tolerance for numbers, case-insensitive equality for strings).
3. `compare_image`: upstream's plot helper saves no data for subplots it can't extract numbers from (KDE plots), but still lists them in the figure's `.json`, so those subplots never matched, even gold against gold (ecommerce_21, healthcare_26: 0.833). When the result and gold `.npy` files hold the same subplots, a subplot missing from both is now judged on its figure properties (type, title, axis labels) alone.

**Upstream issues found (fixed on the fork for the original side of parity; the adapter does not depend on them):**
- Debian 11 packages moved to `archive.debian.org`, so `apt-get` in upstream's agent images fails with 404s and no image builds. The adapter's Dockerfile and the fork point apt at the archive.
- The current `tasks/dev.jsonl` no longer has the `post_process_func` field, so upstream's runners give chart tasks neither `image.py` nor the plotting instructions. The agent then can't write the plot data `compare_image` grades. The earlier `dev.jsonl` (`682960d`) had the field, and upstream's published Claude Code trajectories call `Plotprocess`. The adapter derives the chart outputs from each task's `compare_image` checks, which gives the same list as the earlier file for all 40 chart tasks.
- `evaluate.py` reads a `steps` field that the Claude Code and Codex runners never write, so grading their results raises `KeyError`.

**Differences from upstream's harness.**
- Upstream's container runs the agent as a non-root user named after the host user, to keep bind-mounted files owned by them. Here the agent runs as root in its own image; nothing is bind-mounted.
- Upstream sets no CPU or memory limits. Here each task has 2 CPUs and 4, 8 or 16 GB of memory (by the size of the largest input file), and storage sized to the domain data.
- Upstream's Claude Code and Codex runners stop the CLI after 3600 s and grade the files it saved; Harbor's 3600 s agent timeout does the same.

**Agent network access.** The agent has network access (it needs the model API), as in upstream, whose runners start the agent container on Docker's default network. Upstream's gold files are public in its GitHub repository (`testbed/gold/<task id>/`), so an agent that found them could copy a task's answer and score 1.0. The instruction doesn't name the benchmark, and the gold files never enter the agent's container. Harbor can limit the agent to the model API (`network_mode = "allowlist"`); this adapter keeps upstream's open network so that both sides of parity run the same way.

**Comparability.** The paper's numbers cover all 344 tasks, private ones included, so they can't be compared directly with scores on these 229 public tasks.

## Installation / Prerequisites

- Docker, installed and running, with room for the images: about 4.6 GB of shared base layers plus each domain's data (19.5 GB for all 229 tasks).
- Harbor 0.22.0 or later. The tasks grade in a separate container (`[verifier] environment_mode = "separate"` with the top-level `artifacts` list in `task.toml`), which Harbor supports in 0.22.0 and later (all 229 task files checked to parse in 0.22.0 and 0.27.0). Tested with Harbor 0.27.0.
- Python 3.12+ and `uv`. Install the adapter's dependencies with:
  ```bash
  cd src/agenticdatabench
  uv sync
  ```
- Network access to GitHub and Hugging Face (generation and image builds).
- An API key for the agent, e.g. `ANTHROPIC_API_KEY` for Claude Code.

## Troubleshooting

- **`apt-get` fails with 404s while building.** The Dockerfile points apt at `archive.debian.org`; an older copy of a task, or upstream's own images, still use `deb.debian.org`.
- **A data download fails its hash check.** `fetch_data.py` retries 5 times; a persistent mismatch means the dataset revision in `data_manifest.tsv` changed. Regenerate the tasks.
- **A big task runs out of memory.** The largest loan_risk inputs are 1.5–1.8 GB CSVs, and those tasks ask for 16 GB, which Docker Desktop's default VM may not have.

## Citation

```bibtex
@article{sun2026agenticdatabench,
      title={AgenticDataBench: A Comprehensive Benchmark for Data Agents},
      author={Zhaoyan Sun and Shan Zhong and Daizhou Wen and Jiaxing Han and Guoliang Li and Ying Yan and Peng Zhang and Yu Su and Xiang Qi and Baolin Sun and Chengyuan Yang and Tao Fang and Huaiyu Ruan},
      year={2026},
      journal={VLDB}
}
```

## Authors & Contributions

- **Adapter:** [@Pranavv-dev](https://github.com/Pranavv-dev) (Pranav Vashistha).
- **Benchmark:** the AgenticDataBench authors (see [Citation](#citation)).
- **AI use:** Claude Code wrote most of the adapter code and tests, ran the oracle checks and the adversarial check of the excluded tasks; the author reviewed the results.
- Issues and improvements are welcome via the adapters repository.

## Acknowledgement

API inference compute for running parity tests is generously supported by [2077AI](https://www.2077ai.com/) (https://www.2077ai.com/).
