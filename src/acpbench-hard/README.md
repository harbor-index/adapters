# ACPBench-Hard → Harbor

> Working documentation draft. The contributor must author the final README before review.

## Overview

The generative test split of ACPBench-Hard: eight reasoning categories, each containing 130 questions across thirteen planning domains (1,040 total). Boolean and multiple-choice ACPBench questions and development examples are not evaluation tasks.

Source: [https://github.com/IBM/ACPBench](https://github.com/IBM/ACPBench), pinned at `c93f61c31efb63fa662bcc24d27c15556d319710`. License: CDLA-Permissive-2.0.
The adapter generates all 1040 released test tasks with stable IDs. It adds a file-output instruction for terminal agents and runs grading in a separate container.

## What is ACPBench-Hard?

Prompts use the upstream `configs/tasks/acp_gen_chat` templates and their first two few-shot examples. The upstream generative parser, evaluator classes, grammar, and planning utilities are copied verbatim under `tests/upstream/`. Each task scores one answer with the original evaluator; overall accuracy is the mean of task rewards. Reference answers are rendered from annotations; justification answers remove the annotated single action occurrence or consecutive pair from the original plan.

## Adapter Features

- Required CLI flags: `--output-dir`, `--limit`, `--overwrite`, and `--task-ids`.
- `--source-dir` accepts a local checkout at the pinned revision; otherwise the adapter checks out that revision in a temporary directory.
- `--split parity` selects every tenth source instance (104 tasks), preserving upstream order within every reasoning category.
- Agent images contain no hidden labels or solutions. Harbor collects only `/app/answer.txt` for the independent verifier.
- Oracle scripts copy the generated reference answer into the same output file agents use.

## Generated Task Structure

```text
acpbench-hard/
  <task-id>/
    instruction.md
    task.toml
    environment/Dockerfile
    solution/solve.sh
    solution/answer.txt
    tests/Dockerfile
    tests/test.sh
    tests/verify.py
```

Tests also contain the trusted upstream problem data and evaluator source.

## Run Evaluation / Harness

After the Harbor team publishes the dataset, the registry command will be:

```bash
harbor run -d ibm/acpbench-hard -a oracle
```

This dataset is not yet published. During development, run from the adapters repository root:

```bash
harbor run -p datasets/acpbench-hard -a oracle
harbor run -p datasets/acpbench-hard -a nop
```

From this adapter directory, `harbor run -c run_acpbench-hard.yaml` runs the local oracle dataset.

## Usage: Create Task Directories

```bash
cd src/acpbench-hard
uv sync --locked
uv run acpbench-hard --output-dir ../../datasets/acpbench-hard
uv run acpbench-hard --split parity --output-dir ../../datasets/acpbench-hard-parity
```

Use `--source-dir /path/to/upstream` to avoid a download, `--limit 5` for a smoke test, and `--task-ids <id> ...` for particular tasks. Existing task directories are only replaced with `--overwrite`.

## Comparison with Original Benchmark (Parity)

Formal parity has not run; exploratory pilots are described below. `parity_experiment.json` is intentionally empty; this is not a completed parity claim.

Adapter PR: https://github.com/harbor-index/adapters/pull/55. Generated dataset draft: https://github.com/harbor-framework/harbor-datasets/pull/317. Dataset publication and parity upload remain pending.

Prepare the original-side cohort without model calls:

```bash
git clone https://github.com/Chenghao-Zhang/ACPBench.git acpbench-parity
cd acpbench-parity
git checkout 10fe9de53eb9fcdaa80350ff98ece757c3307c9e
uv run --python 3.12 --with jinja2==3.1.6 --with PyYAML==6.0.2 python harbor_parity/prepare.py --split parity --output-dir inputs
```

Use the fork's `harbor_parity/README.md` for container build, runner arguments, and scoring commands. Supply the agreed settings explicitly before executing model runs.

[Original-side preparation and scoring](https://github.com/Chenghao-Zhang/ACPBench/tree/10fe9de53eb9fcdaa80350ff98ece757c3307c9e/harbor_parity) are published with reproduction commands. They read the pinned upstream data and scorer directly, without importing the adapter. An isolated Codex runner is included and has passed offline orchestration controls. The HTTP Responses connection has been exercised with paired exploratory pilots; formal parity remains pending.
The proposed comparison uses the same pinned CLI agent, exact provider model ID, prompt, limits, and container environment on both sides, with upstream grading on the original side. Proposed configuration for team review: `codex@0.118.0`, `gpt-6-luna`, three runs per side, the same reasoning effort and inference settings, and a 1,800-second task timeout. This follows the matched CLI-agent approach used in the approved CanItEdit adapter. The contributor supplies API access; formal experiment settings still await team confirmation. Eight-task low-effort pilots scored 3/8 on each side, with three missing answer files per side. A postselected two-task medium-effort diagnostic scored 1/2 original and 0/2 Harbor, with missing files persisting. These diagnostics do not establish parity or reliable file output.

The proposed subset has 104 tasks. Run paired sanity checks first, then one full paired subset run, then scale symmetrically to three runs per side. Record raw scores and mean ± sample SEM, preserving all attempts. The original-side runner is published above; final agreed reproduction commands and model results remain required before submission as ready for review.

## Notes & Caveats

- No test tasks are excluded.
- The verifier pins `kstar-planner==1.6.0`. Version 1.4.1 lacks the `planner_error` and `unsolvable` result fields required by the current upstream scoring utilities. The newer compatible version fixes both valid planner fallbacks and malformed goals without changing the upstream scorer.
- The added instruction is `Save your answer to /app/answer.txt.` Apply it identically on the original side during parity.
- The two-shot benchmark prompt is retained; terminal-agent performance is not directly comparable to a single model-call paper result. Planner fallback and parser behavior remain upstream behavior.
- Reference solutions and all grading dependencies are unavailable during ordinary agent execution.
- All 1,040 generated oracle answers pass the pinned upstream scorer in Docker. The full Harbor oracle run passes 1,040/1,040 tasks with zero exceptions; task coverage and dataset checksums were audited. Formal parity and dataset publication remain pending.

## Installation / Prerequisites

Python 3.12+, `uv`, Git, Docker with Compose, and Harbor 0.27.0 (the development runtime). Run `uv tool install harbor` if needed. Docker builds require network access to install dependencies.

## Troubleshooting

- A revision mismatch means `--source-dir` is not checked out at the revision above.
- An existing output task requires `--overwrite` or a fresh output directory.
- A missing answer receives reward zero. Build failures and verifier crashes must be investigated as infrastructure errors.
- Inspect `jobs/<job>/<trial>/verifier/` and the trial exception when a run fails.

## Citation

See the upstream repository for the original BibTeX citation. Kokel, Katz, Srinivas, and Sohrabi. ACPBench Hard: Unrestrained Reasoning about Action, Change, and Planning. ICLR 2026.

```bibtex
@inproceedings{kokel2026acphard
  author       = {Harsha Kokel and
                  Michael Katz and
                  Kavitha Srinivas and
                  Shirin Sohrabi},
  title        = {{ACPBench Hard}: Unrestrained Reasoning about Action, Change, and Planning},
  booktitle    = {{ICLR}},
  publisher    = {OpenReview.net},
  year         = {2026}
}
```

## Authors & Contributions

Original benchmark authors are credited in each generated `task.toml`.
Harbor adaptation: Chenghao Zhang.
