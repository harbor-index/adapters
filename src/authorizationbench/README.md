# AuthorizationBench → Harbor Adapter

> **Status: work in progress.** Sections marked _TODO_ are filled in as the adapter, oracle
> verification, and parity experiments are completed.

## Overview

AuthorizationBench evaluates whether LLM agents act **within the authorization scope** a
principal delegated to them, whether or not acting outside that scope causes direct harm.

- **Source:** [PEACH-Research-Lab/AuthorizationBench](https://github.com/PEACH-Research-Lab/AuthorizationBench)
  pinned at commit `bfe1fc27b513d47dd02a0b5625ccb4f9a2b0e47c`; paper
  [arXiv:2610.27548](https://arxiv.org/abs/2610.27548). Code and benchmark artifacts are MIT-licensed.
- **Size:** 286 executable tasks (`benchmark/manifest.json`): 44 original task cases grounded in
  42 documented real-world source records, plus 242 contextual expansions.
- **Coverage:** 14 situational pressures (12 benign, 2 prompt-injection), tasks with (185) and
  without (101) a direct safety/privacy harm, and 10 personal and professional domains.
- **Interaction:** each task gives the agent a delegated instruction, a role/prior-memory context,
  a `/workspace` with files, and 0–4 deterministic mock services (23 service templates, e.g.
  email, docs portal, Zoom, shopping) exposed as tools. A simulated principal ("Oracle User")
  answers when the agent contacts it through `message_user`.
- **Adapter size:** _TODO_ — planned to include all 286 tasks; any exclusion will be listed in
  [Notes & Caveats](#notes--caveats).

## What is AuthorizationBench?

AuthorizationBench ("Act on Your Behalf, Beyond Your Authorization: Benchmarking LLM Agent
Authorization Alignment", Zhang et al., 2026) turns documented real-world delegation incidents
into executable tasks. In every task, both the authorized completion path and a case-specific
out-of-authorization path are executable, and each task carries an authorization contract:
completion criteria plus a list of out-of-authorization (violated) actions.

Each trajectory receives two independent binary labels from post-hoc judges (Claude Opus 5.5 in
the paper), supported by deterministic state checks for filesystem effects:

- **Out-of-authorization rate (OAR)** — headline metric: share of trajectories in which at least
  one listed out-of-authorization action is realized (19.2% overall in the paper).
- **Completion rate** — share of trajectories that satisfy at least one completion criterion.
- **Safe completion** — completed without any out-of-authorization action.

The paper evaluates OpenHands SDK 1.49.2, the OpenAI Agents SDK 0.20.0 (SandboxAgent), and the
Claude Agent SDK 0.2.159 with four models at low/high reasoning effort, a 35-turn limit, and
MiMo v2.6 Pro as the Oracle User.

## Adapter Features

_TODO — planned:_

- Converts each upstream task bundle (`task_config.json`, `system_prompt.txt`, `prior_memory.md`,
  `fixtures/`, `workspace/`) into a Harbor task, preserving upstream task IDs.
- Runs the upstream mock services and Oracle User in a sidecar container and exposes the
  business tools plus `message_user` to the agent over MCP.
- Collects the service audit log, user-chat log, and workspace state after the agent phase and
  grades them with the upstream completion and violation judges.
- Oracle solutions replay the upstream scripted authorized path for each task.

## Generated Task Structure

_TODO — tentative layout, finalized with the implementation:_

```
authorizationbench/
├── {task_id}/                     # e.g. t0002-f-a-h
│   ├── task.toml                  # Task configuration (name, MCP server, verifier env)
│   ├── instruction.md             # Delegated instruction + agent context
│   ├── environment/
│   │   ├── Dockerfile             # Agent container with /workspace files
│   │   ├── docker-compose.yaml    # Adds the services / Oracle User sidecar
│   │   └── sandbox/               # Sidecar image: mock services, fixtures, MCP gateway
│   ├── solution/
│   │   └── solve.sh               # Replays the upstream scripted authorized path
│   └── tests/
│       ├── test.sh                # Runs the judges and writes the reward
│       └── ...                    # Judge prompts, detectors, task contract
```

Adapter code layout:

```
src/authorizationbench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_authorizationbench.yaml
└── src/authorizationbench/
    ├── __init__.py
    ├── adapter.py
    ├── main.py
    └── task-template/
        ├── task.toml
        ├── instruction.md
        ├── environment/
        │   └── Dockerfile
        ├── solution/
        │   └── solve.sh
        └── tests/
            └── test.sh
```

## Run Evaluation / Harness

### Running with the Datasets Registry

After the dataset is published:

```bash
# Oracle agent (reference solution)
harbor run -d peach-research-lab/authorizationbench

# A specific agent and model
harbor run -d peach-research-lab/authorizationbench -a <agent_name> -m "<model_name>"
```

### Using Job Configurations

From the adapters repository root, after generating tasks locally:

```bash
# Default configuration (oracle agent)
harbor run -c src/authorizationbench/run_authorizationbench.yaml

# Locally prepared dataset path with a specific agent and model
harbor run -p datasets/authorizationbench -a <agent_name> -m "<model_name>"

# Resume a previously started job
harbor job resume -p /path/to/jobs/directory
```

Results are saved in `jobs/` by default (configurable via `jobs_dir` in the YAML config).

### Running Individual Trial

```bash
# Single task with the oracle solution
harbor trial start -p datasets/authorizationbench/<task_id>

# Single task with a specific agent and model
harbor trial start -p datasets/authorizationbench/<task_id> -a <agent_name> -m "<model_name>"
```

## Usage: Create Task Directories

```bash
cd src/authorizationbench
uv sync
uv run authorizationbench --output-dir ../../datasets/authorizationbench
```

Available flags:

- `--output-dir` — directory to write generated tasks to
- `--limit` — generate only the first N tasks
- `--overwrite` — overwrite existing task directories
- `--task-ids` — generate only the listed task IDs

## Comparison with Original Benchmark (Parity)

_TODO — parity plan to be agreed with the Harbor team before any runs._ Scores are reported as
**mean ± sample SEM**; raw per-run scores are in [`parity_experiment.json`](parity_experiment.json).

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|----------------|--------------|--------------------------------|----------------------------|
| TODO | TODO | TODO | TODO | TODO | TODO | TODO |

## Notes & Caveats

- Both the agent phase (Oracle User replies) and the verifier (completion and violation judges)
  call LLMs, so API credentials are needed at run time and at grading time, and scores carry
  judge and user-simulator nondeterminism.
- _TODO_: deviations from the upstream harnesses, excluded tasks, and known limitations.

## Installation / Prerequisites

- Docker installed and running (or a Daytona API key for cloud sandboxes)
- Harbor CLI: `uv tool install harbor`
- Adapter dependencies:
  ```bash
  cd src/authorizationbench
  uv sync
  ```
- _TODO_: environment variables for the Oracle User and judge models.

## Troubleshooting

_TODO._

## Citation

```bibtex
@article{zhang2026authorizationbench,
  title   = {Act on Your Behalf, Beyond Your Authorization:
             Benchmarking {LLM} Agent Authorization Alignment},
  author  = {Zhang, Zhiping and Li, Franklin Mingzhe and
             Huang, Raio Zihao and Li, Tianshi},
  journal = {arXiv preprint arXiv:2610.27548},
  year    = {2026}
}
```

## Authors & Contributions

This adapter is developed and maintained by _TODO: your name_.

**Issues and Contributions:**

- Submit issues and pull requests to [harbor-index/adapters](https://github.com/harbor-index/adapters).

## Acknowledgement

API inference compute for running parity tests is generously supported by
[2077AI](https://www.2077ai.com/) (https://www.2077ai.com/).
