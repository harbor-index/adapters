# Blocksworld Hard → Harbor

## Overview

The released 110-instance PDDL planning dataset from *A Systematic Evaluation of the Planning and Scheduling Abilities of the Reasoning Model o1*. This is not the 600-instance natural-language Blocksworld dataset.

Source: [https://github.com/karthikv792/LLMs-Planning](https://github.com/karthikv792/LLMs-Planning), pinned at `fc638a1aff7df3fe7a1a1d289fa2c04cc24dc284`. License: MIT.
The adapter generates all 110 released test tasks with stable IDs. It adds a file-output instruction for terminal agents and runs grading in a separate container.

## What is Blocksworld Hard?

The prompt is reconstructed from `per_instance_output_pddl` in `llm_planning_analysis/back_prompting_parallel.py`. The domain and all 110 problem files are unchanged. Reference plans come from `instances/blocksworld_hard/info.json`. The verifier builds the upstream bundled VAL and uses its `Plan valid` criterion, accepting any valid plan rather than requiring the reference plan.

## Adapter Features

- Required CLI flags: `--output-dir`, `--limit`, `--overwrite`, and `--task-ids`.
- `--source-dir` accepts a local checkout at the pinned revision; otherwise the adapter checks out that revision in a temporary directory.
- `--split parity` selects every tenth source instance (11 tasks), preserving upstream order.
- Agent images contain no hidden labels or solutions. Harbor collects only `/app/plan.txt` for the independent verifier.
- Oracle scripts copy the generated reference answer into the same output file agents use.

## Generated Task Structure

```text
blocksworld-hard/
  <task-id>/
    instruction.md
    task.toml
    environment/Dockerfile
    solution/solve.sh
    solution/plan.txt
    tests/Dockerfile
    tests/test.sh
    tests/verify.py
```

Tests also contain the trusted upstream problem data.

## Run Evaluation / Harness

After the Harbor team publishes the dataset, the registry command will be:

```bash
harbor run -d planbench/blocksworld-hard -a oracle
```

This dataset is not yet published. During development, run from the adapters repository root:

```bash
harbor run -p datasets/blocksworld-hard -a oracle
harbor run -p datasets/blocksworld-hard -a nop
```

From this adapter directory, `harbor run -c run_blocksworld-hard.yaml` runs the local oracle dataset.

## Usage: Create Task Directories

```bash
cd src/blocksworld-hard
uv sync --locked
uv run blocksworld-hard --output-dir ../../datasets/blocksworld-hard
uv run blocksworld-hard --split parity --output-dir ../../datasets/blocksworld-hard-parity
```

Use `--source-dir /path/to/upstream` to avoid a download, `--limit 5` for a smoke test, and `--task-ids <id> ...` for particular tasks. Existing task directories are only replaced with `--overwrite`.

## Comparison with Original Benchmark (Parity)

[Original-side preparation and scoring](https://github.com/Chenghao-Zhang/LLMs-Planning/tree/82256f992bf0e426719a2fb14e39090e872effc7/harbor_parity) are published with reproduction commands. They read the upstream PDDL inputs and use VAL directly, without importing the adapter. The model runner remains pending.

Parity has not run. `parity_experiment.json` is intentionally empty; this is not a completed parity claim.
The proposed comparison uses the same pinned CLI agent, dated model, prompt, limits, and container environment on both sides, with upstream grading on the original side. Proposed configuration for team review: `codex@0.118.0`, `gpt-5-mini-2025-08-07`, three runs per side, the same reasoning effort and inference settings, and a 1,800-second task timeout. This follows the matched CLI-agent approach used in the approved CanItEdit adapter. These choices and API provisioning are not yet confirmed; no paid run has started.

The proposed subset has 11 tasks. Run paired sanity checks first, then one full paired subset run, then scale symmetrically to three runs per side. Record raw scores and mean ± sample SEM, preserving all attempts. Original-side runner and final reproduction commands remain to be added before submission as ready for review.

## Notes & Caveats

- No test tasks are excluded.
- The added instruction is `Save your answer to /app/plan.txt.` Apply it identically on the original side during parity.
- This adapter exposes the initial zero-shot PDDL planning task to a terminal agent. It does not reproduce the upstream multi-round LLM-Modulo feedback loop. Parity must compare the same terminal agent on both sides, not claim equality with historical model-call or backprompting results.
- Reference solutions and all grading dependencies are unavailable during ordinary agent execution.
- Harbor oracle: 110/110 tasks pass with zero exceptions. No-op controls: 3/3 return zero. Five additional VAL positive/negative controls pass. Independent original-side preparation matches all 110 prompts, domains, and problems exactly; its VAL scorer passes all 110 reference plans and rejects all 110 missing answers. Raw job summaries and the input audit are under `validation/`. Parity and dataset publication remain pending.

## Installation / Prerequisites

Python 3.12+, `uv`, Git, Docker with Compose, and Harbor 0.27.0 (the development runtime). Run `uv tool install harbor` if needed. Docker builds require network access to install dependencies and compile the upstream VAL sources.

## Troubleshooting

- A revision mismatch means `--source-dir` is not checked out at the revision above.
- An existing output task requires `--overwrite` or a fresh output directory.
- A missing answer receives reward zero. Build failures and verifier crashes must be investigated as infrastructure errors.
- Inspect `jobs/<job>/<trial>/verifier/` and the trial exception when a run fails.

## Citation

See the upstream repository for the original BibTeX citation. Valmeekam, Stechly, Gundawar, and Kambhampati. A Systematic Evaluation of the Planning and Scheduling Abilities of the Reasoning Model o1. TMLR, 2025.

```bibtex
@article{valmeekam2025a,
title={A Systematic Evaluation of the Planning and Scheduling Abilities of the Reasoning Model o1},
author={Karthik Valmeekam and Kaya Stechly and Atharva Gundawar and Subbarao Kambhampati},
journal={Transactions on Machine Learning Research},
issn={2835-8856},
year={2025},
url={https://openreview.net/forum?id=FkKBxp0FhR},
note={}
}
```

## Authors & Contributions

Original benchmark authors are credited in each generated `task.toml`.
Harbor adaptation: Chenghao Zhang.
