# AgencyBench Adapter for Harbor

## Overview

This adapter converts **AgencyBench V2** (`GAIR-NLP/AgencyBench`, commit `ec65324be69e81bd4fe394ef6a86d48b8fa5da56`) into Harbor tasks using a **1 scenario = 1 Harbor task** mapping (32 scenarios covering 138 ordered subtasks in full V2; currently implementing all **9 deterministic offline-verifiable scenarios spanning 37 subtasks across 4 categories and 7 runner families**: `Backend/scenario1..3`, `Code/scenario1,6,7`, `MCP/scenario2`, and `Research/scenario2,3`).

- **Upstream Repository:** https://github.com/GAIR-NLP/AgencyBench (`AgencyBench-v2/`, MIT License)
- **Upstream Dataset:** https://huggingface.co/datasets/GAIR/AgencyBench (revision `ea9008235da1864d00380f56e48c18270e84518c`)
- **Paper:** https://arxiv.org/html/2601.11044
- **Harbor Contract Baseline:** `harbor-index/adapters@7b4df9deb2381ae32f26e6cebec06aaa2975dc56` (Contract v1), Harbor CLI `0.26.0`

## What is AgencyBench?

AgencyBench V2 evaluates autonomous agents on long-horizon, multi-stage real-world workflows across 6 core capabilities: **Backend** (3 scenarios / 15 subtasks), **Code** (9 / 29), **Frontend** (3 / 15), **Game** (10 / 50), **MCP** (2 / 10), and **Research** (5 / 19). Each scenario consists of sequential subtasks where earlier workspace deliverables carry forward into subsequent stages, with per-subtask evaluator feedback across attempts.

## Adapter Features

- **Faithful Scenario-Level Mapping:** Each complete scenario maps to one Harbor task (`gair-nlp/agencybench-<category>-scenario-<nn>`), matching the paper's equal 1/32 scenario weighting (verified in arXiv:2601.11044 Table 2).
- **9 Implemented Scenarios (37 Subtasks, 7 Runner Families):**
  - `agencybench-backend-scenario-01` (`backend_cpp_rubric`, 5 subtasks, C++17 + OpenSSL CLI chat application)
  - `agencybench-backend-scenario-02` (`backend_java_rubric`, 5 subtasks, Java 17 CLI task management system)
  - `agencybench-backend-scenario-03` (`backend_py_rubric`, 5 subtasks, Python event generator, WAL KV store, multi-worker orchestrator, DAG planner, and autorepair supervisor)
  - `agencybench-code-scenario-01` (`code_mse_rubric`, 5 subtasks, progressive reaction-kinetics symbolic discovery)
  - `agencybench-code-scenario-06` (`code_loss_single`, 1 subtask, nonlinear harmonic oscillator equation discovery with anti-spoofing independent re-evaluation)
  - `agencybench-code-scenario-07` (`code_loss_single`, 1 subtask, population growth rate equation discovery with anti-spoofing independent re-evaluation)
  - `agencybench-mcp-scenario-02` (`mcp_filesystem`, 5 subtasks, progressive filesystem reorganization)
  - `agencybench-research-scenario-02` (`research_rubric`, 5 subtasks, multi-hop NBA historical & trade fact verification)
  - `agencybench-research-scenario-03` (`research_rubric`, 5 subtasks, cross-sector corporate executive & strategic M&A research)
- **Trusted Multi-Stage Controller (`AgencyBenchAgent`):** Implements Harbor `BaseAgent` (`agencybench.agent:AgencyBenchAgent`) on the trusted host side. Only the currently authorized stage prompt (`subtask_k`) is exposed to the evaluated agent; future queries, ground-truth `rubric` fields, and pinned `eval_task.py` evaluators remain outside the agent-writable container.
- **Exact Per-Scenario Protocol Preservation:** Retains upstream per-scenario divergences verbatim—including `Backend/scenario1..2`'s `score == 10` break, `EXCLUDED_COPY_*` filter, and first-attempt `cache_workspace` retention; `Backend/scenario3`'s `score >= 0` break; `Code/scenario1`'s `score >= 9.9` break and highest-score carry-over; `Code/scenario6..7`'s `score_from_loss` threshold mapping; `MCP/scenario2`'s in-place desktop tree and halt-on-failure semantics; and `Research/scenario2..3`'s in-place deliverable accumulation.
- **Independent Verifier Replay:** `tests/test.sh` never trusts agent-written scores or `meta_eval.json`; it re-executes the unaltered pinned upstream evaluator inside an isolated `/tmp` evalspace with allow-listed copying and symlink-escape rejection, emitting normalized `[0.0, 1.0]` reward to `/logs/verifier/reward.txt` and `/logs/verifier/reward.json`, and full per-subtask diagnostics to `/logs/verifier/agencybench_report.json`.

## Generated Task Structure

```text
agencybench/
└── agencybench-backend-scenario-01/
    ├── task.toml
    ├── instruction.md                 # Stage 1 prompt only (future stages & rubrics hidden)
    ├── environment/
    │   ├── Dockerfile
    │   └── [initial workspace assets] # source/, workspace/, or desktop/ when applicable
    ├── solution/
    │   └── solve.sh                   # Verified stage-by-stage oracle solution
    └── tests/
        ├── test.sh                    # Writes /logs/verifier/{reward.txt,reward.json,agencybench_report.json}
        ├── run_verifier.py            # Independent upstream evaluator replay
        └── upstream/
            ├── eval_task.py           # Pinned unaltered upstream evaluator
            ├── description.json       # Verifier-only stage specifications
            └── [workspace/]           # Immutable evaluate_equation.py & equation.parquet for code_loss_single
```

## Run Evaluation / Harness

### Option 1: Harbor Registry (after dataset publication)

```bash
harbor run -d gair-nlp/agencybench -a oracle
```

### Option 2: Local Generated Dataset

```bash
harbor run -p /path/to/generated/agencybench -a oracle
harbor run -p /path/to/generated/agencybench -a nop
```

### Option 3: Reference Config YAML

```bash
harbor run -c src/agencybench/run_agencybench.yaml
```

### Option 4: Custom Multi-Stage Controller (Scenario 3)

```bash
harbor run -p /path/to/generated/agencybench \
  --agent-import-path agencybench.agent:AgencyBenchAgent \
  --agent-kwarg upstream_root=/path/to/AgencyBench \
  --agent-kwarg inner_agent=sii \
  -m <pinned-model-id>
```

## Usage: Create Task Directories

```bash
cd src/agencybench
uv sync
uv run agencybench --output-dir /path/to/generated/agencybench --split implemented --overwrite
```

Supported CLI flags:
- `--output-dir <path>`: Target directory outside adapter source tree.
- `--split {pilot,parity,implemented,all}`: Scenario split selection (default: `implemented`, 9 scenarios / 37 subtasks).
- `--task-ids <id ...>`: Generate specific Harbor task IDs or upstream scenario IDs.
- `--limit <N>`: Cap number of generated tasks.
- `--overwrite`: Replace existing task directories.
- `--upstream-dir <path>`: Use an existing local `GAIR-NLP/AgencyBench` checkout.

## Comparison with Original Benchmark (Parity)

Parity experiments are **pending Stage D maintainer approval** (scope, agent/model identifiers, API credentials, and budget). No paid runs have been executed.

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|
| Pending Stage D approval | Pending | `S_avg` | 0 | 9 (implemented) | Pending | Pending |

### Reproduction Commands (Planned Symmetric Setup)

```bash
# Original benchmark side (pinned GAIR-NLP/AgencyBench@ec65324be69e81bd4fe394ef6a86d48b8fa5da56)
cd AgencyBench/AgencyBench-v2/Backend/scenario1
python3 eval_task.py

# Harbor side (matched agent/model/prompt/settings via AgencyBenchAgent)
cd src/agencybench
uv run agencybench --output-dir /path/to/generated/agencybench --split parity --overwrite
harbor run -p /path/to/generated/agencybench \
  --agent-import-path agencybench.agent:AgencyBenchAgent \
  --agent-kwarg upstream_root=/path/to/AgencyBench \
  --agent-kwarg inner_agent=sii \
  -m <pinned-model-id>
```

## Notes & Caveats

- **Implemented Deterministic Scope (9 Scenarios / 37 Subtasks):** Covers 100% of `Backend` (`scenario1..3`, 15/15 subtasks), 3 deterministic `Code` scenarios (`scenario1,6,7`, 7 subtasks), `MCP/scenario2` (5 subtasks), and 2 deterministic `Research` scenarios (`scenario2,3`, 10 subtasks). Remaining scenarios requiring paid LLM/vision judges (`Code/4,5`, `Frontend/1..3`, `Game/1..10`, `Research/1,4,5`), nested Docker (`Code/2,3`), MongoDB service (`Code/8,9`), or live GitHub repo mutation (`MCP/1`) are cataloged in `src/agencybench/registry.py` (`SCENARIO_CATALOG`) and gated on Stage D approval.
- **Paper-vs-Code Divergences & Upstream Quirks Preserved / Hardened:**
  - `Backend/scenario1..2/eval_task.py` break early on `score == 10` and retain attempt 1 in `cache_workspace` unless a later attempt scores 10, whereas `Code/scenario1/eval_task.py` breaks at `score >= 9.9` and forwards `max(attempts, key=score)`.
  - `Backend/scenario2/eval_task.py` (`CommandRunner.compile_java` L512) filters `not any(part.startswith(".") for part in path.parts)` on the full absolute path rather than the path relative to `evalspace`; `run_verifier.py` therefore places its temporary evaluation directory under `/tmp` so host `TMPDIR` values containing dot-directories (e.g. `~/.cache/tmp`) never mask Java sources.
  - `Backend/scenario3/eval_task.py` breaks early on `if rubric_result.score >= 0: break` (L1348, i.e., after attempt 1) and computes scheduling fairness via `1.0 - (sigma or math.inf) / 1.5` (L927), which treats exact `0.0` as falsy in Python and substitutes `math.inf`.
  - `Code/scenario1/eval_task.py` uses `THRESHOLDS` `(5e-1, 1e-1)` .. `(1e-2, 1e-3)` rather than the tighter prose numbers in `description.json`.
  - `Code/scenario6..7/eval_task.py` read `subtask1_result.json["loss"]` directly without re-running `evaluate_equation.py`. To prevent reward spoofing, the Harbor verifier validates the `subtask1_result.json` schema AND independently re-executes the immutable pinned `evaluate_equation.py` and `equation.parquet` from `/tests/upstream/workspace/` against the agent's `equation.py`, scoring `max(reported_loss, measured_loss)`.
  - `Code/scenario8/eval_task.py` (`SubtaskVerifier._check_criterion()`) leaves 6/31 acceptance criteria unreachable (`criterion_not_verifiable`, including backtick-wrapped `issues.assigned` and `issue_comment.created`), capping the maximum achievable score at `41.04/50` (`0.821`) under verbatim upstream grading in addition to requiring a local `mongod` service when `USE_MOCK=false`.
  - `Research/scenario2/description.json` stores ground-truth answers inside each subtask dict's `"rubric"` key; matching upstream `_build_prompt()` (L1218), the adapter exposes only `"query"` and `"deliverables"` in `instruction.md` and stage prompts.
  - Upstream ships zero verified reference solutions (`claude/meta_eval.json` contains partial evaluation runs only); stage-by-stage oracle solutions in `solution/solve.sh` were authored and independently verified (`reward = 1.000` on all 9 scenarios).
- **Known Limitations:** `parity_experiment.json` is intentionally empty (`[]`) until Stage D approval and matched Stage E runs complete.

## Installation / Prerequisites

- Python 3.11+ and `uv`
- Harbor CLI (`harbor --version` >= 0.26.0)
- Docker engine (or approved cloud sandbox such as Daytona) for container execution

## Troubleshooting

- **Missing upstream checkout:** Pass `--upstream-dir /path/to/AgencyBench` or set `AGENCYBENCH_UPSTREAM_ROOT`.
- **Verifier security error:** If an agent creates symlinks escaping `/workspace`, the verifier fails closed with `security_violation` and reward `0.0`.
- **Stage snapshots missing:** When running `nop` or a single-turn agent without `AgencyBenchAgent`, the verifier evaluates `/workspace` directly without faking intermediate stages.

## Citation

```bibtex
@article{li2026agencybench,
  title={AgencyBench: Benchmarking the Frontiers of Autonomous Agents in 1M-Token Real-World Contexts},
  author={Li, Keyu and Shi, Junhao and Xiao, Yang and Jiang, Mohan and Sun, Jie and Wu, Yunze and Xia, Shijie and Cai, Xiaojie and Xu, Tianze and Si, Weiye and others},
  journal={arXiv preprint arXiv:2601.11044},
  year={2026}
}
```

## Authors & Contributions

- Adapter owner: [`Breevio`](https://github.com/Breevio) (`breeviii`, v2.0 Adapter List row 173).
