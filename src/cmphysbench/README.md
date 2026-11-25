## CMPhysBench → Harbor Adapter

## Overview

CMPhysBench tests graduate-level condensed matter physics: magnetism, superconductivity, strongly correlated systems, semiconductors and theoretical foundations. Each problem asks for a calculation whose result is a LaTeX expression. This adapter turns the **96 publicly released CMPhysBench problems that have reference answers and can be scored** into 96 Harbor tasks, one per problem.

- Source: [Hugging Face `weidawang/CMPhysBench`](https://huggingface.co/datasets/weidawang/CMPhysBench) at revision `43d185851f731e23aa5737c3667b3a9e87bf8cd1`. Scorer from [CMPhysBench/CMPhysBench](https://github.com/CMPhysBench/CMPhysBench) at commit `b2cd8571279450f0861759f47d98e9fc577aa993`. Both are Apache-2.0.
- Task count: 96 = 100 problems with public answers − 2 unscorable − 2 with the answer in the question.
  - The dataset has 520 problems, but only 100 have a non-empty `final_answer`; the other 420 cannot be graded.
  - Two of the 100 (ids 55 and 74) have reference answers that contain `\int` or `\sum`. Upstream SEED always scores those 0, even for the reference answer, so they are excluded.
  - Two more (ids 56 and 89) state the reference answer verbatim in the question, so they are excluded too.
- Main adaptation: upstream's system prompt and user message are kept word for word. One sentence is added on both sides asking the agent to write its response to `/app/answer.txt`.

## What is CMPhysBench?

CMPhysBench ([Wang et al., arXiv:2508.18124](https://arxiv.org/abs/2508.18124), ICLR 2026) is a set of calculation problems curated from standard condensed matter textbooks. Models answer in a `\boxed{}` LaTeX expression. Answers are scored with **SEED** (Scalable Expression Edit Distance), which extends PHYBench's EED to several answer types (all 96 adapted problems are of type `Expression`):

- **SEED score** (0–100): both expressions are converted to SymPy and simplified, then compared with a tree edit distance. The score is 100 for an equivalent expression, with partial credit for close expressions.
- **Accuracy**: the fraction of problems with SEED score 100.

The paper reports that the best model reaches less than 30% accuracy.

## Adapter Features

- Downloads the pinned dataset revision and the pinned upstream `SEED` package (`__init__.py`, `SEED.py`, `extended_zss.py`, `latex_pre_process.py`). The package is copied verbatim into each task's `tests/SEED/`. `--data-file` and `--seed-repo-dir` accept local copies for offline generation.
- Grading follows upstream `evaluation.py`. `tests/cmphysbench_answer.py` takes the **first** `\boxed{...}` in `/app/answer.txt` (upstream `extract_boxed_latex`; no box means an empty answer) and calls `SEED(reference, answer, answer_type)`. An exception scores 0 (upstream `score_one_safe`). The parity fork imports this same file.
- Rewards are written to `/logs/verifier/reward.json` (with the primary reward also in `reward.txt`): `reward = seed_score / 100`, `seed_score` (0–100) and `accuracy` (0 or 1, using the upstream `|s − 100| < 1e-4` rule).
- Grading is isolated. `[verifier] environment_mode = "separate"` and `artifacts = ["/app/answer.txt"]`: only the answer file reaches a fresh verifier container. The agent never sees `tests/` or the reference answer. `test.sh` deletes any old reward file and runs `/usr/local/bin/python3 -I`. If scoring itself crashes, it writes no reward and the trial is reported as a verifier failure.
- Agent-visible files do not name the benchmark and do not contain source ids, URLs or answers.
- Task names are stable: `cmphysbench/cmphysbench-<source id>`.

## Generated Task Structure

```
cmphysbench/
├── cmphysbench-1/
│   ├── task.toml                 # name, authors, topic, timeouts, separate verifier, artifacts
│   ├── instruction.md            # upstream system prompt + question + symbol list + answer-file sentence
│   ├── environment/
│   │   └── Dockerfile            # python:3.11-slim + pinned sympy 1.14.0, latex2sympy2_extended 1.10.2, pint 0.25, numpy, timeout-decorator
│   ├── solution/
│   │   └── solve.sh              # writes \boxed{reference answer} to /app/answer.txt
│   └── tests/
│       ├── test.sh
│       ├── score.py
│       ├── cmphysbench_answer.py # extraction + SEED call (shared with the parity fork)
│       ├── ground_truth.json     # reference answer and answer type
│       └── SEED/                 # upstream SEED package, verbatim, plus its LICENSE
└── ...
```

Adapter code:

```
src/cmphysbench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_cmphysbench.yaml          # oracle (default) and commented agent blocks
├── cmphysbench_parity_codex.yaml # parity configuration
└── src/cmphysbench/
    ├── __init__.py
    ├── adapter.py
    ├── main.py
    └── task-template/
```

## Run Evaluation / Harness

Run from the registry once the dataset is published:

```bash
harbor run -d cmphysbench/cmphysbench                         # oracle
harbor run -d cmphysbench/cmphysbench -a codex -m "MODEL"     # an agent and model
```

Run from locally generated tasks:

```bash
harbor run -c src/cmphysbench/run_cmphysbench.yaml            # oracle on all 96 tasks
harbor run -c src/cmphysbench/cmphysbench_parity_codex.yaml   # parity configuration
harbor trial start -p datasets/cmphysbench/cmphysbench-1      # single task
```

## Usage: Create Task Directories

```bash
cd src/cmphysbench
uv run cmphysbench --output-dir ../../datasets/cmphysbench
```

Flags: `--output-dir`, `--limit N`, `--overwrite`, `--task-ids` (e.g. `--task-ids 7 cmphysbench-12`), `--data-file`, `--seed-repo-dir`.

## Comparison with Original Benchmark (Parity)

Upstream's harness (`evaluation.py`) runs a local vLLM model with the system prompt, extracts the first boxed answer, and scores it with SEED. For parity both sides run the same CLI agent. The original side is a fork of the upstream repository ([`dreiw2580/CMPhysBench`, branch `harbor-parity`](https://github.com/dreiw2580/CMPhysBench/tree/harbor-parity)). It builds the prompt as `evaluation.py` does, runs `codex exec` with the same pinned version, model, flags and instruction in a container from the same image, and scores with the fork's own `SEED` package through the same `cmphysbench_answer.py`.

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|
| codex@TODO_VERSION | TODO_MODEL | SEED score | 3 | 96 (100%) | TODO | TODO |
| codex@TODO_VERSION | TODO_MODEL | Accuracy (%) | 3 | 96 (100%) | TODO | TODO |

Values are mean ± sample SEM over runs; raw run scores are in `parity_experiment.json`. Oracle: 96/96 tasks at reward 1.0, 0 exceptions (Harbor 0.27.0, Docker).

Reproduce the original side (in the fork):

```bash
cd harbor_parity
docker build -f Dockerfile.agent --build-arg CODEX_VERSION=TODO_VERSION -t cmphysbench-codex:TODO_VERSION .
export OPENAI_API_KEY=... OPENAI_BASE_URL=...
for r in run1 run2 run3; do
  python3 run_codex_original.py --model TODO_MODEL --codex-version TODO_VERSION --run-name $r --workers 8
done
```

Reproduce the Harbor side:

```bash
cd src/cmphysbench && uv run cmphysbench --output-dir ../../datasets/cmphysbench && cd ../..
harbor run -c src/cmphysbench/cmphysbench_parity_codex.yaml   # repeat for 3 runs
```

Links: [adapter PR](https://github.com/harbor-index/adapters/pull/45), [dataset PR](https://github.com/laude-institute/harbor-datasets/pull/TODO), [parity artifacts](https://huggingface.co/datasets/harborframework/parity-experiments/discussions/TODO).

## Notes & Caveats

- **Subset.** 96 of 520 problems.
  - 420 have no public reference answer.
  - Ids 55 and 74 are excluded because their reference answers contain `\int` or `\sum`, which upstream SEED scores as 0 by design, so not even the reference answer can pass.
  - Ids 56 and 89 are excluded because the question text contains the reference answer verbatim (it defines the expression it asks for). Id 85 also matches a substring check (`\theta_{-}` appears in the question), but only as a symbol, so it is kept.
- **Prompt.** Upstream's system prompt, and the user message `question + "\nHere are the relevant symbols:\n" + symbol`, are used verbatim as one instruction. Upstream also prepends an optional `context` field; no record in the pinned data has one. Two sentences are added on both sides: write the response to `/app/answer.txt`, and only the first `\boxed{}` is graded. Upstream's single vLLM call (temperature 0.6, top-p 0.95, max 16384 tokens) is replaced by a CLI agent on both sides.
- **Answer extraction.** Upstream takes the first `\boxed{` occurrence and matches braces without handling escapes. This is kept unchanged, so a response with an earlier draft box is graded on that first box.
- **Oracle.** `solve.sh` writes `\boxed{<reference>}`. Extraction returns the reference string exactly, and upstream SEED returns 100 through its exact-match check. Checked end to end on all 96 tasks: each `solve.sh`, then `test.sh` in a fresh container, gives reward 1.0 (`tools/docker_oracle.sh`, 2026-11-23).
- **Scorer exceptions.** An exception inside SEED scores 0, as in upstream `score_one_safe`, on both sides.
- **Parity infrastructure.** Harbor side: Daytona sandbox, with codex installed by Harbor. Original side: local Docker, with the same codex version preinstalled. Agent flags, instruction, image packages and scorer are identical.
- **Scorer behaviour inherited from upstream.** SymPy simplification has a time limit (`timeout_decorator`, Linux only). Physical units are handled with `pint`.

## Installation / Prerequisites

- Docker (or a Daytona or Modal account for cloud runs)
- Harbor: `uv tool install harbor`
- Adapter dependencies (standard library only): `cd src/cmphysbench && uv sync`
- For parity: an OpenAI-compatible API key and base URL for the chosen model

## Troubleshooting

- `Failed to download ...`: Hugging Face or GitHub is unreachable. Pass local copies with `--data-file` and `--seed-repo-dir`.
- A trial ends with no reward: scoring crashed (this is not a wrong answer). Read `verifier/test-stdout.txt` in the trial directory.

## Citation

```bibtex
@article{wang2025cmphysbench,
  title={CMPhysBench: A Benchmark for Evaluating Large Language Models in Condensed Matter Physics},
  author={Wang, Weida and Huang, Dongchen and Li, Jiatong and Yang, Tengchao and Zheng, Ziyang and Zhang, Di and Han, Dong and Chen, Benteng and Luo, Binzhao and Liu, Zhiyu and others},
  journal={arXiv preprint arXiv:2508.18124},
  year={2025}
}
```

## Authors & Contributions

This adapter is developed and maintained by [dreiw2580](https://github.com/dreiw2580).

**Issues and Contributions:**
- Submit issues and pull requests to [harbor-index/adapters](https://github.com/harbor-index/adapters).

## Acknowledgement

> API inference compute for running parity tests is generously supported by [2077AI](https://www.2077ai.com/) (https://www.2077ai.com/).
