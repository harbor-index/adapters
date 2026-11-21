# TaxCalcBench → Harbor Adapter

## Overview

This adapter converts all 101 cases from
[TaxCalcBench](https://github.com/column-tax/tax-calc-bench/tree/fdfe0db992ae9033f5b8b942633181a3596da64c)
into Harbor tasks. The source code and data are pinned to
`fdfe0db992ae9033f5b8b942633181a3596da64c`.

| Property | Value |
|---|---|
| Task | Read taxpayer documents and compute one tax return |
| Source | MIT-licensed upstream repository; [paper](https://arxiv.org/abs/2507.16126) |
| Coverage | 50 TY2025 cases and 51 TY2024 cases; exclusions: none |
| Reward | 1 if every scored line is exact; otherwise 0 |
| Runtime used for supplied oracle evidence | Harbor 0.26.0, local Docker, separate verifier |
| Status | Parity runs, dataset publication and artifact upload pending |

## What is TaxCalcBench?

TaxCalcBench evaluates tax calculation from taxpayer records. Each case has
input documents and an expected XML return; upstream defines the prompt,
scored lines, parsing rules and comparisons.

| Cases | Inputs | Return |
|---|---|---|
| TY2025: 50 | W-2/1099 PDFs and `remaining_data.json` | Federal Form 1040, California 540, Illinois IL-1040, New York IT-201 or Virginia 760 |
| TY2024: 51 | `input.json` | Federal Form 1040 |

Strict correctness requires every scored line to match exactly. Lenient
correctness allows an absolute difference of at most $5 per line. By-line
scores are the fraction of scored lines that pass each comparison. These are
benchmark grading rules from the pinned upstream evaluator.

## Adapter Features

| Component | Implementation |
|---|---|
| Generation | [adapter.py](src/taxcalcbench/adapter.py) imports upstream prompt builders and scoring definitions; copies inputs into `/app/input/` |
| Instructions | Exact upstream case prompt followed by input paths and the submission path `/app/output/return.txt` |
| Verifier | [score.py](src/taxcalcbench/task-template/tests/score.py) vendors upstream parsing/comparison logic; `tests/expected.xml` and `scored_fields.json` supply the reference data |
| Isolation | Separate verifier container receives the collected `/app/output` artifact; grading code and expected XML are in the verifier image |
| Output | `/logs/verifier/reward.txt` holds strict reward; `score.json` includes strict, lenient and by-line results |
| Oracle | Renders every scored line from the case's `output.xml` in upstream's output format |
| Task IDs | `column-tax/taxcalcbench__<task_id>`; TY2025 uses its source ID, TY2024 prefixes its source name with `ty24-` |

The adapter ships **`taxcalcbench-direct@1.0.0`** in
[parity_agent.py](src/taxcalcbench/parity_agent.py), a Harbor port of upstream's
single generation call. It runs on the host and writes the response into the
task container. The parity configuration imports the class directly.

| Request component | Matched behavior |
|---|---|
| Prompt | Removes the appended Harbor notes before sending the upstream prompt |
| TY2025 input | One user message: prompt plus filename-sorted PDFs as base64 file parts |
| TY2024 input | Prompt string with `json.dumps` of `input.json`, as upstream's `run_tax_return_test` builds it |
| API | LiteLLM `responses`, OpenAI provider; `openai/gpt-6.2-sol`, explicit `high` reasoning effort |
| Tools and sampling | No web-search tool; no explicit temperature or output-token cap, matching this upstream path |
| Streaming and timeout | TY2025: streaming, 14,400 s request timeout; TY2024 at `high`: neither argument explicitly set |
| Response handling | Imports upstream's stream collector, text extractor and model registration helpers |
| Retries | No retry loop in the adapter; raised API exceptions propagate |

A unit test checks that this request equals the one upstream's code sends, for all 101 cases.

## Generated Task Structure

```text
datasets/taxcalcbench/<task_id>/
├── task.toml                 # task name, authors, output artifact, verifier mode
├── instruction.md            # upstream prompt + Harbor task notes
├── environment/
│   ├── Dockerfile            # Python, poppler-utils, pypdf
│   └── input/                # PDFs + remaining_data.json, or input.json
├── solution/
│   ├── solve.sh
│   └── oracle_return.txt
└── tests/
    ├── Dockerfile            # separate verifier image, lxml
    ├── test.sh
    ├── score.py
    ├── expected.xml
    └── scored_fields.json
```

## Run Evaluation / Harness

Registry commands come first; publication and the confirmed dataset ID are
**TBD**. After publication, replace the placeholder with the published ID:

```bash
harbor run -d "<published-dataset-id>" -a oracle
harbor run -d "<published-dataset-id>@parity" -a <agent> -m "<model>"
harbor run -t "column-tax/taxcalcbench__ty25-ca-004" -a <agent> -m "<model>"
```

For local evaluation, generate tasks first and run from the repository root:

```bash
uv run --project src/taxcalcbench --extra parity \
  harbor run -c src/taxcalcbench/taxcalcbench.yaml
uv run --project src/taxcalcbench --extra parity \
  harbor run -p datasets/taxcalcbench/ty25-ca-004 -a oracle
```

Apply the network configuration under Notes & Caveats before evaluating an
agent that can access the internet.

## Usage: Create Task Directories

```bash
cd src/taxcalcbench
uv run taxcalcbench --output-dir ../../datasets/taxcalcbench
uv run taxcalcbench --taxcalcbench-root /path/to/pinned/tax-calc-bench \
  --split parity --output-dir ../../datasets/taxcalcbench-parity
```

| Flag | Meaning |
|---|---|
| `--output-dir` | Destination; defaults to this repository's `datasets/taxcalcbench` |
| `--taxcalcbench-root` | Existing checkout at the pinned commit (checked); skips cloning |
| `--allow-other-ref` | Allow `--taxcalcbench-root` at another commit |
| `--ref` | Ref used when cloning; defaults to the full commit above |
| `--tax-year` | `all` (default), `ty25` or `ty24` |
| `--split` | `full` (default) or deterministic `parity` subset |
| `--task-ids` | Generated task IDs or upstream case names |
| `--limit` | First N selected tasks |
| `--overwrite` | Regenerate existing selected task directories |

## Comparison with Original Benchmark (Parity)

Real parity runs are pending. The plan is three runs per side using
`taxcalcbench-direct` and `openai/gpt-6.2-sol` at `high`, plus one Harbor-only
Codex run on the same 20 cases (Codex version: **TBD**).

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|---|---|---|---|---|---|---|
| taxcalcbench-direct@1.0.0 | openai/gpt-6.2-sol | Strictly correct returns (%) | 3 planned; 0 completed | 20 of 101 | TBD (runs pending) | TBD (runs pending) |
| taxcalcbench-direct@1.0.0 | openai/gpt-6.2-sol | Correct by line (%) | 3 planned; 0 completed | 20 of 101 | TBD (runs pending) | TBD (runs pending) |

**Subset rule.** [parity_subset](src/taxcalcbench/adapter.py) uses one
`random.Random(42)` instance. It samples two sorted TY2025 case names in each
jurisdiction, in the order `us`, `ca`, `il`, `ny`, `va`, then ten sorted TY2024
case names. It returns the selection in discovery order. The fixed 20 IDs are
in [parity_cases.txt](parity/parity_cases.txt); `--split parity` generates them.
Use the default `--tax-year all` for this comparison.

**Reproduction.** Use separate clean upstream checkouts at the pinned commit;
the original script writes into upstream's results tree. Install from each
project's lockfile, export `OPENAI_API_KEY`, and use the same API endpoint on
both sides (`OPENAI_BASE_URL` and `OPENAI_API_BASE` if required). Resolve the
model snapshot and Codex version (**TBD**) before running. Preserve every
attempt and its settings. Complete a matching 5–10-case sanity check on both
sides, then one full 20-case run on both sides, then runs 2 and 3.

From the adapter repository root, after generating the subset:

```bash
# Original side: one full run, using unmodified upstream at the pinned commit.
TAXCALCBENCH_ROOT=/path/to/clean/pinned/tax-calc-bench \
  MODEL=gpt-6.2-sol THINKING_LEVEL=high LIMIT=0 \
  CASES_FILE="$PWD/src/taxcalcbench/parity/parity_cases.txt" \
  RUN=1 OUT_DIR="$PWD/parity_runs/original" \
  bash src/taxcalcbench/parity/run_original.sh

# Harbor side: the matching full run.
TAXCALCBENCH_ROOT=/path/to/pinned/tax-calc-bench \
  uv run --project src/taxcalcbench --extra parity harbor run \
  -c src/taxcalcbench/taxcalcbench-parity.yaml --job-name parity-run1

# Repeat with RUN=2/3 and the same OUT_DIR; use parity-run2/3 for Harbor.
# After both sides complete all three runs:
python3 src/taxcalcbench/parity/collect_parity.py \
  --original parity_runs/original \
  --harbor jobs/parity-run1 jobs/parity-run2 jobs/parity-run3 \
  --json parity_runs/summary.json
```

Report mean ± sample SEM from the raw run arrays. Matching requires overlapping
run-score ranges on each metric. The collector reports scored counts; check
that each side has all 20 cases in each of three runs and audit errors before
accepting its summary. Costs, dates, measured scores and artifacts: **TBD**.

| Link | Status |
|---|---|
| Original code and data | [Pinned upstream](https://github.com/column-tax/tax-calc-bench/tree/fdfe0db992ae9033f5b8b942633181a3596da64c); reproduction uses this checkout |
| Adapter PR | [harbor-index/adapters#27](https://github.com/harbor-index/adapters/pull/27) |
| Dataset PR / registry publication | TBD |
| Hugging Face parity/oracle upload | TBD |

## Notes & Caveats

### Validation

Run with Harbor 0.26.0 and local Docker on the adapter revision that adds this README.

| Check | Result |
|---|---|
| Oracle, all 101 tasks | 101/101 at reward 1.0, 0 errors |
| `nop` agent (no answer), all 101 tasks | 0/101 (reward 0 on every task, 0 errors). The mean by-line score of an empty answer is 37.6%, because upstream reads a missing line as 0 and many expected lines are 0; the by-line metric therefore starts near 38%, not 0 |
| Verifier hack controls ([attack_agent.py](src/taxcalcbench/attack_agent.py)): reward file pre-written, answer symlinked to the expected return, `/tests` scripts planted, empty answer | reward 0 in all 8 trials (one TY2025 and one TY2024 task per attack) |
| Scorer failure (corrupted `scored_fields.json`) | recorded as a verifier error; no reward written |
| Unit tests ([tests](tests)) | 5 pass: prompts equal upstream's; the vendored scorer equals upstream `TaxReturnEvaluator` on oracle, empty, off-by-$1 and duplicated-line answers for all 101 cases; the parity agent's request equals the request upstream's `run_tax_return_test` sends for all 101 cases |
| Both parity sides end to end against a local stand-in for the API | original 20/20, Harbor 20/20 on the parity subset |

For reference, upstream's own saved `gpt-6.2-sol` / `high` outputs score 50% strict and 87.31% by line on the 10 TY2025 parity cases.

### Network and public answers

The expected returns are public on GitHub. Upstream marks web-search runs that looked up a test case as incorrect. The task template keeps Harbor's default public network, so for any agent with internet access, restrict the agent phase in each generated `task.toml`:

```toml
[agent]
network_mode = "allowlist"
allowed_hosts = ["api.openai.com"]   # the model API host(s) the agent needs
```

Tested with a probe agent inside the task container:

| `network_mode` | github.com / raw.githubusercontent.com | api.openai.com | Oracle |
|---|---|---|---|
| default (`public`) | reachable | reachable | 2/2 |
| `no-network` | blocked | blocked | 2/2 |
| `allowlist` (`api.openai.com`) | blocked | reachable | 2/2 |

`taxcalcbench-direct` calls the API from the host and has no search tool, so it is unaffected by these settings.

### Differences from upstream and other details

| Item | Detail |
|---|---|
| Instruction | The upstream prompt, then a "Harbor task notes" block with the input paths and `/app/output/return.txt`. The parity agent removes the block, so the model receives the upstream prompt unchanged |
| Inputs | Upstream attaches the PDFs to the API request; CLI agents read them from `/app/input/` (the image includes poppler-utils and pypdf 6.1.1) |
| Scoring | Unchanged: missing or unparseable amounts count as 0; a TY2025 label that appears more than once counts as 0; TY2024 takes the first matching line |
| Failures | A failed API call in the parity agent raises, and a scorer failure exits non-zero; both are recorded as errors, never as reward 0. A missing answer file is a valid reward 0 |
| Streaming | Upstream's own stream collector is used on both sides; it accepts a non-empty reply that ends without a completion event, on both sides alike |
| Versions | Verifier: Python 3.13-slim, lxml 6.1.0 (upstream's locked version). Parity extra: Harbor 0.26.0, LiteLLM 1.100.5 and OpenAI 2.43.0, as in upstream's lockfile |
| Timeouts | Agent 1,800 s; verifier 120 s; the parity config raises the agent limit to 15,000 s above upstream's 14,400 s TY2025 request timeout |
| Repeated runs | `run_original.sh` refuses to reuse a run folder and checks that the upstream checkout is at the pinned commit |
| Canary | Upstream canary strings stay in the expected XML on the verifier side |
| Exclusions | None |

## Installation / Prerequisites

- Python 3.12+, uv, Git and Docker for task execution.
- A checkout of the pinned upstream commit, or network access for the generator
  to clone it.
- From `src/taxcalcbench`, use `uv sync --extra parity` to install the locked
  adapter and Harbor 0.26.0 dependencies. The `parity` extra supplies the custom
  agent's API dependencies.
- Real model runs need the relevant API credentials. The direct agent reads
  them on the host and requires `TAXCALCBENCH_ROOT` to identify upstream code.

## Troubleshooting

| Symptom | Check |
|---|---|
| Upstream import or missing-data error | Check `--taxcalcbench-root` / `TAXCALCBENCH_ROOT` and confirm the checkout is the pinned commit |
| Parity agent import fails | Launch Harbor with `uv run --project src/taxcalcbench --extra parity` |
| Reward 0 | Inspect verifier logs and `score.json`; confirm `/app/output/return.txt` exists and uses upstream's line format |
| Missing parity result | Inspect API/stream and trial errors; keep infrastructure failures separate from valid scored outcomes |
| Registry dataset cannot be found | Publication is pending; use generated local task paths |

## Citation

Michael R. Bock, Kara Molisee, Zachary Ozer and Sumit Shah.
*TaxCalcBench: Evaluating Frontier Models on the Tax Calculation Task*. [arXiv:2507.16126](https://arxiv.org/abs/2507.16126).

```bibtex
@misc{taxcalcbench,
  title={TaxCalcBench: Evaluating Frontier Models on the Tax Calculation Task},
  author={Bock, Michael R. and Molisee, Kara and Ozer, Zachary and Shah, Sumit},
  eprint={2507.16126},
  archivePrefix={arXiv},
  url={https://arxiv.org/abs/2507.16126}
}
```

## Authors & Contributions

- **Adapter builder:** Gloria Tan ([gloriaowowo](https://github.com/gloriaowowo)).
- **Benchmark:** Michael R. Bock, Kara Molisee, Zachary Ozer and Sumit Shah.
