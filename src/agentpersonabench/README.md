# AgentPersonaBench → Harbor Adapter

## Overview

This adapter converts the survey, chat, web, and os-app surfaces of [AgentPersonaBench](https://github.com/MatrAIx-ai/AgentPersonaBench) into Harbor tasks. The upstream benchmark has 2,460 tasks. This adapter emits 2,301. The other 159 are listed in `EXCLUSIONS.md` and `EXCLUSIONS-<surface>.md` when the dataset is generated. A task is emitted only when its own verifier can score an oracle artifact as fully held. Most chat oracles restate the verifier's HELD clauses as `user_turns.json`. Sixty chat oracles instead use calibrated concrete turns because a rubric restatement was insufficient or unstable. The judge prompt is unchanged.

- **Source**: https://github.com/MatrAIx-ai/AgentPersonaBench (code Apache-2.0, tasks CC-BY-4.0)
- **Paper**: https://arxiv.org/abs/2610.04379
- **What changed**: per-task Dockerfile. Survey and chat images contain that task's `input/` (`bot.md` and `context.md` for chat). Web images start from the upstream Playwright base and add the task site. Os-app images inline the upstream Linux desktop base, then the task's own app install. `solution/solve.sh` is a deterministic oracle. For chat it writes `user_turns.json`. Most tasks restate each verifier HELD clause in the user's voice. The 60 slugs in `chat_oracle_calibrated.txt` use concrete choices and commitments because the restatement did not score stably. Those 60 turns are hand-written and were checked against a judge. `[verifier.env]` passes `ANTHROPIC_API_KEY` through and sets `ADHERENCE_JUDGE_MODEL` to `claude-opus-4-8`. `tests/test.sh` points that verifier at `/logs/verifier/reward.txt`. Web, os-app, and chat reconstruct the task tree the verifier reads, and chat adds `tests/llm_client.py` so the original `from llm_client import chat` still resolves. Task text, `instruction.md`, and verifier pass/fail rules are copied unchanged. Harbor `task.toml` stays the harness config. Where a verifier used to load `[[checks]]` from the benchmark `task.toml`, those tables are written to `tests/checks.toml` and the copied verifier reads that file.
- **Persona**: `persona.yaml` stays in the host task directory. It is not copied into the image. Survey composition is `persona-openhands-sdk`. Chat is `persona-chat`. Web is `persona-web`. Os-app is `persona-computer-1`.

## What is AgentPersonaBench?

AgentPersonaBench checks whether a frozen persona changes what an agent does, not how it describes itself. Survey tasks ask the agent to answer a questionnaire, write a short reply, or submit a small code file. Chat tasks are a 12-turn conversation: the persona is the user, and the task's `bot.md` is the other side. Web tasks ask the agent to use a local page. Os-app tasks ask the agent to use a Linux desktop application. The verifier scores the resulting artifact. A single-attribute task is held or violated. A multi-attribute task awards one point per held check. The task text does not name the trait under test.

## Adapter Features

- Reads a local AgentPersonaBench checkout (`--source` or `$APB_SOURCE`)
- `--surface survey`, `--surface chat`, `--surface web`, or `--surface app`
- Oracle `solve.sh` writes the artifact the upstream verifier treats as fully held
- Upstream `verifier.py` and its answer key are copied unchanged, aside from the `checks.toml` path above
- Survey agent `persona-openhands-sdk` (`agentpersonabench.agents.persona_openhands_sdk:PersonaOpenHandsSdk`): host-side persona prompt, `reasoning_effort=medium`, non-model HTTP blocked
- Chat agent `persona-chat` (`agentpersonabench.agents.persona_chat:PersonaChat` 1.0.0): 12-turn user simulator at temperature 0.7 against `bot.md` at temperature 0.6
- Web agent `persona-web` (`agentpersonabench.agents.persona_web:PersonaWeb` 1.0.0)
- Os-app agent `persona-computer-1` (`agentpersonabench.agents.persona_computer_1:PersonaComputer1`)

## Generated Task Structure

```
agentpersonabench/
├── {task_id}/
│   ├── task.toml                 # Task configuration
│   ├── instruction.md            # Task instructions for the agent
│   ├── persona.yaml              # Host-side persona. Not copied into the image.
│   ├── environment/
│   │   └── Dockerfile
│   ├── solution/
│   │   └── solve.sh              # Oracle
│   └── tests/
│       ├── test.sh
│       ├── verifier.py
│       └── checks.toml           # Present when the verifier reads [[checks]]
```

```
src/agentpersonabench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
└── src/agentpersonabench/
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

`adapter.py` defines `AgentPersonaBenchAdapter` with a `run()` method. `main.py` constructs it and calls `run()` through the standard CLI flags.

## Run Evaluation / Harness

Harbor Registry & Datasets makes running adapter evaluation easy and flexible.

### Running with Datasets Registry

The dataset PR is open ([harbor-datasets#270](https://github.com/harbor-framework/harbor-datasets/pull/270)) and is not in the registry yet. Until it is merged and published, run against a local directory.

```bash
# Oracle check on one generated task
uv run harbor run -p datasets/agentpersonabench/<task_id> -a oracle

# Survey parity agent
PYTHONPATH=src/agentpersonabench/src harbor run -p datasets/agentpersonabench/<survey-task-id> \
  -a agentpersonabench.agents.persona_openhands_sdk:PersonaOpenHandsSdk \
  -m "<provider/model>" --ak reasoning_effort=medium
```

### Using Job Configurations

`run_agentpersonabench.yaml` runs the oracle agent. `run_agentpersonabench_parity.yaml` runs the recorded web parity arm against a directory generated with `--surface web --split parity`. Survey, chat, and os-app use different agents, so their reproduction commands remain separate below.

```bash
# From the repository root
harbor run -c src/agentpersonabench/run_agentpersonabench.yaml

# Chat
PYTHONPATH=src/agentpersonabench/src harbor run \
  -a agentpersonabench.agents.persona_chat:PersonaChat \
  -m "dashscope/deepseek-v4.1-flash" -e docker \
  -p datasets/agentpersonabench/<chat-task-id>

# Web
PYTHONPATH=src/agentpersonabench/src harbor run \
  -a agentpersonabench.agents.persona_web:PersonaWeb \
  -m "qwen3.8-flash" -e docker \
  -p datasets/agentpersonabench/<web-task-id>

# Os-app
PYTHONPATH=src/agentpersonabench/src harbor run \
  -a agentpersonabench.agents.persona_computer_1:PersonaComputer1 \
  -m "qwen3.8-flash" -e docker \
  -p datasets/agentpersonabench/<app-task-id>
```

Results are saved in the `jobs/` directory by default.

### Running Individual Trial

```bash
uv run harbor trial start -p datasets/agentpersonabench/<task_id>

PYTHONPATH=src/agentpersonabench/src harbor trial start -p datasets/agentpersonabench/<task_id> \
  -a agentpersonabench.agents.persona_openhands_sdk:PersonaOpenHandsSdk \
  -m "<provider/model>" --ak reasoning_effort=medium
```

## Usage: Create Task Directories

```bash
cd src/agentpersonabench
uv run agentpersonabench --source /path/to/AgentPersonaBench --surface survey --overwrite \
  --output-dir ../../datasets/agentpersonabench
uv run agentpersonabench --source /path/to/AgentPersonaBench --surface chat --overwrite \
  --output-dir ../../datasets/agentpersonabench
uv run agentpersonabench --source /path/to/AgentPersonaBench --surface web --overwrite \
  --output-dir ../../datasets/agentpersonabench
uv run agentpersonabench --source /path/to/AgentPersonaBench --surface app --overwrite \
  --output-dir ../../datasets/agentpersonabench
```

Available flags:

- `--output-dir` — Directory to write generated tasks (defaults to `datasets/agentpersonabench` at the repo root)
- `--limit` — Generate only the first N tasks
- `--overwrite` — Overwrite existing tasks
- `--task-ids` — Only generate specific task IDs
- `--source` — AgentPersonaBench checkout (or set `$APB_SOURCE`)
- `--surface` — `survey` (default), `chat`, `web`, or `app`
- `--split` — `all` or `parity`. The parity subset is 115 emitted tasks (survey 25, chat 28, web 32, os-app 30), drawn with `random.Random(42)` and listed in `parity_task_ids.json`

Generated counts from the current checkout: survey 502 of 502, chat 562 of 565, web 644 of 707, os-app 593 of 686.

## Comparison with Original Benchmark (Parity)

The comparison below is a 115-task subset, not a full-benchmark parity. Task ids are in `parity_task_ids.json` (`random.Random(42)`, stratified by surface: survey 25, chat 28, web 32, os-app 30). Scores are mean ± sample standard error of the mean of the three full passes. The metric is the verifier's point count (checks held), the same integer the upstream envelope stores, not a 0–1 rate. Survey, chat, and web use `claude-haiku-5-5`. Os-app uses `claude-opus-4-8`. Effort is medium. Temperature is omitted. Chat, and any verifier that calls a model, uses judge `claude-opus-4-8` on both sides. Survey checks are rule-based. On every surface the two run ranges overlap.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|------------------|--------------|------------------------------|----------------------------|
| persona-openhands-sdk@1.0.0 | claude-haiku-5-5 | adherence points | 3 | 25 | 1.360 ± 0.023 | 1.467 ± 0.035 |
| persona-chat@1.0.0 | claude-haiku-5-5 | adherence points | 3 | 28 | 1.012 ± 0.031 | 1.000 ± 0.021 |
| persona-web@1.0.0 | claude-haiku-5-5 | adherence points | 3 | 32 | 1.260 ± 0.021 | 1.281 ± 0.018 |
| persona-computer-1@1.0.0 | claude-opus-4-8 | adherence points | 3 | 30 | 1.066 ± 0.033 | 1.078 ± 0.040 |

Raw runs, original then Harbor. Survey: (1.36, 1.32, 1.40) and (1.48, 1.40, 1.52). Chat: (0.964, 1.000, 1.071) and (1.036, 1.000, 0.964). Web: (1.281, 1.219, 1.281) and (1.312, 1.250, 1.281). Os-app: (1.033, 1.133, 1.033) and (1.133, 1.000, 1.100).

Claude Code (`persona-claude-code@1.0.0`, `claude-haiku-5-5`) was run on the Harbor side only, one pass: survey mean 1.440 (25 tasks) and web mean 1.281 (32 tasks). There is no upstream side and sample SEM is undefined at one run, so it is not a parity row.

Reproduction, from the repository root after generating the subset with `--split parity`:

```bash
# Harbor survey
PYTHONPATH=src/agentpersonabench/src harbor run -y \
  -a agentpersonabench.agents.persona_openhands_sdk:PersonaOpenHandsSdk \
  -m anthropic/claude-haiku-5-5 -e docker \
  -p datasets/agentpersonabench-parity -k 3 --ak reasoning_effort=medium

# Harbor web
PYTHONPATH=src/agentpersonabench/src harbor run -y \
  -a agentpersonabench.agents.persona_web:PersonaWeb \
  -m anthropic/claude-haiku-5-5 -e docker \
  -p datasets/agentpersonabench-parity-web -k 3

# Harbor chat. Judge is claude-opus-4-8 from the generated task.toml.
PYTHONPATH=src/agentpersonabench/src harbor run -y \
  -a agentpersonabench.agents.persona_chat:PersonaChat \
  -m anthropic/claude-haiku-5-5 -e docker \
  -p datasets/agentpersonabench-parity-chat -k 3

# Harbor os-app
PYTHONPATH=src/agentpersonabench/src harbor run -y \
  -a agentpersonabench.agents.persona_computer_1:PersonaComputer1 \
  -m anthropic/claude-opus-4-8 -e docker \
  -p datasets/agentpersonabench-parity-app -k 3 --ak max_steps=40

# Upstream, from an AgentPersonaBench checkout. One task path per line.
# python evaluation/run_task.py <task-path> --model haiku-5-5 --effort medium --seed 0
# Os-app uses --model opus-4-8. Repeat seeds 0, 1, and 2.
```

The upstream survey command must run `write_persona_prompt_file` with a Python that has PyYAML. `evaluation/src/lib/harbor_solve.sh` calls bare `python3` for that step. On this machine that interpreter is the eval venv, placed first on `PATH`. Details are in `parity_experiment.json`.

## Notes & Caveats

Emitted tasks keep upstream `instruction.md`, `persona.yaml`, `input/`, and `verifier.py` bytes. The verifier change is the `[[checks]]` path, from the benchmark `task.toml` to `tests/checks.toml`. Persona rendering reads that file, so a tested value such as `None` or `Unknown` stays in the prompt. `persona.yaml` is not copied into the image at build time. Survey does not upload it. Web and chat copy it from `tests/` only after the agent has finished. `persona-computer-1` uploads it to `/app/input/persona.yaml` when the agent starts, matching APB's mixin.

`tests/test.sh` deletes `/logs/verifier/reward.txt` before the verifier runs. The verifier then rebuilds `/input` and `/environment` from the pristine copies under `tests/`, which Harbor uploads only at grade time.

Excluded tasks, 159 of 2,460: survey 0, chat 3, web 63, os-app 93. The current oracle synthesizer has not produced and verified a full-score artifact for the excluded web and os-app tasks; this does not claim that no such artifact can exist. Three chat tasks (`debugger-first-chat`, `instrumentation-first-chat`, `risk-seeking-chat`) raise `OracleError` because the verifier has no HELD clause. Twelve chat verifiers are rule-based. Sixty chat oracles use hand-written calibrated turns (`chat_oracle_calibrated.txt`) because a rubric restatement was insufficient or unstable. The other chat oracles restate the verifier's own HELD sentences; that shows the judge accepts the restatement. `reviewstyle-web` uses the same calibrated review text as `reviewstyle-chat` and a real Playwright trace.

Web pages keep ground truth in `data-*` attributes and HTML comments inside the agent-visible image, as upstream does. `persona-web` does not read those attributes. A shell-capable agent can. Os-app catalogs under `/opt/task-environment/` are in the same position.

The Harbor oracle's latest trial is fully held on all 2,301 emitted tasks, with 0 exceptions. Rewards stay the verifier's point counts, so the mean of the raw rewards is 1.586, not a pass rate. LLM-judge trials in that run used `dashscope/deepseek-v4.1-flash` with thinking disabled, passed through `--verifier-env`. The 60 calibrated turns and the three chat exclusions were gated with `glm-4.7`, thinking disabled. Generated `task.toml` still pins `claude-opus-4-8`. That pin was not used for this oracle, because `ANTHROPIC_API_KEY` returns HTTP 401. The first pass scored 13 tasks short. Their oracle text, or a missing `tests/questionnaire.yaml`, was fixed, and the rerun scored those 13 fully held. Upstream commit `db1b8b493009811314421e7a1775c947762dc3b7`. Harbor 0.18.0.

`persona-computer-1` is Harbor's computer-1 with the persona prompt spliced in. It is not APB's vendored computer-1. The 5% os-app comparison uses this agent, and the three-run ranges overlap. When `ADHERENCE_TEMPERATURE` is unset the agent uses 0.7; the recorded opus runs did not set it, and `claude-opus-4-8` drops an explicit temperature.

An earlier one-task chat probe used `glm-4.7`. APB's vaccines transcript in that probe ends with an `[Ended: satisfied — ...]` turn, and Harbor's last turn is a normal goodbye. For DeepSeek, the Harbor chat bot uses a completion budget of 8192 because a 1024 budget returns empty `content` after `reasoning_content`. APB's bot budget stays 1024. The parity table above is the `claude-haiku-5-5` subset, not that probe.

The generated dataset lives in `datasets/agentpersonabench`, which is gitignored. The adapter PR contains the adapter. The tasks are in [harbor-datasets#270](https://github.com/harbor-framework/harbor-datasets/pull/270). Parity artifacts are in [parity-experiments discussion 274](https://huggingface.co/datasets/harborframework/parity-experiments/discussions/274).

## Installation / Prerequisites

```bash
cd src/agentpersonabench
uv sync
```

- Docker installed and running
- Harbor installed (`uv tool install harbor`)
- An AgentPersonaBench checkout for `--source`
- API keys for the agent and, for chat grading, `ANTHROPIC_API_KEY`. `glm-4.7` grading needs `ZAI_API_KEY` and `ZAI_API_BASE`. DashScope models need `DASHSCOPE_API_KEY`.

## Troubleshooting

- Chat reward 0 with `judge HTTP 401` means the Anthropic key was rejected. The verdict is not a content score. `ADHERENCE_JUDGE_MODEL` in the generated `task.toml` is still `claude-opus-4-8`.
- A GLM judge that returns empty text spent the token budget on reasoning. `tests/llm_client.py` disables thinking for model ids that contain `glm`.
- `run_agentpersonabench_parity.yaml` is the recorded web parity arm. Generate `datasets/agentpersonabench-parity-web` with `--surface web --split parity` before using it. Survey, chat, and os-app stay on their own commands in the parity section.

## Citation

```bibtex
@article{agentpersonabench2026,
  title={AgentPersonaBench: Benchmarking Persona-Driven User Simulation},
  author={Huang, Jintao and others},
  year={2026},
  url={https://arxiv.org/abs/2610.04379}
}
```

## Authors & Contributions

This adapter is developed by Jaden Hou (36116720+JianhengHou@users.noreply.github.com). Benchmark authors are listed in the upstream `CITATION.cff` and copied into each generated `task.toml`.

**Issues and Contributions:**

- Submit Issues and Pull Requests to the main repository
- Follow the project's coding style and commit guidelines

## Acknowledgement

API inference for the sampled parity runs used credentials already present in the local environment. No 2077AI parity-key grant was used.
