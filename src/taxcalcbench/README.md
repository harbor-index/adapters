# TaxCalcBench → Harbor Adapter

## Overview

This adapter converts all 101 cases of
[TaxCalcBench](https://github.com/column-tax/tax-calc-bench/tree/fdfe0db992ae9033f5b8b942633181a3596da64c)
into Harbor tasks. Upstream code and data are pinned to commit
`fdfe0db992ae9033f5b8b942633181a3596da64c`.

| Property | Value |
|---|---|
| Task | Read a taxpayer's documents and compute one tax return |
| Source | MIT-licensed upstream repository; [paper](https://arxiv.org/abs/2507.16126); [leaderboard](https://taxcalcbench.ai) |
| Coverage | 50 TY2025 cases and 51 TY2024 cases; no exclusions |
| Reward | 1 if every scored line is exact, otherwise 0 (upstream's "correct returns (strict)") |
| Verification | Deterministic; runs in a separate verifier container |
| Status | Oracle 101/101. Parity matched: all 101 tasks, 5 runs per side, the run ranges overlap on all four upstream metrics (see Parity). Codex CLI: 35.0% strictly correct returns over 100 of 101 tasks. Dataset PR and parity artifacts linked below |

## What is TaxCalcBench?

TaxCalcBench (Column Tax) tests whether a model can prepare a US personal income
tax return from the taxpayer's records. Each case has input documents and an
expected return in XML. Upstream defines the prompt, the scored lines, the
parsing rules and the comparison.

| Cases | Inputs | Return to compute |
|---|---|---|
| TY2025: 50 (10 each for US, CA, IL, NY, VA) | Taxpayer PDFs (such as W-2, W-2G, 1099 and 1098 forms and the prior-year Form 1040) and `remaining_data.json` | Federal Form 1040, California 540, Illinois IL-1040, New York IT-201 or Virginia 760 |
| TY2024: 51 | One `input.json` | Federal Form 1040 |

Upstream reports four numbers per model: correct returns (strict: every scored
line exact), correct returns (lenient: every line within $5), and the share of
correct lines under each rule. Its leaderboard is per tax year.

## Adapter Features

| Component | Implementation |
|---|---|
| Generation | [adapter.py](src/taxcalcbench/adapter.py) imports upstream's prompt builders and scored-line definitions from the pinned checkout and copies each case's inputs to `/app/input/` |
| Instruction | The upstream prompt for the case, unchanged, then an "Input files and answer location" block that lists the input paths and asks for the answer in `/app/output/return.txt` |
| Verifier | [score.py](src/taxcalcbench/task-template/tests/score.py) ports upstream's parsing and comparison (`TaxReturnEvaluator`); a unit test checks it against upstream's evaluator on every case |
| Isolation | The verifier runs in its own container and grades the collected `/app/output/return.txt`; the scorer and the expected return never enter the agent's container |
| Network | The agent phase has no network apart from the agent's model endpoint. See Notes & Caveats |
| Output | `/logs/verifier/reward.txt` holds the strict reward; `score.json` holds strict, lenient and by-line results per scored line |
| Oracle | Renders every scored line from the case's `output.xml` in upstream's output format |
| Task names | `column-tax/taxcalcbench__<task_id>`; TY2025 keeps the upstream case name, TY2024 gets the prefix `ty24-` |

**Parity agent.** Upstream has no agent: it makes one model call per case. The
adapter ships `taxcalcbench-direct@1.0.0`
([parity_agent.py](src/taxcalcbench/parity_agent.py)), which sends that same
call from the host and writes the reply into the task container.

| Part of the call | What the agent does |
|---|---|
| Prompt | Removes the appended block and sends the upstream prompt |
| Inputs | The PDF names downloaded from `/app/input` must equal the list in the prompt, and the JSON file must be there; a partial download stops the trial as an error |
| Model and level names | Upstream's own alias tables give the canonical names, as its CLI does; TY2025 calls also pass upstream's model list check |
| TY2025 input | One user message: the prompt plus each PDF as a base64 file part, sorted by file name |
| TY2024 input | The prompt string, which contains `json.dumps` of `input.json` as upstream's `run_tax_return_test` builds it |
| API | LiteLLM `responses` (OpenAI Responses API), `reasoning.effort` from upstream's thinking-level table, no tools |
| Streaming and timeout | TY2025: streaming with a 14,400 s request timeout; TY2024 at `high`: neither is set, as upstream |
| Reply | Collected with upstream's own stream collector and text extractor, imported from the pinned checkout |
| Retries and concurrency | No retries and one call in flight at a time, as upstream's sequential runner (`n_concurrent: 1` in the parity config); trials still start their containers in parallel |
| Failures | Upstream leaves a failed generation unscored. Here every failure of the call or of the agent leaves the agent as an error whose reward Harbor marks invalid; the parity config gives a trial timeout and a Docker or Daytona step that fails around the agent (`RuntimeError`) the same treatment through `override_error_policy`. `collect_parity.py` is the authority for which trials count and for the four parity numbers: it counts a trial only with evidence that its score belongs to the model's reply (see "Which trials count" below) |
| Evidence | `request_meta.json` is written before the call is sent and updated after it: outcome (`ok`, `api_failure`, `reply_not_delivered`, `cancelled` for a call that was cut off, or `started` when the process ended before any of these was written), request settings, API base URL (no credentials), upstream commit and the LiteLLM, OpenAI SDK and Harbor versions. A finished, failed or cancelled call also records whether the request had gone out and how far a stream got; a completed call records the model name the API returned, the response id and status and whether the stream ended with `response.completed`. The reply itself is kept as `agent/return.txt` |

A unit test captures the request that upstream's own code sends
(`run_tax_return_test`) and checks that the agent's request equals it for all
101 cases, for `openai/gpt-6-luna` at thinking level `high`. Before any trial
starts, the agent checks that `TAXCALCBENCH_ROOT` is an unmodified checkout of
the pinned commit, that upstream's modules are loaded from it, that
`LITELLM_LOCAL_MODEL_COST_MAP=True` is set, as upstream sets it, and that the
process reads text as UTF-8, as `run_original.sh` checks on its side.

## Generated Task Structure

```text
datasets/taxcalcbench/<task_id>/
├── task.toml                 # task name, authors, /app/output artifact, separate verifier, agent phase without network
├── instruction.md            # upstream prompt + input files and answer location
├── environment/
│   ├── Dockerfile            # python:3.13-slim (pinned by digest), poppler-utils, pypdf
│   └── input/                # PDFs + remaining_data.json, or input.json
├── solution/
│   ├── solve.sh
│   └── oracle_return.txt
└── tests/
    ├── Dockerfile            # verifier image: python:3.13-slim (pinned by digest), lxml
    ├── test.sh
    ├── score.py
    ├── expected.xml          # upstream output.xml, with upstream's canary strings
    ├── scored_fields.json
    ├── UPSTREAM_LICENSE      # upstream's MIT licence
    └── UPSTREAM_NOTICE.txt   # upstream's evaluation-only notice (CANARY.txt)
```

## Run Evaluation / Harness

From the registry, once the dataset is published (requested ID
`column-tax/taxcalcbench`, tag `v1.0`):

```bash
harbor run -d column-tax/taxcalcbench -a <agent> -m "<model>"
harbor run -t column-tax/taxcalcbench__ty25-ca-004 -a <agent> -m "<model>"
```

From a local checkout, after generating the tasks (next section), from the
repository root:

```bash
# Oracle on the full dataset (expect 101/101)
harbor run -c src/taxcalcbench/run_taxcalcbench.yaml

# One task, any agent
harbor run -p datasets/taxcalcbench/ty25-ca-004 -a <agent> -m "<model>"
```

## Usage: Create Task Directories

```bash
cd src/taxcalcbench

# Clone upstream at the pinned commit and write all 101 tasks
uv run taxcalcbench --output-dir ../../datasets/taxcalcbench

# A 6-case split for a quick check, from an existing checkout
uv run taxcalcbench --taxcalcbench-root /path/to/tax-calc-bench \
  --split sanity --output-dir ../../datasets/taxcalcbench-sanity
```

| Flag | Meaning |
|---|---|
| `--output-dir` | Destination; defaults to `datasets/taxcalcbench` in this repository. While it runs, the generator keeps a hidden `.staging` folder there and removes it at the end |
| `--taxcalcbench-root` | Existing checkout; must be the pinned commit with no local changes outside `results/` and no extra files in the test data |
| `--allow-other-ref` | Skip that check for `--taxcalcbench-root` |
| `--ref` | Ref to clone when no checkout is given; defaults to the pinned commit |
| `--tax-year` | `all` (default), `ty25` or `ty24` |
| `--split` | `full` (default) or `sanity` (six fixed cases for the pre-run check) |
| `--task-ids` | One or more task IDs (`ty25-ca-004`) or upstream case names |
| `--limit` | First N selected tasks |
| `--overwrite` | Regenerate existing task directories; an old task is replaced only when its new version is complete |

## Comparison with Original Benchmark (Parity)

**Parity matched.** All 101 tasks, 5 runs per side: the run ranges overlap on
all four upstream metrics.

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|---|---|---|---|---|---|---|
| taxcalcbench-direct@1.0.0 | openai/gpt-6-luna | Strictly correct returns (%) | 5 | 101 (100%) | 31.29 ± 0.40 | 31.29 ± 0.67 |
| taxcalcbench-direct@1.0.0 | openai/gpt-6-luna | Correct by line (%) | 5 | 101 (100%) | 78.28 ± 0.25 | 78.09 ± 0.21 |
| taxcalcbench-direct@1.0.0 | openai/gpt-6-luna | Lenient correct returns (%) | 5 | 101 (100%) | 36.63 ± 0.44 | 37.23 ± 0.50 |
| taxcalcbench-direct@1.0.0 | openai/gpt-6-luna | Correct by line, lenient (%) | 5 | 101 (100%) | 80.65 ± 0.25 | 80.58 ± 0.12 |

| Metric | Original, runs 1–5 | Harbor, runs 1–5 | Gap (Harbor − original) | Paired test p |
|---|---|---|---|---|
| Strictly correct returns (%) | 30.69, 32.67, 31.68, 30.69, 30.69 | 31.68, 30.69, 33.66, 30.69, 29.70 | 0.00 | 1.00 |
| Correct by line (%) | 78.49, 78.67, 78.59, 77.31, 78.36 | 77.55, 78.30, 78.40, 77.62, 78.57 | −0.20 | 0.71 |
| Lenient correct returns (%) | 35.64, 37.62, 37.62, 35.64, 36.63 | 38.61, 35.64, 36.63, 37.62, 37.62 | +0.59 | 0.82 |
| Correct by line, lenient (%) | 81.01, 81.20, 80.74, 79.76, 80.54 | 80.66, 80.46, 80.18, 80.67, 80.91 | −0.07 | 0.90 |

**The gaps are run-to-run noise.** The gap is the difference between the two
5-run means; the paired test compares each task's 5-run average on the two
sides, and p near 1 means no detectable difference. All four ranges still
overlap with any one run left out. The MSE over the four gaps is 0.10; in 95%
of 10,000 shuffles it is larger. A shuffle randomly splits each task's 10
replies (5 per side) into two groups of 5, so the shuffled MSEs are the gaps
that random assignment of the same replies produces; if the adapter changed the
results, the real MSE would sit above most of them.

**Same request, same scoring.** Prompts and input files are identical for all
101 cases (unit test against the request upstream's own code sends); the
input tokens the API reports are equal for all 505 call pairs;
all 1,010 counted replies, rescored with upstream's evaluator, give 0
disagreements with the stored scores.

**Failure analysis.** No wrong line traces to the adapter: every wrong scored line in the 1,010 replies comes from a number, label or column layout the model wrote.

| Check | Result |
|---|---|
| Wrong line-runs | TY2024: original 646, Harbor 644 of 4,845 (13.3% each). TY2025: original 1,222, Harbor 1,237 of 4,100 (29.8% vs 30.2%) |
| Most-missed lines | The same on both sides; per-line miss counts correlate at r = 0.99 |
| Main root errors (hand-traced samples) | Tax rules on both sides: state credits and tables, EIC and child tax credit, missed Schedule 2/3 items; most other wrong lines are carried down from these |
| Lines scored 0 only for format (amount right; label or column layout not what upstream's parser reads) | Net: original 6, Harbor 10 lines (0.06 points of the by-line gap) |
| 8 cases with the largest gaps | 4 in each direction; together +0.01 points |

| Item | Setting |
|---|---|
| Agent | `taxcalcbench-direct@1.0.0` on Harbor (Daytona, [run_taxcalcbench_parity.yaml](run_taxcalcbench_parity.yaml)); unmodified upstream at the pinned commit on the original side ([run_original.sh](parity/run_original.sh)). One request in flight at a time on each side, as upstream's sequential runner |
| Model | `openai/gpt-6-luna`, thinking level `high`, no web search; set by the Harbor-Index maintainers and listed in upstream's runner. The Harbor side recorded `gpt-6-luna` as the served model for every completed call |
| Endpoint | The Harbor-Index team API, `https://app.ppapi.ai/v1`, on both sides |
| Tasks | All 101 ([full_cases.txt](parity/full_cases.txt)); runs 4–5 on the original side used the same cases in the Harbor side's order ([full_cases_harbor_order.txt](parity/full_cases_harbor_order.txt)) |
| Runs | 5 per side, 2026-11-25 to 2026-11-26 (US Pacific), after a 6-case sanity check on both sides |
| Retries | A failed generation is left unscored and attempted again; the first valid attempt counts. Harbor: 9 of 514 trials invalid (Daytona sandbox build failures, one of them after the reply had arrived, one API stream error, 4 trials cancelled when run 4 stalled on a network outage), all retried. Original: 2 re-attempts |
| Metrics | Upstream's four, as upstream computes them: strictly correct returns (%), correct by line (%), and their lenient versions, which accept a line within $5 of the expected amount |
| Cost | About $7 on the team API: $2.81 for the original side (upstream's LiteLLM estimate, the 6-case sanity check included), about the same for the Harbor side (same requests), $1.32 for the Codex run |
| Standard CLI agent | Codex CLI 0.171.0 with `gpt-6-luna`, Harbor side only, 1 run on Daytona ([run_taxcalcbench_codex.yaml](run_taxcalcbench_codex.yaml)): 35.0% strictly correct returns over 100 of 101 tasks; `ty25-va-001` has no score: in all three attempts Codex wrote its answer, then Daytona could not build the separate verifier sandbox |

**Reproduction.** Both sides read the API key from `OPENAI_API_KEY` and the
endpoint from `OPENAI_BASE_URL` / `OPENAI_API_BASE`, and both use one clean
checkout of the pinned upstream commit (`TAXCALCBENCH_ROOT`). The original-side
script exports the commit into a private temporary folder, without upstream's
saved leaderboard outputs, and runs upstream's CLI there. uv keeps the
environment in `<checkout>/.venv`, built from the checkout's `uv.lock`; no
tracked file of the checkout is written.

From the repository root:

```bash
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=...
export OPENAI_API_BASE="$OPENAI_BASE_URL"
export TAXCALCBENCH_ROOT=/path/to/tax-calc-bench
export LITELLM_LOCAL_MODEL_COST_MAP=True
export DAYTONA_API_KEY=...

# Tasks
cd src/taxcalcbench && uv run taxcalcbench --output-dir ../../datasets/taxcalcbench-full && cd ../..

# One run on each side, started together (RUN=1 ... 5; runs 4-5 on the original side
# used CASES_FILE=src/taxcalcbench/parity/full_cases_harbor_order.txt)
RUN=1 OUT_DIR=parity-experiments/full-original CASES_FILE=src/taxcalcbench/parity/full_cases.txt \
  bash src/taxcalcbench/parity/run_original.sh &
uv run --project src/taxcalcbench --extra parity harbor run \
  -c src/taxcalcbench/run_taxcalcbench_parity.yaml --job-name full-run1
wait
# (after a failed or interrupted case on the original side: the same command with RERUN_FAILED=1)
# (a case collect_parity.py lists as not scored on the Harbor side: the harbor command with
#  -p datasets/taxcalcbench-full -i <task folder> and --job-name full-run1-retry, one -i per task)

# After five runs on each side
python3 src/taxcalcbench/parity/collect_parity.py \
  --cases src/taxcalcbench/parity/full_cases.txt \
  --same-cases src/taxcalcbench/parity/full_cases_harbor_order.txt \
  --original parity-experiments/full-original/run_{1,2,3,4,5} \
  --harbor jobs/full-run1,jobs/full-run1-retry jobs/full-run2 \
           jobs/full-run3,jobs/full-run3-retry,jobs/full-run3-retry2 \
           jobs/full-run4,jobs/full-run4-retry,jobs/full-run4-retry2 jobs/full-run5 \
  --json parity-experiments/summary.json --export-dir parity-experiments/results_collection

# Standard CLI agent (Harbor side only)
uv run --project src/taxcalcbench --extra parity harbor run \
  -c src/taxcalcbench/run_taxcalcbench_codex.yaml --job-name codex-full-run1
```

`collect_parity.py` computes upstream's four metrics with the same arithmetic on
both sides (strictly correct returns, correct by line, and their lenient versions,
where a line within $5 of the expected amount counts) and reports mean ± sample SEM
and the range-overlap check from the adapter guide for each. On the original side it
reads the lenient count from the expected and actual amounts in
`evaluation_result_high_1.md` and checks it against upstream's lenient summary; on the
Harbor side it checks every per-line mark in `score.json` against the line's amounts.
`--same-cases` names a copy of the case list with the same rows in another order; an
original run recorded with it counts as a run of `--cases`, and the report lists each
original run's case-list file (`case_list_files`).

| Exit code | Meaning |
|---|---|
| 0 | Data complete and the run ranges overlap on all four metrics (strictly correct returns, correct by line, and their lenient versions) |
| 1 | Data incomplete. Examples: fewer than two runs on a side or unequal run counts; a case without a valid score; a run or trial without a complete record of its settings; different settings, reasoning effort or package versions within a side or across sides, in any attempt that left a complete request record; request parameters that differ between attempts of a case or do not fit the tax year; a run made with a case list other than `--cases` and the `--same-cases` files, or with `LIMIT`; the same job, trial or run given twice, a trial filed under another job, or a run's job folders given out of order; an attempt of a task (counted or failed) that ran on different task files; a job configuration that disagrees with the request records; two served-model names |
| 3 | Data complete, ranges do not overlap on a metric |

**Which trials count.** A failed generation is left unscored on both sides.

| Side | A case has a valid score when | Otherwise |
|---|---|---|
| Original | The script recorded the case as `ok` (upstream's call ended and both of its files were taken over) and the evaluation file is there | The status is `generation_failed`, `evaluation_missing` or `in_progress`; upstream's message is in `run.log` |
| Harbor | The verifier scored the answer file it found; the agent's request record says the reply arrived (`ok`) with all settings; the collected answer file equals `agent/return.txt` byte for byte | The trial is listed with its error type or the missing piece of evidence |

A Harbor trial with that evidence counts even when a container or network
step after the agent raised an error; the report lists it as a warning. A
case without a valid score can be attempted again with the same settings:
`RERUN_FAILED=1` for the original script, a second Harbor job for those tasks
(passed to the collector as `jobs/full-run1,jobs/full-run1-retry`, the run's own job first; the collector checks the order by the local start time Harbor recorded for each job). The
files of earlier attempts are kept, the report lists every attempt, and an
original-side attempt that was cut off is always run again; what it had saved
stays with that attempt and is never counted.

| `run_original.sh` behaviour | Detail |
|---|---|
| Where upstream runs | A private export of the commit, used by one launcher and removed at the end (a SIGKILL of the script leaves it behind); no tracked file of the checkout is written |
| One launcher per run folder | A second one on the same `RUN` label stops with a message; the lock ends when the launcher's process ends (a helper command running at that moment can hold it until it finishes) |
| Stopping | Ctrl-C, Ctrl-\\, `kill` or a closed terminal during a case stops upstream's processes at once and writes `status.tsv`; during start-up (uv builds the environment on first use) it takes effect when that step ends and nothing has been recorded yet |
| Recorded settings (`run.settings`) | Upstream commit, canonical model and level names, reasoning effort, API base URL, LiteLLM and OpenAI SDK versions, case-list hash, `LIMIT`; a rerun must match all of them |
| Refused starts | A checkout that is not the clean pinned commit or whose `uv.lock` would need a change, a `.env` file upstream would load, a non-UTF-8 shell locale, a malformed case list |

| Link | Status |
|---|---|
| Original benchmark | [column-tax/tax-calc-bench@fdfe0db](https://github.com/column-tax/tax-calc-bench/tree/fdfe0db992ae9033f5b8b942633181a3596da64c) (unmodified; upstream's runner lists `gpt-6-luna`) |
| Adapter PR | [harbor-index/adapters#27](https://github.com/harbor-index/adapters/pull/27) (first copy, closed: [harbor-framework/adapters#126](https://github.com/harbor-framework/adapters/pull/126)) |
| Dataset PR | [harbor-framework/harbor-datasets#318](https://github.com/harbor-framework/harbor-datasets/pull/318) |
| Parity PR (Hugging Face) | [harborframework/parity-experiments#321](https://huggingface.co/datasets/harborframework/parity-experiments/discussions/321); files mirrored at [gigiiii2025/taxcalcbench-parity-experiments@732f335](https://huggingface.co/datasets/gigiiii2025/taxcalcbench-parity-experiments/tree/732f335d671e6ee4c82483e17e47a78540640334/adapters/taxcalcbench) while that repository is over the Hub's file limit |

## Notes & Caveats

### Validation

Run with Harbor 0.27.0 on local Docker, on the task files this revision
generates. Jobs marked "no model" used a local stand-in for the API that
returns the reference answer; they test the plumbing only.

| Check | Result |
|---|---|
| Oracle, all 101 tasks | 101/101 at reward 1.0, 0 errors (local Docker); 2/2 on Daytona |
| `nop` agent (writes nothing), all 101 tasks | 0/101, 0 errors; mean by-line score 37.6% (see Differences from upstream) |
| Verifier hack controls ([attack_agent.py](src/taxcalcbench/attack_agent.py)) on `ty25-ny-002` and `ty24-single-senior-blind-over-65` (the oracle answer scores 1.0 on both): reward file written in advance; answer symlinked to the expected return; scripts planted in `/tests`; empty answer; 6 MiB answer | Reward 0 in all 10 trials |
| Scorer failure (corrupted `scored_fields.json`) | Verifier error, no reward written |
| Network during the agent phase, probed from inside the container | Default: `github.com`, `raw.githubusercontent.com` and `api.openai.com` all blocked. With `--allow-agent-host api.openai.com`: the GitHub hosts blocked, `api.openai.com` reached (HTTP 401 without a key). Oracle passes under the default |
| Codex CLI 0.171.0 under the default (no model) | Installs in the task image; its requests reach the configured model endpoint; Harbor switches its web search off. The stand-in cannot serve Codex, so no answer was produced |
| Parity agent failures in real trials (no model): a failed stream; a trial timeout under the parity config | `UnknownApiError` and `AgentTimeoutError`, both with an invalid reward; each job counts 0 trials |
| Parity pipeline end to end (no model) | Sanity subset 6/6 on each side; dry runs on each side scored every case; `collect_parity.py` exits 0. It exits 1 with a sanity run given as one of the parity runs, with an original run made against another endpoint, and with a run's job folders given out of order. A retry job for one task runs from the documented command; joined to a run that already has that case, the collector keeps the first attempt and lists the retry as ignored. The upstream checkout shows 0 changed paths after all original-side runs |
| Original-side script on the real upstream code (no model) | Stopped by SIGTERM in the middle of a case: the script exits with code 130 about 1 s later, upstream's processes are gone, `status.tsv` shows the case as `in_progress`, and `RERUN_FAILED=1` finishes the run with that case as attempt 2. A second launcher on the same run folder is refused while another `RUN` label runs alongside. The checkout shows 0 changed paths afterwards |
| Unit tests ([tests](tests)) | 203 pass: every instruction is the exact upstream prompt followed by one notes block; the ported scorer equals upstream's `TaxReturnEvaluator` on oracle, empty, off-by-$1 and duplicated-line answers for all 101 cases, its command line writes the right reward for oracle, missing, empty, oversized and symlinked answers, and `test.sh` writes the oracle reward and none when the scorer fails; the parity agent's request equals the one upstream's `run_tax_return_test` sends for all 101 cases (and for two other OpenAI models on the 51 TY2024 cases, and under a model alias); failure mapping, incomplete downloads, cancelled and timed-out trials; the parity config; generation safety and the pinned-checkout check; the original-side launcher on a fake upstream checkout (private export, lock, stop by signal or closed terminal, reruns); the collector's statistics and completeness checks. |

With the real model: the parity runs and the Codex run above (Codex under the
no-network default, on Daytona).

### Parity limits

| Limit | Detail |
|---|---|
| Prompt cache | Only the original side records prompt-cache hits, so cache use cannot be compared between the sides |
| Served model | The original side records no served-model name or response id, so routing cannot be compared per call |
| Another model | Unmodified upstream accepts any OpenAI model name for TY2024. Its TY2025 runner accepts only its own model list, so a model outside that list needs a fork whose only change widens the list; the parity agent and the original-side script then take the fork's commit from `TAXCALCBENCH_EXPECTED_REF`, which is recorded with every run (task generation ignores the variable) |

### Network and public answers

The expected returns are public in the upstream repository, and upstream
counts a web-search run that looked up its test case as incorrect. The tasks
therefore set `network_mode = "no-network"` for the agent phase.

| Situation | What the agent can reach while solving |
|---|---|
| Installed CLI agent whose model endpoint Harbor can resolve (for example Codex with `OPENAI_BASE_URL`) | That endpoint only; Harbor adds it on its own and switches the agent's web tools off |
| Installed agent whose endpoint Harbor cannot resolve | Nothing, with a warning; pass the endpoint with `--allow-agent-host` |
| `--allow-agent-host <host>` given | Exactly the listed hosts; they replace the automatic endpoint, so list the model endpoint too |
| `taxcalcbench-direct` | It calls the API from the host, so the setting does not affect it |

Agent installation runs before the agent phase with normal network access.
The per-phase switch needs an environment backend that supports it: local
Docker (on a host kernel with nftables support; tested with Colima) and
Daytona (single-container tasks such as these) do; a backend without it
rejects the task. The parity and Codex runs used Daytona. The no-network default matches upstream's base setting (no web
search); upstream's separate web-search leaderboard rows have no counterpart
here.

### Known upstream issues (kept as upstream has them)

| Case | Issue upstream documents |
|---|---|
| `ty24-single-senior-blind-over-65` | Missing Form 1095-A data needed for Form 8962 (upstream issue 66) |
| `ty24-mfj-dependent-claimed-2441-exclusion`, `ty24-mfj-w2-box12-codes-a-b-1099int-schedulec`, `ty24-single-senior-blind-over-65` | The input answers "no" to two questions but still lists amounts, and the expected return counts them (upstream issue 161) |
| `ty25-ny-003` | Same pattern for student loan interest |

Four cases are affected in total. The expected returns are unchanged, so
scores stay comparable with upstream.

### Differences from upstream

| Item | Detail |
|---|---|
| Inputs | Upstream attaches the PDFs to the model request. An agent in the container reads them from `/app/input/`; every PDF has a text layer and the image includes poppler-utils and pypdf 6.1.1 |
| Instruction | The appended block is the only addition. The parity agent removes it before the call |
| Missing answer | An agent that writes no answer file gets reward 0. Upstream has no such case: its model call returns text or fails |
| Reward | The strict metric only; lenient and by-line results are in `score.json`. An empty answer already scores 37.6% by line, because upstream reads a missing line as 0 and many expected lines are 0 |
| Scorer guards | The answer is decoded as UTF-8 with replacement; an answer over 5 MiB or a symlink is scored as a wrong answer without being parsed. Upstream's in-memory scorer has no such limits |
| Tax years | One dataset holds both years; upstream's leaderboard is per year. Use `--tax-year` to generate one year |
| Agent time limit | 3,600 s per task. Upstream's TY2025 request timeout is 14,400 s; the parity config gives each trial 15,000 s, counted from the moment its turn to call the model comes |
| Streaming | Upstream's collector accepts a non-empty streamed reply that ends without a completion event. Both sides behave the same; the Harbor side records it in `request_meta.json` and the parity report lists such trials |
| TY2024 calls | Sent without streaming, as upstream. A gateway with a short idle timeout can cut a long call (see "Which trials count") |
| Served model | The Harbor side records the model name the API returned for each call; upstream's saved outputs do not, so the original side has the requested name only |
| Versions | Verifier: lxml 6.1.0, upstream's locked version. Parity extra: LiteLLM 1.100.5 and OpenAI SDK 2.43.0, upstream's locked versions, with Harbor 0.27.0; other host packages (lxml, NumPy, google-genai, python-dotenv) resolve to newer versions than upstream's lock |

### Evaluation only

Upstream asks that TaxCalcBench is not used for model training and that its
notice and canary strings stay in any copy. Each task keeps upstream's
`output.xml` with both canary strings, upstream's notice and its licence on
the verifier side (`tests/`). Files the agent can see contain no canary string
and no benchmark name.

## Installation / Prerequisites

- Python 3.13+ (upstream's code, imported for generation and parity, requires it), uv, Git and Docker.
- Harbor 0.27.0 or later (separate verifier and per-phase network policy).
- A checkout of the pinned upstream commit, or network access so the generator
  can clone it.
- For the parity agent: `uv sync --extra parity` in `src/taxcalcbench`, an API
  key in `OPENAI_API_KEY`, `TAXCALCBENCH_ROOT` and
  `LITELLM_LOCAL_MODEL_COST_MAP=True`.
- For the original side: the same checkout and key; uv builds the checkout's
  environment on first use.
- Tests, from `src/taxcalcbench`: `TAXCALCBENCH_ROOT=/path/to/tax-calc-bench uv run --extra parity --with pytest pytest tests`.

## Troubleshooting

| Symptom | Check |
|---|---|
| "upstream checkout ... expected fdfe0db", "has local changes" or "hidden from git status" | Use an unmodified checkout of the pinned commit, or pass `--allow-other-ref` for generation |
| Parity agent stops before the first trial | `TAXCALCBENCH_ROOT`, `LITELLM_LOCAL_MODEL_COST_MAP=True`, and the `parity` extra |
| `run_original.sh` stops before the first case | Its message names the cause: another launcher on the same run folder, a checkout that is not the clean pinned commit, a `.env` file upstream would load, a non-UTF-8 shell locale, or a reused `RUN` label |
| An agent cannot reach its model or another host it needs | List every needed host with `--allow-agent-host`, including the model endpoint |
| "network policy ... cannot change network policy after start" | The environment backend cannot switch network policy per phase; use local Docker or Daytona |
| Reward 0 with a correct-looking answer | `score.json` shows expected and parsed values per line; the answer must be in `/app/output/return.txt` in upstream's `Line N: ... \| ... \| amount` format |
| Trial counted as an error | `exception.txt` and, for the parity agent, `agent/request_meta.json` |

## Citation

```bibtex
@misc{bock2025taxcalcbench,
  title={TaxCalcBench: Evaluating Frontier Models on the Tax Calculation Task},
  author={Bock, Michael R. and Molisee, Kara and Ozer, Zachary and Shah, Sumit},
  year={2025},
  eprint={2507.16126},
  archivePrefix={arXiv},
  url={https://arxiv.org/abs/2507.16126}
}
```

## Authors & Contributions

This adapter is developed and maintained by Gloria Tan
([gloriaowowo](https://github.com/gloriaowowo)). The benchmark is by Michael R.
Bock, Kara Molisee, Zachary Ozer and Sumit Shah (Column Tax).

**Issues and Contributions:**
- Submit issues and pull requests to this repository and mention `taxcalcbench`.
- Changes to prompts or scoring belong upstream; this adapter follows the pinned upstream commit.
