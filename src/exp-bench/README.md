## EXP-Bench → Harbor Adapter

## Overview

EXP-Bench evaluates whether AI agents can carry out a complete AI research experiment:
given a research question from a real ICLR 2024 or NeurIPS 2024 paper and the paper's
code repository with key files masked, the agent must design the experiment, implement
and run it, and state a conclusion grounded in its own results.

- **Source:** 461 tasks from 51 papers (24 ICLR 2024, 27 NeurIPS 2024),
  [arXiv:2505.24785](https://arxiv.org/abs/2505.24785). Dataset on
  [Hugging Face](https://huggingface.co/datasets/Just-Curieous/EXP-Bench) (CC-BY-4.0);
  evaluation harness in [Just-Curieous/Curie](https://github.com/Just-Curieous/Curie/tree/main/benchmark/exp_bench) (Apache-2.0).
- **Task types:** type 1 (210 tasks) masks source files the agent must re-implement;
  type 2 (123) masks the scripts that run an experiment; type 3 (128) masks nothing but
  the README and asks for the experiment from scratch.
- **This adapter: 425 tasks.** The 36 excluded tasks are ones the upstream harness
  itself marks `invalid_task`: 33 list a source file that does not exist in the
  paper's repository (it never existed in the repository's history), and 3 list
  `README.md`, which upstream removes before masking. See Notes & Caveats.
- **Main adaptations:** repositories are pinned to the commit that was current when the
  tasks were constructed (upstream clones `HEAD` at run time), and the judge talks to any
  OpenAI-compatible endpoint (upstream uses Azure OpenAI). Prompts, masking, judge prompts
  and score definitions are unchanged.

## What is EXP-Bench?

[EXP-Bench](https://arxiv.org/abs/2505.24785) ("EXP-Bench: Can AI Conduct AI Research
Experiments?") curates research tasks from influential AI papers. Each task gives an
agent a research question, a description of the method, and optional step-by-step
instructions, plus the paper's repository with the implementation masked. Agents must
formulate a design, implement and execute it, and conclude. An LLM judge (o3-mini)
grades the run against the paper-derived ground truth:

- **D (design):** percentage of ground-truth design variables matched.
- **I (implementation):** percentage of ground-truth setup requirements fulfilled by the
  agent's git diff, with the original source files as reference.
- **C (conclusion):** whether the conclusion semantically matches the expected outcome.
- **E (execution):** whether the agent's `reproduce_exp_bench.sh` runs successfully.
- A log monitor first checks that the agent did not read the paper, use git to recover
  masked files, or fabricate results; if it did, every score is 0.

In the paper, the best configuration (OpenHands + o3-mini) reached D 18.4, I 20.3,
C 21.0 and 0.5% fully correct and executable experiments.

## Adapter Features

- Task generation from the full upstream task records (question, method, agent
  instructions, design ground truth, setup requirements, expected outcome, masked
  sources), with stable task ids `<conference>-<paper_id>-<task_index>` and task names
  `just-curieous/exp-bench__<task id>`.
- Environment: the paper repository at a pinned commit in `/workspace`, submodules
  converted to HTTPS and initialised, README and task source files removed from disk and
  from the git index exactly as upstream `mask_repo` does.
- Agent prompt: upstream `eval_gen_prompt.txt`, filled the way upstream fills it for
  OpenHands / Inspect agents, including the output JSON path and
  `/workspace/reproduce_exp_bench.sh`.
- Verifier (`tests/judge.py`): a port of upstream `judge.py`: same git diff commands and
  70 MB truncation, same four judge prompts filled with the same Python objects, same
  retry and "Error parsing response" rules, the same chunking of large diffs (upstream
  token counting and text splitter, stopping at chunk 15), the setup monitor, and the
  1 hour execution check. Judge dependencies are pinned to upstream's environment.
- Rewards (`/logs/verifier/reward.json`): `design`, `implementation`, `conclusion`,
  `execution`, `implementation_and_execution`, `all_correct`,
  `all_correct_and_execution`, `monitor_pass`, and `reward` (= `conclusion`, also in
  `reward.txt`). Judge outputs, the patch and the agent answer are saved under
  `/logs/verifier/`.
- `--split parity` generates the parity subset; `--gpus`, `--cpus`, `--memory-mb`,
  `--storage-mb` and `--agent-timeout-hours` set resources and the time limit.
- No custom agent: any Harbor agent can run the tasks; parity uses OpenHands, which the
  upstream harness supports.

## Generated Task Structure

```
exp-bench/
├── {task_id}/                       # e.g. neurips2024-97649-0
│   ├── task.toml                    # name, authors, metadata, judge env, resources
│   ├── instruction.md               # upstream eval_gen_prompt filled for this task
│   ├── environment/
│   │   ├── Dockerfile               # miniconda base + tools
│   │   └── prepare_workspace.sh     # clone at pinned commit, mask README and sources
│   ├── solution/
│   │   ├── solve.sh                 # oracle: restore sources, write reference answer
│   │   ├── oracle_answer.json       # ground-truth design and expected outcome
│   │   └── oracle_files/            # original masked source files
│   └── tests/
│       ├── test.sh                  # installs pinned judge deps, runs judge.py
│       ├── judge.py                 # upstream judging pipeline
│       ├── prompts/                 # upstream judge prompts (verbatim)
│       └── task_data.json           # task record and ground-truth source files
```

Adapter code:

```
src/exp-bench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_exp-bench.yaml
├── scripts/build_task_bundle.py     # how the task bundle is built from upstream outputs
└── src/exp_bench/
    ├── adapter.py
    ├── main.py
    ├── prompts/eval_gen_prompt.txt  # upstream agent prompt (verbatim)
    ├── data/exp_bench_tasks.json.gz # full task records, pinned commits, source hashes
    ├── data/parity_task_ids.txt     # parity subset
    └── task-template/
```

## Run Evaluation / Harness

### Running with Datasets Registry

After the dataset is published:

```bash
# Oracle (see the note on EXP_BENCH_ORACLE_VALIDATION below)
EXP_BENCH_ORACLE_VALIDATION=1 harbor run -d just-curieous/exp-bench

# An agent and model
harbor run -d just-curieous/exp-bench -a openhands -m "<model>"

# Parity subset
harbor run -d just-curieous/exp-bench@parity -a openhands -m "<model>"
```

The verifier needs `OPENAI_API_KEY` (and `OPENAI_BASE_URL` for a non-OpenAI endpoint)
for the judge. `EXP_BENCH_JUDGE_MODEL` defaults to `o3-mini`, the upstream judge.

### Using Job Configurations

```bash
# From the repository root
harbor run -c src/exp-bench/run_exp-bench.yaml

# Locally prepared dataset
harbor run -p datasets/exp-bench -a openhands -m "<model>"

# Resume a job
harbor job resume -p /path/to/jobs/directory
```

### Running Individual Trial

```bash
EXP_BENCH_ORACLE_VALIDATION=1 harbor trial start -p datasets/exp-bench/neurips2024-98316-5
harbor trial start -p datasets/exp-bench/neurips2024-98316-5 -a openhands -m "<model>"
```

## Usage: Create Task Directories

```bash
cd src/exp-bench
uv sync
uv run exp-bench --output-dir ../../datasets/exp-bench                     # all 425 tasks
uv run exp-bench --split parity --output-dir ../../datasets/exp-bench-parity
uv run exp-bench --task-ids neurips2024-97649-0 iclr2024-18447-11 --overwrite
uv run exp-bench --gpus 1 --agent-timeout-hours 1 --output-dir ../../datasets/exp-bench-gpu
```

The task records ship with the adapter (`src/exp_bench/data/exp_bench_tasks.json.gz`;
override with `--data-file`). Generation downloads each task's original source files from
GitHub at the pinned commit, checks them against the recorded sha256, and caches them in
`~/.cache/harbor-exp-bench-adapter` (override with `--cache-dir`), so it needs network
access on first run.

## Comparison with Original Benchmark (Parity)

Parity has not been run yet. Values are reported as mean ± sample SEM.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|------------------|--------------|------------------------------|----------------------------|
| openhands@TBD | TBD | conclusion (C) | 3 | 33 (7.8%) | TBD | TBD |

Planned setup (to be confirmed with the Harbor team before any paid run):

- **Subset (provisional):** about 30–40 tasks stratified across papers. The current
  candidate list in `src/exp_bench/data/parity_task_ids.txt` holds the 33 valid tasks
  marked `MATCH` / `PARTIAL_MATCH` in the dataset's published verification records
  (`verification/status.csv`), i.e. tasks with a reproduced, runnable environment. That
  list leans towards easier tasks, so the final selection rule will be agreed with the
  Harbor team and documented here.
- **Agent / model:** OpenHands at one pinned version on both sides, with one low-cost
  model; the same judge model on both sides.
- **Runs:** sanity check on 5–10 tasks on both sides, then one full run on both sides,
  then three runs per side. Metrics compared: D, I, C, E (run ranges must overlap; MSE
  of per-task scores also reported).
- **Original side:** the upstream harness (`evaluation/parallel_eval.py`, generate then
  judge) from a fork that pins the same repository commits and OpenHands version.
  Reproduction commands will be listed here with the fork link.
- **Harbor side:**
  ```bash
  uv run exp-bench --split parity --output-dir ../../datasets/exp-bench-parity
  harbor run -p datasets/exp-bench-parity -a openhands -m "<model>"
  ```

## Notes & Caveats

Deviations from the upstream harness and why:

- **Pinned repositories.** Upstream clones each repository's current `HEAD` at run time,
  so results drift as repositories change. The adapter pins every repository to the last
  default-branch commit before that paper's tasks were constructed (recorded in
  `task.toml` metadata) and fetches it with `--depth 1`.
- **Reference scripts for the setup judge** are read from the pinned commit at generation
  time; upstream downloads them from the `main`/`master` branch when judging.
- **36 excluded tasks** (see Overview) would fail upstream's masking step and are skipped
  by upstream as `invalid_task`; the list is computed by `EXPBenchAdapter.invalid_reason`.
- **Environment.** Upstream runs agents in the OpenHands sandbox with the repository
  mounted at `/workspace`; here the repository is baked into a miniconda image (the base
  of upstream's `ExpDockerfile_default`) at `/workspace`.
- **Execution check** runs `reproduce_exp_bench.sh` in the agent's container after the
  agent finishes; upstream starts a fresh container with the workspace mounted, so
  system packages the agent installed outside `/workspace` do not carry over there.
  E can therefore be higher here when the script relies on such packages.
- **Monitor input.** Upstream passes the OpenHands log; here the monitor reads all files
  in `/logs/agent`, truncated (head and tail) to fit the judge context, where upstream
  would fail on an over-long log.
- **Credentials folder.** Upstream copies its Azure credential files into
  `setup_apis_exp/`; here `setup_apis_exp/env.sh` exports `OPENAI_API_KEY` /
  `OPENAI_BASE_URL` from `EXP_BENCH_TASK_OPENAI_API_KEY` /
  `EXP_BENCH_TASK_OPENAI_BASE_URL` (empty unless set). The folder is excluded from the
  judged diff.
- **Judge endpoint.** OpenAI-compatible client instead of Azure; transient API errors are
  retried. The judge model must be the same on both sides of any comparison.
- **Primary reward** is C (conclusion correctness). The paper does not state the cut-offs
  for the conjunctive metrics; `all_correct` here requires D = I = 100 and C correct,
  pending confirmation.
- **Time limit.** Default 0.5 hours per task (upstream `run_parallel_gen_evals.py`
  default); the paper used a 40 minute soft limit. The output file name encodes the
  limit, as upstream does.
- **GPUs.** Upstream agents had 4× A40; tasks default to `gpus = 0` here. Many tasks need
  a GPU to finish their experiment; use `--gpus` for GPU runs.
- **Oracle.** EXP-Bench has no runnable reference trajectory. The oracle restores the
  masked files and writes the ground-truth design and expected outcome. Run it with
  `EXP_BENCH_ORACLE_VALIDATION=1`, which skips the log monitor and execution check (they
  judge a real trajectory). Type 3 tasks have no reference code, so their oracle
  `implementation` score is low by construction; oracle validation uses `reward`.
- **Task data.** The public Hugging Face CSV is a flattened view that omits fields the
  judge needs (method, design ground truth, code URL, masked-source lists). The adapter
  ships the full upstream task records (`*_complete_final.json`, one per paper; task index
  = position in that file, as upstream enumerates it) under the dataset's CC-BY-4.0
  license. `scripts/build_task_bundle.py`
  documents how the bundle is assembled from the upstream construction outputs.

## Installation / Prerequisites

- Docker (or a Harbor cloud environment such as Daytona)
- Harbor: `uv tool install harbor`
- Adapter dependencies:
  ```bash
  cd src/exp-bench
  uv sync
  ```
- Judge credentials in the host environment: `OPENAI_API_KEY`, optional
  `OPENAI_BASE_URL`, optional `EXP_BENCH_JUDGE_MODEL`.
- Optional credentials for experiments that call an LLM:
  `EXP_BENCH_TASK_OPENAI_API_KEY`, `EXP_BENCH_TASK_OPENAI_BASE_URL`.
- Image builds clone each paper's repository; some repositories are large.

## Troubleshooting

- **Build fails with "EXP-Bench masking failed"**: a listed source file is missing at the
  pinned commit; report the task id.
- **Verifier fails installing judge dependencies**: the verifier needs network access to
  PyPI and the judge endpoint (`network_mode = "public"`).
- **Long verifier runs**: the execution check can take up to 1 hour; set
  `EXP_BENCH_EXEC_CHECK=0` to skip it (E is then 0).

## Citation

```bibtex
@article{kon2025expbench,
  title={EXP-Bench: Can AI Conduct AI Research Experiments?},
  author={Kon, Patrick Tser Jern and Liu, Jiachen and Zhu, Xinyi and Ding, Qiuyi and Peng, Jingjia and Xing, Jiarong and Huang, Yibo and Qiu, Yiming and Srinivasa, Jayanth and Lee, Myungjin and Chowdhury, Mosharaf and Zaharia, Matei and Chen, Ang},
  journal={arXiv preprint arXiv:2505.24785},
  year={2025},
  url={https://arxiv.org/abs/2505.24785}
}
```

## Authors & Contributions

This adapter is developed and maintained by Jingjia Peng (EXP-Bench co-author). Yuwei Han
started the initial scaffold in
[harbor-framework/harbor#742](https://github.com/harbor-framework/harbor/pull/742).

**Issues and Contributions:**
- Submit issues and pull requests to the adapters repository.
- Report dataset errors on the [EXP-Bench dataset page](https://huggingface.co/datasets/Just-Curieous/EXP-Bench).

## Acknowledgement

API inference compute for parity experiments is generously supported by
[2077AI](https://www.2077ai.com/) (https://www.2077ai.com/).
