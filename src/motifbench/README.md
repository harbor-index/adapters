## MotifBench (Critic's Eye) → Harbor Adapter

## Overview

MotifBench (from *Critic's Eye: Review-Grounded Graph Reasoning for Long-Form Motif Discovery*, EMNLP 2026 Findings) evaluates **non-lexical long-document motif recovery**: given target lines from Shakespeare's *Macbeth* and the full text of the play, agents rank recurring literary motifs from a fixed candidate inventory grounded in published literary criticism (26 critical studies, 1765–2020).

- **Task type & domain:** Long-context non-lexical literary reasoning (`Knowledge & Long Context`), English.
- **Provenance & license:** [Acture/MotifBench](https://huggingface.co/datasets/Acture/MotifBench) (CC BY-NC 4.0; Folger Shakespeare Library digital edition CC BY-NC 3.0). Note that this literary benchmark is unrelated to the protein-design benchmark of the same name (`arXiv:2502.12479`); tasks are namespaced under `critics-eye/motifbench__*`.
- **Task count:** **12 tasks** (default `--granularity scene-group`), covering **270 cleaned query lines** and **449 line–motif associations** over **218 clean candidate motifs** (derived from the 314 lines / 277 motifs in `Acture/MotifBench/macbeth/macbeth.jsonl` after anti-leakage sanitization; `--granularity act` [5 tasks] and `--granularity query` [270 tasks] are also supported).
- **Main modifications for Harbor:**
  - Provides the complete *Macbeth* play (2,456 lines, ~19.6k words) as both `/app/macbeth.txt` and `/app/macbeth.pdf` inside an offline container.
  - Filters out 59 leaky motif descriptors (19 explicit Act/Scene/Line citations such as `(4.1.138)` and 40 verbatim passage-pointer descriptors) and 60 pairs with verbatim 3-gram overlap within ±15 lines, eliminating trivial regex/`grep` reward-hacking surfaces while retaining **96.4%** zero content-word overlap and **36.5%** >50-line dispersion.
  - Groups contiguous scenes within each Act into 12 balanced batch tasks (13–36 queries/task, modeled on `src/locomo`) so aggregate `Hit@5` is smooth under partial observation.

## What is MotifBench?

Most long-context benchmarks evaluate **locating** an explicitly stated answer span, which CLI agents with `grep`/`rg` saturate easily. A literary motif is stated nowhere and its realizations rarely share vocabulary; recovering it requires **assembling** scattered, non-lexically-linked passages across the full work. MotifBench anchors ground truth in 26 works of published literary criticism rather than ad-hoc annotation and evaluates ranking via **Hit@5** (primary), **Hit@1**, **Hit@10**, **MRR@20**, and **nDCG@10**. On full-play *Macbeth*, frontier LLMs reach ~17–29% Hit@5.

## Adapter Features

- **Zero-dependency adapter:** Pure Python standard library (`dependencies = []`); auto-downloads dataset assets from HuggingFace `Acture/MotifBench` pinned at revision `12fadc6a46aa8d2fcc7848777574122afeccf59f`, or reads a local export via `--data-dir`.
- **Dual-format full play in workspace:** Mounts complete *Macbeth* as both `/app/macbeth.txt` and searchable `/app/macbeth.pdf` (with pinned `pypdf==7.1.1`) alongside `/app/motifs.json` and `/app/queries.json` under read-only `/app/`. The agent environment runs with `network_mode = "no-network"`.
- **100% deterministic verifier:** Programmatic Python scoring (`tests/verifier.py`) with zero LLM-as-a-Judge dependency and fail-safe zero initialization. The verifier runs in a separate container (`environment_mode = "separate"`, `tests/Dockerfile`, no extra packages, `network_mode = "no-network"`) and reads the transferred artifact at `/workspace/predictions.json`.
- **Multi-granularity & parity support:** Supports `--granularity scene-group` (12 tasks, default), `act` (5 tasks), and `query` (270 tasks), plus `--split parity`, `--limit`, `--overwrite`, and `--task-ids`.
- **Dataset-level micro aggregation:** Includes `src/motifbench/metric.py` to compute query-micro-averaged `Hit@5` across all 270 queries.

## Generated Task Structure

```
datasets/motifbench/
├── {task_id}/                      # e.g., macbeth-act1-s1-s2 ... macbeth-act5-s4-s8
│   ├── task.toml                   # schema 1.3; separate verifier; artifact /workspace/predictions.json
│   ├── instruction.md              # Agent instructions, schema, and query lines
│   ├── environment/
│   │   ├── Dockerfile              # python:3.13-slim + pypdf==7.1.1, read-only /app/
│   │   ├── macbeth.txt             # Complete play (2,456 lines, Folger anchors)
│   │   ├── macbeth.pdf             # Complete play (searchable PDF)
│   │   ├── motifs.json             # 218 cleaned candidate motifs
│   │   └── queries.json            # Query lines for this task
│   ├── solution/
│   │   └── solve.sh                # Deterministic 100% oracle writing /workspace/predictions.json
│   └── tests/
│       ├── Dockerfile              # Separate verifier image: python:3.13-slim, no pip packages
│       ├── test.sh                 # Runs /tests/verifier.py
│       ├── verifier.py             # Deterministic Hit@5 / Hit@1 / Hit@10 / MRR@20 / nDCG@10 scorer
│       └── ground_truth.json       # Gold motif IDs per query line (baked into the verifier image)
```

Adapter code layout:
```
src/motifbench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── motifbench.yaml
├── run_motifbench_parity_codex.yaml
└── src/motifbench/
    ├── __init__.py
    ├── adapter.py
    ├── main.py
    ├── metric.py
    └── task-template/
        ├── task.toml
        ├── instruction.md
        ├── environment/
        │   └── Dockerfile
        ├── solution/
        │   └── solve.sh
        └── tests/
            ├── Dockerfile
            ├── test.sh
            └── verifier.py
```

## Run Evaluation / Harness

### Running with Datasets Registry

```bash
# Standalone Harbor CLI (uv tool install "harbor>=0.27.0")
# Use oracle agent (reference solution)
harbor run -d critics-eye/motifbench -a oracle

# Use your specified agent and model
harbor run -d critics-eye/motifbench -a <agent_name> -m "<model_name>"
```

### Using Job Configurations

From the adapters repository root:

```bash
# Oracle verification (default agent in motifbench.yaml)
harbor run -c src/motifbench/motifbench.yaml -a oracle

# Run with a specific agent/model
harbor run -c src/motifbench/motifbench.yaml -a <agent_name> -m "<model_name>"

# Or run directly against locally generated tasks
harbor run -p datasets/motifbench -a <agent_name> -m "<model_name>"
```

### Running Individual Trial

```bash
# Single task with oracle
harbor trial start -p datasets/motifbench/macbeth-act1-s1-s2 -a oracle

# Single task with an agent
harbor trial start -p datasets/motifbench/macbeth-act1-s1-s2 -a codex -m "openai/gpt-5-mini"
```

## Usage: Create Task Directories

```bash
cd src/motifbench
uv run motifbench --output-dir ../../datasets/motifbench
```

`Acture/MotifBench` is pinned at revision `12fadc6a46aa8d2fcc7848777574122afeccf59f`. `uv run motifbench --output-dir ../../datasets/motifbench` downloads that revision and works out of the box without `--data-dir`. The default `--output-dir` is `datasets/motifbench` at the adapters repository root (`src/motifbench/src/motifbench/main.py` is four parents below that root).

To regenerate from a local export instead, pass `--data-dir`:

```bash
cd src/motifbench
uv run motifbench --data-dir /path/to/motifbench_harbor/macbeth --output-dir ../../datasets/motifbench --overwrite
```

Available flags:
- `--output-dir` — Directory to write generated tasks (defaults to `datasets/motifbench` at the repo root)
- `--data-dir` — Optional local directory containing `macbeth_full.txt`, `macbeth_full.pdf`, `macbeth_clean.jsonl`, and `motifs_clean.json`
- `--granularity` — `scene-group` (12 tasks, default), `act` (5 tasks), or `query` (270 tasks)
- `--split` — `full` (default) or `parity`
- `--limit` — Generate only the first N tasks
- `--overwrite` — Overwrite existing tasks
- `--task-ids` — Only generate specific task IDs

## Comparison with Original Benchmark (Parity)

Parity experiments follow Harbor **Scenario 2** (LLM-based non-agentic benchmark adapted with a matching `codex` runner on both the upstream evaluation harness and Harbor). Both sides read the same cleaned export (`macbeth_full.txt`, `macbeth_full.pdf`, `motifs_clean.json`, `macbeth_clean.jsonl`) and the same rendered `instruction.md`, whose read-only inputs are `/app/macbeth.txt`, `/app/macbeth.pdf`, `/app/motifs.json`, and `/app/queries.json`. The upstream runner is `scripts/motifbench_harbor/run_parity_codex.py` in the Critic's Eye repository. It rebuilds the Harbor tasks, stages those `/app` inputs, scores with `scripts/motifbench_harbor/evaluate.py` (`Hit@5`, `Hit@1`, `Hit@10`, `MRR@20`, `nDCG@10`, and micro-averaged `Hit@5`), and writes `result_original_trial{n}.json` plus a mean ± sample SEM summary. Live model numbers stay pending Harbor-team approval of `parity_experiment.json`. The deterministic oracle path scores **1.0000** micro Hit@5 on all 12 scene-group tasks / 270 queries.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|------------------|--------------|------------------------------|----------------------------|
| TBD | TBD | Hit@5 (micro) | 3 | 12 (270 queries) | Pending | Pending |

Reproduction commands. Upstream, from the Critic's Eye checkout. `--mode oracle` and `--mode dry-run` do not call a paid API:

```bash
uv run python scripts/motifbench_harbor/run_parity_codex.py \
  --mode oracle \
  --data-dir /home/actur/data/motifbench_harbor/macbeth \
  --harbor-root /home/actur/repos/harbor \
  --granularity scene-group \
  --split parity \
  --runs 1
```

`--mode dry-run` only stages `/app` and the instruction text. The paid path is explicit: `--mode codex --model openai/gpt-5-mini --codex-version 0.117.0 --runs 3` with `OPENAI_API_KEY` set.

Harbor side, from this repository root, with the standalone Harbor CLI:

```bash
harbor run -c src/motifbench/run_motifbench_parity_codex.yaml
```

## Notes & Caveats

- **Anti-leakage sanitization:** From the 277 raw motifs and 585 line rows in `Acture/MotifBench` (`macbeth/macbeth.jsonl`), the cleaning pipeline removes 19 motifs containing explicit Act/Scene/Line citations, 40 passage-pointer descriptors (e.g., `"the passage at 1.2.40"`, `"the line 'he loves us not'"`), and 60 local-window (±15 lines) verbatim 3-gram pairs. This yields 218 motifs, 270 query lines, and 449 pairs with 96.4% zero content-word overlap and 36.5% >50-line dispersion.
- **Partial observation:** Published criticism is selective rather than exhaustive; an unrecorded motif is not necessarily wrong. Accordingly, the primary metric is query-averaged **Hit@5** (top-5 recovery), which is monotonic under additional valid observations.
- **Verifier isolation:** `task.toml` uses schema 1.3, `artifacts = ["/workspace/predictions.json"]`, and `[verifier] environment_mode = "separate"`. Both the agent environment and the verifier environment set `network_mode = "no-network"`. Harbor copies the artifact into the verifier container at `/workspace/predictions.json`; `tests/Dockerfile` bakes `test.sh`, `verifier.py`, and `ground_truth.json` into `/tests/` with no extra packages.
- **Reward schema:** `verifier.py` writes `/logs/verifier/reward.json` with `"reward"` (`Hit@5`) as the alphabetically first key, alongside `score_hit_at_1`, `score_hit_at_5`, `score_hit_at_10`, `score_mrr_at_20`, `score_ndcg_at_10`, `score_sum`, and `total_queries`.
- **Licensing:** Primary text derives from the Folger Digital Edition of *Macbeth* (CC BY-NC 3.0); dataset release is CC BY-NC 4.0.

## Installation / Prerequisites

- Docker installed and running
- Standalone Harbor CLI, 0.27.0 or newer: `uv tool install "harbor>=0.27.0"`
- Python 3.12+ with `uv`:
  ```bash
  cd src/motifbench
  uv sync
  ```

## Troubleshooting

- **Verifier reports `reward = 0.0` immediately:** Check `/logs/verifier/grading_details.json` for `load_error`. The separate verifier reads `/workspace/predictions.json`, the path declared in `artifacts`. Ensure the agent wrote a valid JSON object there keyed by `line_id` (e.g., `"ftln-0004"`) with lists of valid `motif_id` strings from `/app/motifs.json`.
- **HuggingFace download failure:** The default pin is `Acture/MotifBench` at `12fadc6a46aa8d2fcc7848777574122afeccf59f`. If that download fails, pass `--data-dir` pointing at a directory that contains `macbeth_full.txt`, `macbeth_full.pdf`, `macbeth_clean.jsonl`, and `motifs_clean.json`.

## Citation

```bibtex
@inproceedings{sun2026criticseye,
  title     = {Critic's Eye: Review-Grounded Graph Reasoning for Long-Form Motif Discovery},
  author    = {Sun, Xinyu and Yin, Zhangyue and Wang, Xin},
  booktitle = {Findings of the Association for Computational Linguistics: EMNLP 2026},
  year      = {2026}
}
```

## Authors & Contributions

This adapter is developed and maintained by [Xinyu Sun](mailto:sunxy23@m.fudan.edu.cn).

**Issues and Contributions:**
- Submit Issues and Pull Requests to the main repository
- Follow the project's coding style and commit guidelines

## Acknowledgement

API inference compute for running parity tests will be acknowledged here if supported by Harbor team credits.
