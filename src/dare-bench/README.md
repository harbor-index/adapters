## DARE-Bench → Harbor Adapter

## Overview

DARE-Bench evaluates whether an LLM agent can carry out data-science work: follow a prescribed
machine-learning pipeline exactly, build a good predictive model, or forecast a time series, by
writing and running Python against real Kaggle-derived data. This adapter covers the **public
evaluation release** of [Snowflake-Labs/dare-bench](https://github.com/Snowflake-Labs/dare-bench):
162 entries, each with two question variants, giving **324 Harbor tasks**.

| Task type | v1 | v2 | Entries | Harbor tasks |
| --- | --- | --- | ---: | ---: |
| classification | instruction following (IF), exact match | ML modeling (MM), macro-F1 | 68 | 136 |
| regression | instruction following (IF), `numpy.allclose` | ML modeling (MM), clipped R² | 42 | 84 |
| time series | exogenous-feature forecasting (XF), clipped R² | canonical forecasting (CF), clipped R² | 52 | 104 |
| **Total** | | | **162** | **324** |

The released set is the paper's "strictly open-sourceable subset" (Appendix, Table "Performance
on the Strictly Open-Sourceable Subset"). The paper's 352-task full benchmark and its training
data are not part of this adapter. Code is Apache-2.0; each dataset keeps its original Kaggle
license (`LICENSE/kaggle_license_test.csv` upstream; recorded per task in `task.toml`).

Adaptation changes, all kept minimal:
- The original question is used verbatim, after a one-paragraph preamble that says where the files
  are (`/workspace`) and where `prediction.csv` goes.
- The agent runs in a container whose Python packages mirror the data-science subset of
  SandboxFusion's Python runtime, the sandbox the paper evaluates in.
- Scoring uses upstream's `scripts/evaluation.py`, copied verbatim.

## What is DARE-Bench?

DARE-Bench (Shu et al., ICLR 2026, [arXiv:2602.24288](https://arxiv.org/abs/2602.24288)) is a
"training-focused" data-science agent benchmark with verifiable ground truth. Each task gives a
natural-language question, a `metadata.txt` describing the dataset, and train/test files
(CSV, Parquet or multi-table SQLite).

- **Instruction-following** questions spell out filtering ranges, features, imputers, encoders,
  the model and its random seed. The agent's predictions must equal those of upstream's
  reference-solution code; 20% of the training rows carry injected out-of-range noise that the
  stated filters remove.
- **Modeling and forecasting** questions only state the goal and are scored against held-out
  labels.

Scores are reported per category as a percentage; a missing prediction counts as 0.

## Adapter Features

- **One task per (entry, variant).** The agent sees exactly that variant's `needed_files` plus
  `metadata.txt`.
- **Isolated grading.** The verifier runs in a separate, fresh copy of the task image
  (`[verifier] environment_mode = "separate"`); only `/workspace/prediction.csv` is carried over as
  an artifact. Changes the agent makes to packages, the interpreter or other files cannot affect
  the score.
- **Hidden material stays in `tests/`.** Held-out labels, the clean training split, and the
  reference-solution code are never in the agent image.
- **Faithful IF labels.** As upstream does with `force_simulate=true`, the verifier generates
  the IF label at verification time inside the task container. It runs upstream's reference
  code, rendered from `all_metadata.json` exactly as `generate_reference_solution_code()`
  renders it, on the clean split. The label is produced by the same packages the agent used.
  (Upstream's README notes these labels are environment-dependent.) Upstream's live harness renders
  `utils.GT_GENERATION_TEMPLATE` rather than `reference_solution.REFERENCE_SOLUTION_TEMPLATE`; the two
  differ only in logging, an unused evaluation block (validation files carry no target) and extra
  return-dict entries. Preprocessing, training and prediction code are identical.
- **Verbatim upstream scorer.** `tests/dare_evaluation.py` is a byte-identical copy of
  upstream `scripts/evaluation.py`, with the same row alignment (`row_id` or composite keys for
  CF), metrics and multi-target averaging. A malformed or missing `prediction.csv` scores 0.
- **Pinned runtime.** Python 3.10 with SandboxFusion's pins (scikit-learn 1.4.0, pandas 2.2.0,
  numpy 1.26.3, scipy 1.11.4, statsmodels, xgboost, lightgbm, catboost, pmdarima, torch CPU, …)
  plus pyarrow for the Parquet entries. BLAS threads are fixed to 1.
- **Parity agent.** `dare_bench.dare_parity_agent:DAREBenchParityAgent` ports upstream's agent
  loop for matched comparisons (see Parity).

## Generated Task Structure

```
dare-bench/
├── {file_path}-{v1|v2}/
│   ├── task.toml                  # metadata: variant, family, metric, dataset license
│   ├── instruction.md             # preamble + original question (verbatim)
│   ├── environment/
│   │   ├── Dockerfile             # python:3.10-slim + SandboxFusion data-science pins
│   │   ├── requirements.txt
│   │   └── workspace/             # needed_files for this variant + metadata.txt
│   ├── solution/
│   │   ├── solve.sh
│   │   ├── spec.json              # pipeline parameters stated in the question
│   │   └── if_solver.py | mm_solver.py
│   └── tests/
│       ├── test.sh                # writes /logs/verifier/reward.txt
│       ├── verify.py
│       ├── dare_evaluation.py     # verbatim upstream scripts/evaluation.py
│       ├── spec.json, metadata.json
│       ├── labels/                # MM/XF/CF: released ground truth
│       └── reference/             # IF: reference_solution.py + clean train split + val split
```

Adapter code:
```
src/dare-bench/
├── README.md, adapter_metadata.json, parity_experiment.json, pyproject.toml, run_dare-bench.yaml
├── tests/test_adapter.py
└── src/dare_bench/
    ├── adapter.py, main.py
    ├── dare_parity_agent.py       # port of the original agent loop (parity)
    ├── sandbox_executor.py        # in-container stand-in for SandboxFusion /run_code
    └── task-template/
```

## Run Evaluation / Harness

### Running with Datasets Registry

After the dataset is published:
```bash
harbor run -d snowflake-labs/dare-bench                         # oracle
harbor run -d snowflake-labs/dare-bench -a <agent> -m "<model>"
```

### Using Job Configurations

```bash
harbor run -c src/dare-bench/run_dare-bench.yaml            # oracle by default
harbor run -p datasets/dare-bench -a <agent> -m "<model>"
harbor job resume -p /path/to/jobs/directory
```

### Running Individual Trial

```bash
harbor trial start -p datasets/dare-bench/<task_id>
harbor trial start -p datasets/dare-bench/<task_id> -a <agent> -m "<model>"
```

## Usage: Create Task Directories

The adapter reads a local checkout of the upstream repository (data are in Git, about 1.6 GB):

```bash
git clone --filter=blob:none --sparse https://github.com/Snowflake-Labs/dare-bench.git
git -C dare-bench sparse-checkout set scripts data/eval LICENSE
git -C dare-bench checkout 01447145304c67b861a004ada6d86f29640de61a

cd src/dare-bench
uv sync
uv run dare-bench --source-dir /path/to/dare-bench --output-dir ../../datasets/dare-bench
uv run dare-bench --source-dir /path/to/dare-bench --output-dir ../../datasets/dare-bench-parity --split parity
```

Flags: `--output-dir`, `--limit`, `--overwrite`, `--task-ids <file_path>-v1 ...`, `--source-dir`
(required), `--split full|parity`, `--variant all|v1|v2`. Task ids are the upstream `file_path`
(lowercased, unsafe characters replaced by `-`) plus `-v1`/`-v2`. The parity split is a fixed,
seeded sample of 5 tasks from each of the six categories (30 tasks), drawn before any model run.

## Oracle

`solution/solve.sh` uses only the files in `/workspace`.

- **IF tasks (110):** `if_solver.py` performs the stated pipeline and reproduces the reference
  output exactly: **110/110 = 1.0**. It handles two data details the question leaves implicit:
  - boolean ranges `[False, True]` apply to columns stored as `0/1` strings;
  - one entry stores numeric-coded categoricals as strings in training but floats in validation,
    so training is cast to the validation dtype.
- **MM/XF/CF tasks (214):** these are scored by macro-F1 or clipped R² against real held-out
  values, so no legitimate solution reaches 1.0. `mm_solver.py` is a gradient-boosting baseline
  that never reads labels. Following merged adapters with continuous rewards (MLGym-Bench,
  SLDBench, FrontierCS, AlgoTune), the oracle is reported with its real scores. The verifier is
  separately checked by replaying each label file as the prediction, which scores 1.0 on all 324
  tasks.

| Category | Tasks | Oracle mean reward |
| --- | ---: | ---: |
| classification IF | 68 | 1.000 |
| regression IF | 42 | 1.000 |
| classification MM | 68 | 0.716 |
| regression MM | 42 | 0.667 |
| time series XF | 52 | 0.483 |
| time series CF | 52 | 0.215 |

Full run: 324 trials, 0 exceptions, about 14 min at 12-way concurrency (local Docker, separate verifier).
Run on Harbor 0.26.0 and again on 0.27.0 (latest stable), with identical rewards on all 324 tasks.
A NOP agent (no action) scores 0 on all 324 tasks with 0 exceptions.

**Daytona check.** A 12-task oracle subset (two per category, covering CSV, Parquet and SQLite and
the largest datasets) gives rewards identical to local Docker on all 12, and three parity-agent
tasks run cleanly on Daytona (0 exceptions). Task resources (4 vCPU / 8 GiB / 10 GiB) fit Daytona's
default per-sandbox limit; the full local oracle gives identical rewards at 8 GiB and 16 GiB.

## Comparison with Original Benchmark (Parity)

Setup (identical on both sides):
- **Original:** upstream harness (`scripts/run_agentic_reason.py`) at commit
  `01447145304c67b861a004ada6d86f29640de61a`, with the Hydra overrides from
  `scripts/run_parallel_datasci_auto.sh` (`max_turn=5`, `tool.python_executor.timeout=200`,
  `force_simulate=true`, reasoning effort `high`), run through
  `harbor_parity/run_original.sh` in the fork
  ([macabdul9/dare-bench](https://github.com/macabdul9/dare-bench/tree/harbor-parity), branch `harbor-parity`). Code runs in a SandboxFusion-compatible `/run_code` service
  built from the same package pins as the Harbor task image.
  - **GPT-6 patch:** upstream only sends `gpt-o4-mini`, `gpt-o3` and `gpt-5` through its
    Responses API client. A small patch to `models.py`, `remote_llm.py` and `python_executor.py`
    (5 lines changed) routes `gpt-6*` names down the same path; the gpt-5 branch's rename of the model to `gpt-5`
    is dropped so the requested model is the one called. Nothing else in the agent changes.
- **Harbor:** `dare_bench.dare_parity_agent:DAREBenchParityAgent` (v1.3.0), a port of the same
  loop. It implements both upstream API paths: chat completions (`max_tokens=16384,
  temperature=0.7, top_p=0.8`) and the Responses API used for reasoning models
  (`max_output_tokens=16384`, `reasoning={"effort": "high", "summary": "auto"}`,
  `text={"verbosity": "medium"}`, no sampling parameters). The verbatim question is the only user
  message; the `python_executor` schema, description and error strings match upstream; runs are
  stateless with explicit `files_to_load`/`files_to_save`; 200 s per call, 5 turns.
  - **Sandbox isolation:** upstream runs model code in a separate SandboxFusion container that
    holds only the loaded files. The Harbor agent reproduces this inside the task container by
    running each call as an unprivileged user in a fresh directory, with `/workspace` and the
    agent's private directory at mode 700. Code therefore sees only what it loaded, and results
    reach `/workspace` only through `files_to_save`. SandboxFusion runs code as root, so `/mnt` is
    left world-writable (sticky): models that write to `/mnt/data` succeed on both sides.
- **Checked offline:** with a scripted mock endpoint, the two sides send JSON-identical requests
  (including tool results) on both API paths, and score the same.

Results: fixed seeded split of 30 tasks (5 per category, `parity_task_ids.txt`), 3 runs per side,
mean ± sample SEM over run means. Model served through an OpenAI-compatible LiteLLM gateway.
0 exceptions and 0 harness crashes across 180 runs.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|------------------|--------------|------------------------------|----------------------------|
| dare-bench-parity-agent@1.3.0 | gpt-6.1-sol | mean reward | 3 | 30 of 324 | 0.791 ± 0.017 | 0.801 ± 0.005 |

Per run: original 0.809 / 0.808 / 0.756; Harbor 0.792 / 0.803 / 0.809. The run ranges overlap
overall and in every category.

| Category (5 tasks each) | Original | Harbor |
| --- | ---: | ---: |
| classification IF | 1.000 ± 0.000 | 1.000 ± 0.000 |
| classification MM | 0.539 ± 0.040 | 0.486 ± 0.009 |
| regression IF | 1.000 ± 0.000 | 1.000 ± 0.000 |
| regression MM | 0.745 ± 0.001 | 0.689 ± 0.037 |
| time series XF | 0.795 ± 0.046 | 0.863 ± 0.024 |
| time series CF | 0.665 ± 0.059 | 0.769 ± 0.032 |

An earlier Harbor run with agent v1.2.0, before `/mnt` was made writable, scored
0.787 ± 0.012 (0.773 / 0.778 / 0.812), also overlapping; the table reports the final v1.3.0 runs.

With 5 tasks per category the per-category numbers are noisy (a single task moves a category
mean by 0.2, and a run with no prediction scores 0); the differences go both ways. Per task,
the two sides' scores are close; category gaps come mostly from single runs that produced no
prediction.

**Standard CLI agent (Harbor only).** To show the tasks work outside the custom loop, codex was run
on the same split: `codex@0.171.0` + gpt-6.1-sol scores **0.806 ± 0.009** (0.817 / 0.815 / 0.788;
1 agent timeout in 90 trials). Codex has a full shell and no turn limit, so this is not comparable to
the original harness or the paper.

```bash
harbor run -p datasets/dare-bench-parity -a codex -m openai/gpt-6.1-sol -n 4
```

Reproduce:
```bash
# Original side (https://github.com/macabdul9/dare-bench, branch harbor-parity)
DARE_DATA=/path/to/dare-bench OPENAI_API_KEY=... OPENAI_BASE_URL=... \
  harbor_parity/run_original.sh parity_task_ids.txt gpt-6.1-sol run1 out/original-run1

# Harbor side
PYTHONPATH=src/dare-bench/src OPENAI_API_KEY=... OPENAI_BASE_URL=... \
  harbor run -p datasets/dare-bench-parity -a dare_bench.dare_parity_agent:DAREBenchParityAgent \
  -m openai/gpt-6.1-sol -n 8
```

Links:
- Original benchmark: https://github.com/Snowflake-Labs/dare-bench
- Original-side fork: https://github.com/macabdul9/dare-bench/tree/harbor-parity
- Adapter PR: TBD
- Dataset PR: https://huggingface.co/datasets/harborframework/harbor-datasets/discussions/70
- Parity results: https://huggingface.co/datasets/harborframework/parity-experiments/discussions/305 (files at https://huggingface.co/datasets/macabdul9/dare-bench-parity-experiments until the shared dataset accepts uploads; see https://github.com/harbor-framework/adapters/issues/129)

Reference point (paper appendix, open-sourceable subset = this release, 3 runs): gpt-4.1 scores
class-IF 55.39, class-MM 57.75, reg-IF 50.00, reg-MM 57.68, time-XF 41.19, time-CF 6.81.

## Notes & Caveats

- **Environment sensitivity of IF labels.** Upstream labels depend on the package versions.
  Moving from scikit-learn 1.5.2 to SandboxFusion's 1.4.0 changed 4 of 110 labels. The adapter
  therefore generates labels in the agent's own container at verification time. Two independent
  generations were byte-identical.
- **Paper vs. code.** The paper says greedy decoding; on the chat-completions path the code sends
  temperature 0.7 (the script's 0.0 is replaced by `temperature or 0.7`). Reasoning models on the
  Responses API get no sampling parameters.
  The tool description advertises "up to 5" invocations (`max_turn`), not the "3" printed in the
  paper's appendix example.
- **Tool errors and crashes.** A failed tool call (missing or unsafe file in `files_to_load`,
  oversized file, bad arguments) returns upstream's error message to the model and the episode
  continues. Upstream's harness itself crashes, scoring 0, only on invalid tool-call JSON on the
  chat-completions path, non-object JSON arguments, an unexpected response shape, or an API
  error (no retry). The parity agent reproduces those cases by ending the episode and removing
  `prediction.csv`.
- **Standard CLI agents** (Claude Code, Codex, …) work on these tasks with a normal shell (see the
  codex run under Parity); that is a different setting from the 5-turn, 200 s tool budget and is not
  comparable to paper numbers. On a rate-limited endpoint keep concurrency low: codex issues many
  requests per trial.
- **Held-out labels are public.** MM, XF and CF labels are real held-out rows of public Kaggle
  datasets, and `metadata.txt` keeps upstream's dataset description. Tasks allow network access
  (agents need it to reach their model API, and upstream's sandbox is not network-restricted), so an
  agent could in principle fetch the source dataset. Treat unusually high MM/XF/CF scores with
  suspicion and check trajectories for downloads.
- **Image size:** about 2.5 GB (torch CPU, xgboost, catboost, lightgbm). The build layers are
  shared across tasks.
- **Data licenses:** CC-BY-SA, CC-BY-NC(-SA), ODbL and others apply to some entries; see
  `dataset_license` in each `task.toml`.

## Installation / Prerequisites

- Docker, Harbor (`uv tool install harbor`; tested on 0.26.0 and 0.27.0), Python 3.11+ with `uv`.
- `cd src/dare-bench && uv sync`.
- A dare-bench checkout (see Usage).
- Parity runs need `OPENAI_API_KEY` and, for a compatible endpoint, `OPENAI_BASE_URL`.

## Troubleshooting

- *Verifier exits non-zero with "Infrastructure error":* the IF reference code failed to run;
  check `/logs/verifier/reference_run.log`. This is a fixture problem, not an agent failure.
- *"all predefined address pools have been fully subnetted":* with the separate verifier each
  trial uses two Docker networks, and Docker's default pool holds about 30. Keep `-n` at 12 or
  below on local Docker, or enlarge `default-address-pools` in `/etc/docker/daemon.json`.
- *Slow first build:* the shared Python layer installs about 2 GB of wheels; later tasks reuse it.

## Citation

```bibtex
@inproceedings{shu2026darebench,
  title={DARE-Bench: Evaluating Modeling and Instruction Fidelity of LLMs in Data Science},
  author={Shu, Fan and Wang, Yite and Wu, Ruofan and Liu, Boyi and Yao, Zhewei and He, Yuxiong and Yan, Feng},
  booktitle={International Conference on Learning Representations},
  year={2026}
}
```

## Authors & Contributions

This adapter is developed and maintained by Abdul Waheed (Carnegie Mellon University, abdulw@cs.cmu.edu) for Harbor-Index v2.0.

**Issues and Contributions:**
- Submit issues and pull requests to the adapters repository.

## Acknowledgement

> API inference compute for the parity experiments was provided through Carnegie Mellon University's AI gateway.
