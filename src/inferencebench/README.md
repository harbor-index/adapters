# InferenceBench → Harbor Adapter

## Overview

InferenceBench measures how well a coding agent can deploy and optimize an OpenAI-compatible LLM inference server.

- **Task type:** open-ended systems optimization with a wall-clock budget
- **Size:** 12 tasks (4 scenarios × 3 seed pairs), all of them adapted
- **Model served:** `mistralai/Mistral-7B-Instruct-v0.3`
- **Resources per task:** 1 NVIDIA H100 80 GB GPU, 16 CPUs, 180 GB of memory, a 2-hour agent budget
- **Score:** speedup over a naive PyTorch server. It is unbounded, and a failed run counts as 1×.
- **License:** Apache-2.0 (benchmark code). The adapter derives its verifier scripts from the benchmark's own Harbor adapter, `src/harbor_adapter`.

Task IDs have the form `inferencebench-<scenario>-mistral-7b-<development seed>-<evaluation seed>`, for example `inferencebench-a-mistral-7b-21-1337`.

## What is InferenceBench?

[InferenceBench](https://github.com/aisa-group/InferenceBench) ([paper](https://arxiv.org/abs/2607.20468), [site](https://inferencebench.ai/)) gives an agent a model, one GPU, an optimization scenario, and a time budget. The agent writes a launcher, `start_server.sh`. The harness then restarts that launcher in a clean process tree and measures it on requests chosen by a held-out seed.

| Scenario | Requests | Primary metric |
|---|---|---|
| A (input heavy) | 8192 tokens in, 1024 out | 1 / TTFT p50 |
| B (output heavy) | 1024 tokens in, 8192 out | 1 / TPOT p50 |
| C (high load) | 1024 in, 1024 out; burst, Poisson, and constant load | geometric mean of request throughput |
| D (general) | 4096 in, 2048 out | geometric mean of 1 / TTFT p50, 1 / TPOT p50, and request throughput |

Two gates decide whether a run counts. The quality gate requires at least 95% of the PyTorch baseline's accuracy on 500 MMLU-Pro questions. The integrity gate is a Claude judge that reviews the submission for disallowed shortcuts.

## Adapter Features

- Generates all 12 tasks from a pinned upstream commit, using upstream's prompt, evaluator, quality gate, judge, and package locks.
- Runs on any Harbor environment that provides a GPU and a non-root agent user, with no extra volumes. `harbor run -d` works without setup steps.
- Reports the paper's numbers. See [the reward table](#reward).
- Uses vLLM with default settings as the oracle, and compares it with the paper's "vLLM default" row.
- Adds `inferencebench.agents:ClaudeCodeUntilDeadline`, which repeats upstream's continuation loop for parity runs (see [Notes & Caveats](#notes--caveats)).
- Adds `inferencebench-score`, which computes the paper's per-scenario means and aggregate from Harbor job directories.

### Reward

`/logs/verifier/reward.json` has these keys:

| Key | Value |
|---|---|
| `reward` | `speedup` when `valid` is 1, otherwise 1.0. This is one cell of the paper's Table 2. |
| `valid` | 1 when the run passes both gates and the judge did not flag it |
| `speedup` | `primary_metric` divided by the PyTorch baseline; 0 if there is no valid measurement |
| `primary_metric` | the raw metric from the table above |
| `eval_valid`, `speed_pass`, `quality_pass` | 1 or 0 for the evaluation and its checks |
| `judge_valid` | 1 when the judge ran and wrote valid verdicts. Without valid verdicts the run is not flagged, so check this key before you report results. |
| `contamination_detected`, `disallowed_use_detected` | 1 or 0 from the judge |

The PyTorch baseline comes from the paper's Table 12: 840 ms TTFT (A), 41 ms TPOT (B), 0.132 req/s (C), and a score of 1.68 (D).

Harbor's default `Mean` averages raw speedups across very different scales. Use `inferencebench-score` for results that compare with the paper.

## Generated Task Structure

```
inferencebench-a-mistral-7b-21-1337/
├── task.toml             # resources, timeouts, per-task environment, collect hook
├── instruction.md        # prompt from upstream's get_prompt.py
├── environment/
│   ├── Dockerfile        # upstream's image, plus the evaluator bundle and benchmark samples
│   ├── collect.sh        # stops the agent and prepares the sandbox for the verifier
│   ├── inference_eval/   # evaluator available to the agent
│   └── home/task/        # the agent's working directory
├── solution/
│   ├── solve.sh          # oracle: installs vLLM 0.11.0
│   └── start_server.sh   # upstream's default vLLM launcher
└── tests/
    ├── test.sh           # judge, final evaluation, reward
    ├── write_reward.py
    └── ib/               # evaluator, scenario files, and judge for the final evaluation
```

Adapter code:

```
src/inferencebench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_inferencebench.yaml       # oracle on a Docker host with GPUs
├── inferencebench_daytona.yaml   # oracle on Daytona
├── inferencebench_modal.yaml     # oracle on Modal
├── inferencebench_parity.yaml    # Harbor side of a parity run
├── tests/test_adapter.py
└── src/inferencebench/
    ├── adapter.py
    ├── agents.py                 # continuation-loop agent
    ├── main.py
    ├── score.py
    ├── data/quality/             # PyTorch accuracy reference
    └── task-template/
```

## Run Evaluation / Harness

### Running with the registry

The dataset is not published yet. After publication:

```bash
harbor run -d aisa-group/inferencebench -e daytona -a claude-code -m anthropic/claude-sonnet-4-6
```

### Using a job configuration

```bash
cd src/inferencebench
uv run inferencebench --output-dir ../../datasets/inferencebench
cd ../..

# Oracle on a Docker host with GPUs
uv run --project src/inferencebench --with harbor==0.25.0 harbor run -c src/inferencebench/run_inferencebench.yaml

# Oracle on Daytona
uv run --project src/inferencebench --with 'harbor[daytona]==0.25.0' harbor run -c src/inferencebench/inferencebench_daytona.yaml

# Oracle on Modal
uv run --project src/inferencebench --with 'harbor[modal]==0.25.0' harbor run -c src/inferencebench/inferencebench_modal.yaml
```

### Running individual trials

```bash
harbor trial start -p datasets/inferencebench/inferencebench-a-mistral-7b-21-1337 -e daytona -a oracle
```

### Reading the results

```bash
uv run --project src/inferencebench inferencebench-score jobs/<job-name>
```

## Usage: Create Task Directories

```bash
cd src/inferencebench
uv run inferencebench --output-dir ../../datasets/inferencebench
uv run inferencebench --output-dir ../../datasets/inferencebench --limit 4 --overwrite
uv run inferencebench --output-dir ../../datasets/inferencebench --task-ids inferencebench-a-mistral-7b-21-1337
```

The adapter fetches the pinned upstream commit into `~/.cache/harbor-inferencebench`. `--upstream-dir` uses another checkout, and `--num-hours` changes the budget (the paper's main results use 2).

## Comparison with Original Benchmark (Parity)

Parity has not been run yet.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|------------------|--------------|--------------------------------|----------------------------|
| TBD | TBD | Aggregate speedup | TBD | 12 (100%) | TBD | TBD |

Oracle reference values from the paper's "vLLM default" row:

| Scenario | A | B | C | D | Aggregate |
|---|---|---|---|---|---|
| Speedup | 1.25× ± 0.04 | 2.25× ± 0.10 | 48.69× ± 1.74 | 1.96× ± 0.06 | 4.05× ± 0.07 |

Links: [original benchmark](https://github.com/aisa-group/InferenceBench), [adapter PR](https://github.com/harbor-framework/adapters/pull/71). The fork, dataset PR, and parity PR are pending.

Reproduction scripts for both sides will be added with the parity results.

## Notes & Caveats

**Oracle.** The score is an unbounded speedup, so no solution can earn a fixed maximum. The oracle ships vLLM with default settings. A correct run has `valid = 1` on all 12 tasks and values close to the table above; it does not have `reward = 1`. It confirms the task and verifier end to end, but says nothing about agent capability.

The PyTorch server itself cannot be the oracle. Upstream measures its baseline at concurrency 1 with a 900 s request timeout, whereas the final evaluation uses each scenario's concurrency and a 300 s timeout. In scenario B, about half of its requests would exceed the timeout, and a failed run also scores 1×, so a score near 1× would not show that anything worked.

**Evaluation time limit.** Upstream's own Harbor adapter limits each evaluation attempt to 2 hours. This adapter does not, because scenario B alone takes vLLM's default settings about 2.4 hours, and any valid server slower than about 15 ms per token would fail on Harbor but not on the original benchmark.

**Quality reference.** `data/quality/` holds the PyTorch baseline's accuracy from the paper (128/500) and has not been recomputed yet. The upstream repository does not ship this file.

**Shared sandbox.** The final evaluation runs in the agent's sandbox, whereas upstream's own Harbor adapter uses a second sandbox and passes `/home/agent` through a Modal volume. With a shared sandbox, `harbor run` needs no volume, runs on any GPU environment, and keeps upstream's single HF cache. A second sandbox on any other environment would receive `/home/agent`, including agent-built engines and weights, through the machine that runs Harbor. The trade-off is that `harbor jobs regrade` does not apply. `harbor lint` reports `shared-verifier` for this reason. Isolation works as follows:

- The agent runs as the non-root user `agent`, as upstream runs it as the job user.
- Harbor runs the `[[verifier.collect]]` hook before it uploads `/tests`. `collect.sh` stops all of the agent's processes first, so nothing of the agent's is running when the evaluator and the evaluation seed arrive.
- `collect.sh` removes what the agent left in `/tmp`, `/var/tmp`, `/dev/shm`, and `/logs/verifier`, and keeps the `/tmp` targets of symbolic links in the task directory, as `run_task.sh` does.
- `test.sh` makes `/tests` readable only by root before it starts the agent's server.
- The image's `PATH` lists the agent-writable bin directory last, and the root scripts use a clean `PATH` and `PYTHONPATH`, so root never runs a program that the agent planted.
- The judge reviews a copy of the task directory at `/var/lib/ib_judge/task` while the real home directory is closed to it. The two paths in the judge prompt change accordingly.

**Differences from the original benchmark:**

| | Original (HTCondor and Apptainer) | Harbor |
|---|---|---|
| Agent restarts | reruns the CLI with a "continue" prompt until the budget ends; restarts a crashed agent up to 3 times | stock agents run once; `ClaudeCodeUntilDeadline` repeats the loop, and parity runs use it |
| Budget start | right before the agent starts | when the healthcheck finishes downloading the model |
| Server port | a free port chosen per run | fixed at 8000 |
| Evaluation time limit | none | evaluation stops 30 minutes before Harbor's 12-hour verifier limit, and the run then counts as failed |
| Agent's `PATH` | the user bin directory first | first for Claude Code and the evaluation server; other agents find it after the system directories |
| Prompt clause | "non-interactive mode" clause only for Claude | kept for every agent, because a registered task has one instruction |
| `nvidia-smi` wrapper, `check_cuda.py` | present | omitted |
| Starting points | `default`, `vllm_running`, `bare` | `default` only |
| `/data` bind mount | binds the host's `/data` read-only if it exists | none |

**Samples.** LongBench-v2 has exactly 503 rows and the benchmark samples 503, so the cached file is byte-identical for all seeds and the seed only chooses requests at evaluation time. The image build downloads it once in a shared layer and checks its sha256. Tasks therefore stay under 1 MB, and the build needs access to Hugging Face.

**Environments.** Daytona is the default for GPU runs. Modal runs GPU sandboxes under gVisor. The effect of either sandbox on latency measurements has not been measured; the oracle comparison will show it.

**Limitations.** The adapter covers the paper's main condition only: Mistral-7B on H100 with a 2-hour budget. The ablations with other models, budgets, and starting points are not included.

## Installation / Prerequisites

- [uv](https://docs.astral.sh/uv/) and Harbor 0.25.0 or newer
- One of:
  - Daytona: `uv tool install 'harbor[daytona]'`, then set `DAYTONA_API_KEY`
  - Modal: `uv tool install 'harbor[modal]'`, then `modal setup`
  - A host with Docker, the NVIDIA container runtime, and H100 GPUs
- `HF_TOKEN`: a read-only token whose account has accepted the terms of `mistralai/Mistral-7B-Instruct-v0.3`
- `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` for the judge, for every run, and `ANTHROPIC_BASE_URL` if the key belongs to a proxy. The judge uses `claude-sonnet-4-6`. Without a credential, the judge is skipped and `judge_valid` is 0.
- The API key of your agent

## Troubleshooting

- **The trial ends with an infrastructure error and no reward:** `collect.sh` did not finish. Read `artifacts/` and the trial log.
- **`valid` is 0 and `quality_pass` is 0:** read `verifier/metrics.json`. A quantized server can fall below the 95% threshold; this is a real benchmark outcome.
- **`eval_valid` is 0:** read `verifier/eval.log` and `verifier/server.log`.
- **The image build fails at `sha256sum`:** the Hugging Face dataset changed. The samples no longer match the published benchmark.
- **The healthcheck fails:** check that `HF_TOKEN` has access to the gated model.

## Citation

```bibtex
@article{yeon2026inferencebench,
  title   = {InferenceBench},
  author  = {Yeon, Jehyeok and Rank, Ben and Andriushchenko, Maksym},
  journal = {arXiv preprint arXiv:2607.20468},
  year    = {2026}
}
```

## Authors & Contributions

This adapter is developed and maintained by Yuanfan Chen. It builds on the Harbor adapter by the InferenceBench authors in [`src/harbor_adapter`](https://github.com/aisa-group/InferenceBench/tree/main/src/harbor_adapter) (Apache-2.0); `collect.sh` and `test.sh` are modified copies of their scripts.

**Issues and Contributions:** Please open issues or pull requests in the [adapters repository](https://github.com/harbor-framework/adapters).
