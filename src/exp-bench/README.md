## EXP-Bench → Harbor Adapter

## Overview

EXP-Bench evaluates whether AI agents can carry out an AI research experiment: given a
research question from an ICLR 2024 or NeurIPS 2024 paper and the paper's repository with
key files masked, the agent designs the experiment, implements and runs it, and states a
conclusion.

- **Source:** 461 tasks from 51 papers, [arXiv:2505.24785](https://arxiv.org/abs/2505.24785).
  Dataset on [Hugging Face](https://huggingface.co/datasets/Just-Curieous/EXP-Bench)
  (CC-BY-4.0); harness in [Just-Curieous/Curie](https://github.com/Just-Curieous/Curie/tree/main/benchmark/exp_bench) (Apache-2.0).
- **Task types:** type 1 (210) masks source files to re-implement, type 2 (123) masks the
  scripts that run an experiment, type 3 (128) masks only the README.
- **This adapter: 421 tasks.** 40 tasks cannot be graded by the upstream pipeline and are
  excluded (see Notes & Caveats).
- **Main adaptations:** repositories are pinned to the commit current when the tasks were
  built (upstream clones `HEAD`), and the judge uses any OpenAI-compatible endpoint
  (upstream uses Azure). Prompts, masking, judge prompts and scores are unchanged.

## What is EXP-Bench?

Each task gives a research question, a method description and optional step-by-step
instructions, plus the masked repository. An LLM judge (o3-mini) grades the run:

- **D (design):** share of ground-truth design variables matched.
- **I (implementation):** share of setup requirements met by the agent's git diff.
- **C (conclusion):** whether the conclusion matches the expected outcome.
- **E (execution):** whether the agent's `reproduce_exp_bench.sh` runs.
- A log monitor first checks that the agent did not read the paper, recover masked files
  with git, or fake results; otherwise every score is 0.

The paper's best configuration (OpenHands + o3-mini) reached D 18.4, I 20.3, C 21.0 and
0.5% fully correct and executable experiments.

## Adapter Features

- Tasks from the full upstream task records, with stable ids
  `<conference>-<paper_id>-<task_index>` and names `just-curieous/exp-bench__<task id>`.
- Environment: the repository at its pinned commit in `/workspace`, masked as upstream
  `mask_repo` does.
- Instruction: upstream `eval_gen_prompt.txt`, filled as upstream fills it for its agents.
- Verifier (`tests/judge.py`): a port of upstream `judge.py` (diff generation, the four
  judge prompts, retries, chunking of large diffs, log monitor, 1-hour execution check),
  with judge dependencies pinned to upstream's.
- Rewards (`/logs/verifier/reward.json`): `design`, `implementation`, `conclusion`,
  `execution`, conjunctive metrics, `monitor_pass`, and `reward` (= `conclusion`).
- Any Harbor agent can run the tasks; no custom agent.

## Generated Task Structure

```
exp-bench/
├── {task_id}/                       # e.g. neurips2024-97649-0
│   ├── task.toml                    # metadata, judge env, resources
│   ├── instruction.md               # upstream agent prompt for this task
│   ├── environment/
│   │   ├── Dockerfile
│   │   └── prepare_workspace.sh     # clone at pinned commit, mask
│   ├── solution/                    # oracle: restore sources, write reference answer
│   └── tests/
│       ├── test.sh                  # installs judge deps, runs judge.py
│       ├── judge.py
│       ├── prompts/                 # upstream judge prompts
│       └── task_data.json           # task record and reference sources
```

Adapter code: `src/exp_bench/adapter.py`, `main.py`, `prompts/eval_gen_prompt.txt`,
`data/exp_bench_tasks.json.gz` (task records, pinned commits, source hashes),
`data/parity_task_ids.txt`, and `task-template/`. `scripts/build_task_bundle.py` shows how
the bundle is built from upstream outputs.

## Run Evaluation / Harness

### Running with Datasets Registry

```bash
EXP_BENCH_ORACLE_VALIDATION=1 harbor run -d just-curieous/exp-bench   # oracle
harbor run -d just-curieous/exp-bench -a claude-code -m "<model>"
harbor run -d just-curieous/exp-bench@parity -a claude-code -m "<model>"
```

The verifier needs `OPENAI_API_KEY` (and `OPENAI_BASE_URL` for a non-OpenAI endpoint).
`EXP_BENCH_JUDGE_MODEL` defaults to `o3-mini`.

**Deferred judging.** With `EXP_BENCH_DEFER_JUDGE=1` the verifier saves the agent's answer,
patch, logs and execution check to `verifier/judge_inputs.json` instead of calling the
judge; grade the job later with the same judge code:

```bash
cd src/exp-bench
OPENAI_API_KEY=... uv run --with openai==1.66.3 --with tiktoken==0.9.0 \
    --with langchain-text-splitters==0.3.6 python scripts/judge_deferred.py <job-dir>
```

### Using Job Configurations

```bash
harbor run -c src/exp-bench/run_exp-bench.yaml
harbor run -p datasets/exp-bench -a claude-code -m "<model>"
harbor job resume -p /path/to/jobs/directory
```

### Running Individual Trial

```bash
EXP_BENCH_ORACLE_VALIDATION=1 harbor trial start -p datasets/exp-bench/neurips2024-98316-5
harbor trial start -p datasets/exp-bench/neurips2024-98316-5 -a claude-code -m "<model>"
```

## Usage: Create Task Directories

```bash
cd src/exp-bench
uv sync
uv run exp-bench --output-dir ../../datasets/exp-bench                     # all 421 tasks
uv run exp-bench --split parity --gpus 1 --output-dir ../../datasets/exp-bench-parity
uv run exp-bench --task-ids neurips2024-97649-0 --overwrite
uv run exp-bench --gpus 1 --agent-timeout-hours 1 --output-dir ../../datasets/exp-bench-gpu
```

Generation downloads each task's reference source files at the pinned commit, checks
their sha256, and caches them in `~/.cache/harbor-exp-bench-adapter`.

## Comparison with Original Benchmark (Parity)

Parity has not been run yet. Values are reported as mean ± sample SEM.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|------------------|--------------|------------------------------|----------------------------|
| claude-code@2.1.330 | claude-haiku-5-5 (proposed) | conclusion (C) | 3 | 57 (13.5%) | TBD | TBD |

Plan (to be confirmed with the Harbor team):

- **Subset:** 57 tasks from 40 papers in `src/exp_bench/data/parity_task_ids.txt`: 27 tasks
  whose experiments can finish on CPU within the time limit, plus a random sample of 30
  tasks that need a GPU, stratified by task type (selection rule in the file header). Each
  task gets 1 GPU on both sides.
- **Agent:** Claude Code at a pinned version, one low-cost model, 30-minute limit. Upstream
  only ships an OpenHands build from Feb 2025 that cannot call current models, so Claude
  Code is added to the original harness with Harbor's install command and flags.
- **Judge:** `claude-haiku-5-5` on both sides (`EXP_BENCH_JUDGE_MODEL=claude-haiku-5-5`,
  `OPENAI_BASE_URL=https://api.anthropic.com/v1/`, an Anthropic key in `OPENAI_API_KEY`).
- **Runs:** 5-task sanity check, then 1 full run, then 3 runs per side. Main metric C; D, I
  and E are also compared, by overlap of run ranges and per-task MSE.
- **Original side:** branch [`harbor-parity`](https://github.com/Just-Curieous/Curie/tree/harbor-parity)
  of the official EXP-Bench repository; commands in `benchmark/exp_bench/harbor_parity/README.md`.
- **Harbor side:**
  ```bash
  uv run exp-bench --split parity --gpus 1 --output-dir ../../datasets/exp-bench-parity
  harbor run -c src/exp-bench/run_exp-bench_parity.yaml
  ```

## Notes & Caveats

- **Pinned repositories.** Each repository is fetched at the last commit before its
  paper's tasks were built (recorded in `task.toml`); reference sources for the setup
  judge come from that commit, where upstream downloads them from `main`/`master`.
- **40 excluded tasks** (`EXPBenchAdapter.invalid_reason`). 36 fail upstream's masking
  step (33 list a file that never existed in the repository, 3 list `README.md`) and
  upstream skips them as `invalid_task`. For 4 (`iclr2024-18447-11`,
  `neurips2024-97791-5`, `iclr2024-18439-0`, `iclr2024-18439-2`) the reference files alone
  exceed o3-mini's 200k-token context, so the released judge cannot grade them; the
  authors' earlier runs could, because reference files were added to the prompt only in
  May 2025. `scripts/audit_judge_context.py` recomputes the list for another judge.
  `neurips2024-94391-12` leaves about 7k tokens for the agent's patch.
- **Environment.** Upstream runs agents in the OpenHands sandbox with the repository
  mounted at `/workspace`; here the repository is baked into a miniconda image (the base
  of upstream's image).
- **Execution check** runs in the agent's container after the agent finishes; upstream
  starts a fresh container, so packages installed outside `/workspace` do not carry over
  there and E can be higher here.
- **Monitor input** is the agent's console log (`/logs/agent/openhands.txt` or
  `claude-code.txt`; all text logs for other agents), truncated head and tail to fit the
  judge context where upstream would fail.
- **Credentials folder.** `setup_apis_exp/env.sh` exports `OPENAI_API_KEY` /
  `OPENAI_BASE_URL` from `EXP_BENCH_TASK_OPENAI_API_KEY` / `EXP_BENCH_TASK_OPENAI_BASE_URL`
  (empty unless set), and is excluded from the judged diff.
- **Claude judge (optional).** `EXP_BENCH_JUDGE_MODEL=claude-*` with
  `OPENAI_BASE_URL=https://api.anthropic.com/v1/` sends a one-line user turn instead of an
  empty one, a strict JSON schema instead of `json_object`, and no `temperature`; prompts
  are unchanged. Scores are not comparable to o3-mini scores.
- **Conjunctive metrics.** The paper does not state their cut-offs; `all_correct` here
  requires D = I = 100 and C correct.
- **Time limit** is 0.5 hours (upstream default; the paper mentions a 40-minute soft limit).
- **GPUs.** Upstream agents had 4× A40; tasks default to `gpus = 0` here.
- **Oracle.** EXP-Bench has no reference trajectory: the oracle restores the masked files
  and writes the ground-truth answer. Run it with `EXP_BENCH_ORACLE_VALIDATION=1`, which
  skips the monitor and execution check. Type 3 tasks have no reference code, so their
  oracle `implementation` is low by design.
- **Task data.** The public CSV omits fields the judge needs, so the full upstream task
  records ship with the adapter under the dataset's CC-BY-4.0 license.

## Installation / Prerequisites

- Docker (or a Harbor cloud environment such as Daytona)
- Harbor: `uv tool install harbor`
- `cd src/exp-bench && uv sync`
- Judge credentials: `OPENAI_API_KEY`, optional `OPENAI_BASE_URL`, `EXP_BENCH_JUDGE_MODEL`.
- Optional credentials for experiments that call an LLM: `EXP_BENCH_TASK_OPENAI_API_KEY`,
  `EXP_BENCH_TASK_OPENAI_BASE_URL`.

## Troubleshooting

- **"EXP-Bench masking failed" at build:** a listed source file is missing at the pinned
  commit; report the task id.
- **Judge dependency install fails:** the verifier needs network access to PyPI and the
  judge endpoint.
- **Long verifier runs:** the execution check can take an hour; `EXP_BENCH_EXEC_CHECK=0`
  skips it (E is then 0).

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

Developed and maintained by Jingjia Peng (EXP-Bench co-author). Yuwei Han started the
initial scaffold in [harbor-framework/harbor#742](https://github.com/harbor-framework/harbor/pull/742).

- Submit issues and pull requests to the adapters repository.
- Report dataset errors on the [EXP-Bench dataset page](https://huggingface.co/datasets/Just-Curieous/EXP-Bench).

## Acknowledgement

API inference compute for parity experiments is generously supported by
[2077AI](https://www.2077ai.com/) (https://www.2077ai.com/).
