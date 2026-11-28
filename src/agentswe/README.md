## AgentSWE → Harbor Adapter

## Overview

AgentSWE evaluates coding agents as *agent builders*: a Builder (a code-agent harness plus model) receives a
natural-language requirement and a few public example cases, develops a task-specific agent in one long session with
authoritative development feedback, and the frozen agent it delivers is then run on held-out cases that cover the
rest of the requirement. Scores come from deterministic gates plus a requirement-derived rubric applied by an LLM
judge (0–100 per case; a run's score is the mean over its held-out cases).

- **Source:** [VectorSpaceLab/AgentSWE](https://github.com/VectorSpaceLab/AgentSWE) at commit
  [`6643c739`](https://github.com/VectorSpaceLab/AgentSWE/tree/6643c7398b2d7dd58b32b6a3a829e564ab68a23f)
  (Apache-2.0), paper [arXiv:2610.13361](https://arxiv.org/abs/2610.13361).
- **Upstream size:** 25 tasks in three families: Creation (10), Editing (10) and Optimization (5). AgentSWE-Lite is an
  8-task subset (Creation `repo`, `db`, `pdf`; Editing `deeptutor`, `aider`, `openwiki`; Optimization `tau3`,
  `pinch`).
- **This adapter:** one Harbor task per AgentSWE task. The current release adapts the Creation family tasks whose
  per-case evaluation needs a single container: `repository-bug-repair` (`repo`), `database-analytics` (`db`) and
  `scientific-pdf-translation` (`pdf`), i.e. all Creation tasks of AgentSWE-Lite. The remaining tasks are listed in
  [Notes & Caveats](#notes--caveats) with the reason for each exclusion.
- **Main adaptation:** upstream runs every task through Harbor but orchestrates it from a host-side controller that
  starts up to 23 Harbor jobs per task. Here that controller runs inside the task as an `evaluator` sidecar and as a
  separate verifier, so a task is one Harbor trial. Prompts, cases, rubrics, harnesses and scoring scripts are the
  upstream files at the pinned commit, unchanged.

## What is AgentSWE?

AgentSWE (*Can Coding Agents Build the Agent You Actually Want?*) measures whether a coding agent can deliver an agent
that actually satisfies a requirement, not just one that passes the examples it was shown. Each task is a commission:
the Builder gets the requirement documents and two public development cases, may submit its candidate for
authoritative evaluation on those cases (up to 10 accepted submissions for Creation), and its final delivery is frozen
and run on six held-out cases. Every delivery calls the same fixed runtime model through an evaluator-owned broker,
so differences in scores come from what the Builder built (prompts, tool orchestration, state handling, output
validation). Creation tasks start from an empty workspace; Editing tasks extend real open-source agents; Optimization
tasks improve an agent's score on an existing benchmark.

## Adapter Features

- **Pinned upstream, generated tasks:** `agentswe` clones AgentSWE at the pinned commit (or uses `--upstream-dir`) and
  copies each task's upstream files into the generated task; the configuration profile (`lite-v1.1` by default) is
  applied as upstream does (overlay files replace base files).
- **Builder session:** the task's `instruction.md` is upstream's Builder prompt (`one_stop.builder_instruction`),
  byte for byte. The Builder sees `/builder-package` (requirement documents and public dev cases), a writable private
  copy of the task's conda environment at its documented path, and writes its agent to `/workspace/submission`.
- **Authoritative dev feedback:** `submit_dev_candidate` talks to upstream's `one_stop.DevController` running in the
  `evaluator` sidecar over a socket on a shared volume (same CLI, same feedback text, same round limit and
  idempotency rules). The sidecar holds the dev cases, rubric, broker stack and provider keys; the Builder container
  holds none of them.
- **Freeze and held-out evaluation:** after the Builder exits, a `[[verifier.collect]]` hook calls upstream's
  `freeze_after_builder_exit` in the sidecar. The frozen delivery, the Builder's environment and the dev records are
  passed to a separate verifier (`tests/Dockerfile`), which holds the held-out cases and runs each case exactly as
  upstream's Candidate and Eval jobs do: it stages them with the task's own `adapter.py`, executes their
  `solution/solve.sh` and `tests/test.sh`, and recreates every mount and environment variable their generated compose
  files declare. Candidates run as an unprivileged user; reward = held-out mean / 100.
- **Upstream broker topology:** Candidate → evaluator broker (`runners/creation/broker_v2.py`) → runtime role broker
  (`broker/agentswe_broker`) → provider; Result judge → judge role broker → provider; Builder → builder role broker
  (`--rate-limit-policy as_503`) → provider. Only the role brokers hold provider keys.
- **Images:** built from upstream's pinned recipes (Ubuntu digest, apt snapshot, `env-builder` micromamba environments,
  `trusted-browser` bundle), all `linux/amd64`. No privileged containers and no Docker socket; `repo` adds only
  `SYS_PTRACE`, which its upstream verifier needs for `strace`.
- **Failures are not zeros:** a provider or broker failure withholds the score (the verifier exits without a reward),
  following upstream's `assert_runtime_health`.
- **Builder harness:** `agentswe.codex_agent:AgentSWECodex` is upstream's `ProviderCodex` (Codex 0.144.1 with the
  AgentSWE-Lite model catalog generated by upstream `gen_codex_config.py`) ported to Harbor 0.27.0. Any Harbor agent can
  be used as the Builder; the custom agent is what the parity experiments use.

## Generated Task Structure

```
agentswe/
├── repository-bug-repair/
│   ├── task.toml                      # agent 8 h, separate verifier, artifacts, freeze hook
│   ├── instruction.md                 # upstream Builder prompt
│   ├── environment/
│   │   ├── Dockerfile                 # Builder: task env (private copy) + /builder-package
│   │   ├── docker-compose.yaml        # main + evaluator sidecar, shared volumes
│   │   ├── builder-package/           # requirement documents + public dev cases
│   │   ├── submit_dev_candidate       # wrapper around upstream's client
│   │   └── evaluator/                 # sidecar image: DevController, brokers, dev cases, rubric
│   ├── solution/
│   │   └── solve.sh                   # null delivery (no upstream reference agents exist)
│   └── tests/
│       ├── Dockerfile                 # separate verifier: held-out cases, trusted runtime, brokers
│       ├── docker-compose.yaml
│       └── test.sh                    # python3 -m agentswe_harbor.verify
├── database-analytics/
└── scientific-pdf-translation/
```

Adapter code:

```
src/agentswe/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_agentswe.yaml
└── src/agentswe/
    ├── adapter.py                     # AgentSWEAdapter: pin, select, render tasks
    ├── main.py                        # CLI
    ├── codex_agent.py                 # AgentSWECodex (Builder harness)
    ├── codex_builder/                 # upstream gen_codex_config.py + Lite catalog (vendored)
    ├── runtime/agentswe_harbor/       # sidecar + verifier runtime (devserver, freeze, pipeline, verify)
    └── task-template/
```

## Run Evaluation / Harness

Once the dataset is published:

```bash
harbor run -d agentswe/agentswe -a <agent> -m "<model>"
```

For development and reproduction, from the repository root after generating the tasks (see below):

```bash
# Oracle (null delivery; expected reward 0, exercises freeze, Candidate and Eval jobs)
harbor run -c src/agentswe/run_agentswe.yaml

# AgentSWE Builder harness (as in the parity experiments)
uv run --project src/agentswe --extra parity harbor run -p datasets/agentswe \
  --agent-import-path agentswe.codex_agent:AgentSWECodex -m gpt-6-luna \
  --ak version=0.144.1 --ak context_window=272000

# Any Harbor agent as the Builder
harbor run -p datasets/agentswe -a codex -m "<model>"
```

Provider configuration is read from the environment of the `harbor` process:

| Variable | Used by | Default |
|---|---|---|
| `AGENTSWE_RUNTIME_BASE_URL` / `_API_KEY` / `_MODEL` / `_EFFORT` | runtime role broker (model the delivered agent calls) | `https://api.deepseek.com/v1`, `deepseek-flash`, `high` |
| `AGENTSWE_JUDGE_BASE_URL` / `_API_KEY` / `_MODEL` / `_EFFORT` | judge role broker (Result judge) | `https://api.deepseek.com/v1`, `deepseek-flash`, `max` |
| `AGENTSWE_BUILDER_BASE_URL` / `_API_KEY` / `_MODEL` / `_EFFORT` | builder role broker (only for `AgentSWECodex`) | unset |
| `AGENTSWE_PROTOCOL_MODE=smoke` | upstream smoke budget: 1 dev round, 1 held-out case | `formal` |
| `AGENTSWE_DOCKER_REGISTRY`, `AGENTSWE_BUILD_PROXY`, `AGENTSWE_APT_MIRROR`, `AGENTSWE_CONDA_CHANNEL`, `AGENTSWE_PIP_INDEX_URL`, `AGENTSWE_NODE_DIST_URL` | image builds behind a firewall | public upstreams |

## Usage: Create Task Directories

```bash
cd src/agentswe
uv run agentswe --output-dir ../../datasets/agentswe
```

Available flags:
- `--output-dir` — directory to write generated tasks (default `datasets/agentswe` at the repository root)
- `--limit` — generate only the first N tasks
- `--overwrite` — overwrite existing tasks
- `--task-ids` — only generate these tasks (upstream id or short name, e.g. `repo`)
- `--split` — `full` (all adapted tasks), `lite` / `parity` (the AgentSWE-Lite tasks)
- `--profile` — upstream configuration profile (default `lite-v1.1`)
- `--upstream-dir` — use an existing AgentSWE checkout at the pinned commit instead of cloning
- `--cache-dir` — where the pinned checkout is cloned (default `~/.cache/harbor-agentswe`)

## Comparison with Original Benchmark (Parity)

Parity is in progress; the plan below is under discussion with the Harbor team.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|------------------|--------------|------------------------------|----------------------------|
| agentswe-codex@0.144.1 | gpt-6-luna | held-out mean score | pending | 3 (Creation Lite) | pending | pending |

- **Configuration (both sides):** Builder Codex 0.144.1 with the AgentSWE-Lite catalog and `gpt-6-luna`; runtime and
  judge `deepseek-v4.1-flash` (`lite-v1.1` profile efforts: runtime `high`, judge `max`); same machine, local Docker.
- **Additional check:** score replay — the same frozen delivery scored by upstream's Candidate/Eval jobs and by the
  Harbor verifier, with request bodies to the runtime and judge compared.

Reproduction (both sides on the same host; provider variables as in the table above):

```bash
# Original side: unmodified upstream at the pinned commit
git clone https://github.com/VectorSpaceLab/AgentSWE.git && cd AgentSWE
git checkout 6643c7398b2d7dd58b32b6a3a829e564ab68a23f
cat >> .env <<'EOF'
AGENTSWE_PROFILE=lite-v1.1
AGENTSWE_DEFAULT_BASE_URL=<runtime/judge provider>/v1
AGENTSWE_DEFAULT_API_KEY=<key>
AGENTSWE_RUNTIME_MODEL=deepseek-v4.1-flash
AGENTSWE_JUDGE_MODEL=deepseek-v4.1-flash
AGENTSWE_BUILDER_BASE_URL=<builder provider>/v1
AGENTSWE_BUILDER_API_KEY=<key>
AGENTSWE_BUILDER_MODEL=gpt-6-luna
AGENTSWE_BUILDER_CONTEXT_WINDOW=272000
EOF
python3 -m agentswe setup repo
python3 -m agentswe run repo --builder codex --seed 1

# Harbor side
cd src/agentswe && uv run agentswe --output-dir ../../datasets/agentswe && cd ../..
uv run --project src/agentswe --extra parity harbor run -p datasets/agentswe/repository-bug-repair \
  --agent-import-path agentswe.codex_agent:AgentSWECodex -m gpt-6-luna \
  --ak version=0.144.1 --ak context_window=272000
```

## Notes & Caveats

- **Oracle:** upstream ships no reference agents for Creation tasks, so no delivery is known to reach reward 1.0.
  `solution/solve.sh` delivers a valid null agent; the oracle run checks that the pipeline (freeze, Candidate job,
  deterministic gates, Result judge) runs without exceptions and scores it 0.
- **Excluded for now:**
  - *Editing (10 tasks):* upstream runs them as system-scope systemd units with cgroup v2 delegation on the host and
    sends the lower agent and judge only to the official DeepSeek API; this needs host-level privileges that a Harbor
    task container does not have.
  - *Optimization (5 tasks):* each runs an upstream benchmark harness (τ³, PinchBench, BrowseComp, OSWorld,
    Terminal-Bench) in its own multi-container topology; to follow.
  - *Creation tasks with multi-service per-case jobs:* `desktop-gui-automation` (GUI control plane and model relay),
    `document-to-editable-pptx` and `web-research-report` (search front, LibreOffice), `authorized-vulnerability-
    validation` (`NET_ADMIN` sandbox), and the remaining single-container Creation tasks until they are verified.
- **Differences from upstream:** per-case Candidate and Eval jobs run sequentially as processes inside the evaluator
  or verifier container instead of in separate containers; Candidates run as an unprivileged user with a scrubbed
  environment, while upstream also restricts their network to the broker. The Builder's model traffic goes through
  the builder role broker in the sidecar instead of a host-side broker.
- **Variance:** single builds vary a lot (upstream reports 32.8, 33.3 and 90.5 for the same configuration); report
  means over several runs.
- **Cost and time:** a formal run is up to 8 h of Builder session plus up to 10 dev evaluations and 6 held-out cases.

## Installation / Prerequisites

- Linux x86-64 host with Docker (Compose ≥ 2.20); about 20 GB of disk per task for images and environments.
- Harbor 0.27.0 (`uv tool install harbor`) or the adapter's `parity` extra:
  ```bash
  cd src/agentswe
  uv sync --extra parity
  ```
- Provider keys in the environment (see the table above). The runtime and judge default to the official DeepSeek API.

## Troubleshooting

- **Image builds behind a firewall:** set `AGENTSWE_DOCKER_REGISTRY` to a Docker Hub mirror and `AGENTSWE_BUILD_PROXY`
  (or a Docker client `proxies` configuration) for apt, conda, Node and Chrome downloads.
- **Verifier exits without a reward:** an evaluator-side failure (provider error, broker failure, missing evidence);
  see `/logs/verifier` and the per-case logs under `hidden_phase/` in the trial directory.
- **`submit_dev_candidate` reports the controller missing:** the `evaluator` sidecar failed to start; check its logs.

## Citation

```bibtex
@misc{wang2026agentswe,
  title         = {AgentSWE: Can Coding Agents Build the Agent You Actually Want?},
  author        = {Jiahao Wang and Hongjin Qian and Yuyang Hu and Jiajun Zhang and Zheng Liu},
  year          = {2026},
  eprint        = {2610.13361},
  archivePrefix = {arXiv},
  url           = {https://arxiv.org/abs/2610.13361}
}
```

## Authors & Contributions

This adapter is developed and maintained by [Zixuan Wang](https://github.com/wannabeyourfriend).

**Issues and Contributions:**
- Submit Issues and Pull Requests to the main repository
- Follow the project's coding style and commit guidelines

## Acknowledgement

AgentSWE was created by Jiahao Wang, Hongjin Qian, Yuyang Hu, Jiajun Zhang and Zheng Liu. The vendored Codex
configuration generator (`codex_builder/`) is upstream's `builders/codex` code under the Apache-2.0 license.
