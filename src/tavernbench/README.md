# TavernBench → Harbor Adapter

## Overview

This adapter converts [TavernBench](https://github.com/MCG-NJU/AutoTavern/blob/main/docs/TAVERNBENCH.md) into Harbor tasks. TavernBench asks questions about Hearthstone Battlegrounds: which actions are legal, how a combat will end, where to place a minion, what to buy, how to play a turn. Every task is a self-contained prompt answered with one JSON object.

- **Source:** [`MCG-NJU/AutoTavern`](https://github.com/MCG-NJU/AutoTavern), `tavernbench/data/`, pinned to commit `fb61201a557ddd36a411cf44aac51ce4d9b606ce` (tag `v1.0.0`). The adapter checks the SHA-256 of every file it reads.
- **Paper:** Ma and Wang, [*Build, Don't Play: Self-Improving LLM Research Agents Develop a Hearthstone Battlegrounds AI That Rivals Top Players*](https://arxiv.org/abs/2610.33845).
- **License:** MIT for the tasks, labels and scoring code. The card names and card texts inside the prompts are game content of Blizzard Entertainment; see the upstream [NOTICE](https://github.com/MCG-NJU/AutoTavern/blob/main/NOTICE).
- **Adapted size:** 368 tasks, the `eval` split. This is the fixed set every system answers in the paper: 8 tasks from each of 37 scenarios, plus all tasks of the three play-out scenarios D4 (16), N1 (29) and N2 (27). `--split full` generates all 960 tasks of the benchmark; `eval` is a subset of it.
- **Metric:** accuracy, the share of tasks whose answer is correct under the task's scoring rule. Each task has a reward of 1 or 0.

The prompts are not changed. The adapter puts the benchmark's one-line system prompt in front of each prompt and adds one closing sentence that tells the agent to write the JSON object to `/app/answer.json`.

## What is TavernBench?

TavernBench is the benchmark released with AutoTavern. It has 960 tasks in 40 scenarios and eight categories (rules, economy, combat, positioning, ordering, targets, selection, strategy), all for patch 36.6.3. A prompt holds a summary of the game rules, the player's view of the current state, the rules text of every card in that view, a question and the answer format.

The labels come from TavernGym, the simulator released with the benchmark: rule tasks are labelled by executing the stated actions, and decision tasks by simulating hundreds or thousands of combats after each option. The paper reports the mean accuracy over the 30 play scenarios ("Play") and over the 10 rule-execution scenarios ("Rules").

Each label names one scoring rule:

| Scoring | An answer is correct when | Tasks (eval / full) |
|---|---|---|
| `exact` | it equals the label (strings case-insensitively) | 232 / 552 |
| `set` | it has the same elements as the label | 8 / 24 |
| `brier` | the most likely outcome matches and the Brier score is at most 0.1 | 16 / 48 |
| `numeric_rel` | the error is at most max(1, tol × label) | 8 / 24 |
| `numeric_abs` | the error is at most tol | 8 / 24 |
| `value_map`, `value_map_str`, `value_json` | the chosen option's simulated value is within tol of the best option's | 96 / 288 |

## Adapter Features

- Downloads the two JSONL files of the requested split at the pinned commit, verifies their SHA-256 and caches them. `--source-dir` reads a local AutoTavern checkout instead.
- Writes one Harbor task per benchmark item, in the benchmark's order. Task ids are the upstream ids in lower case (`R1-000` becomes `r1-000`).
- Keeps the label out of the agent's reach: it is stored in `tests/`, which Harbor copies into the container only for verification.
- Verifies with the benchmark's own scoring rules. `tests/score.py` holds `parse` and `score_item` from `tavernbench/tavernbench/score.py` with the same behaviour.
- Records the graded score (0 to 1) and the parsed answer in `/logs/verifier/score.json` next to the reward.
- Includes an oracle that writes an answer built from the label. For the `value_*` rules it picks the option with the highest value.
- Has no dependencies outside the Python standard library, in the adapter and in the verifier. The task image is `python:3.13-slim` plus the packages command-line agents need for their own installation (curl, git, Node.js, npm, procps), so agent setup does not run `apt` in every trial.

## Generated Task Structure

```text
datasets/tavernbench/
├── r1-000/
│   ├── task.toml             # name, benchmark authors, scenario, category, scoring rule
│   ├── instruction.md        # system line + prompt + where to write the answer
│   ├── environment/
│   │   └── Dockerfile        # python:3.13-slim, working directory /app
│   ├── solution/
│   │   └── solve.sh          # writes the reference answer to /app/answer.json
│   └── tests/
│       ├── test.sh           # runs score.py and writes /logs/verifier/reward.txt
│       ├── score.py          # the benchmark's scoring rules
│       └── label.json        # the task's label
├── r1-001/
└── ...
```

Every task is named `mcg-nju/tavernbench__<task id>` in `task.toml`. The `[metadata]` table records the upstream id, scenario, category and scoring rule, so results can be grouped the way the paper groups them.

## Run Evaluation / Harness

Once the dataset is published to the Harbor registry (planned name `mcg-nju/tavernbench`), run it with:

```bash
harbor run -d mcg-nju/tavernbench -a oracle
harbor run -d mcg-nju/tavernbench -a <agent_name> -m "<model_name>"
```

Until then, generate the tasks locally. Harbor is an external CLI; install it with `uv tool install harbor`. From the repository root:

```bash
cd src/tavernbench
uv run tavernbench --output-dir ../../datasets/tavernbench
cd ../..
```

Run the oracle (Docker must be running; no model credentials are needed):

```bash
harbor run -c src/tavernbench/run_tavernbench.yaml
# Equivalent:
harbor run -p datasets/tavernbench -a oracle
```

Run an agent, or a single task:

```bash
harbor run -p datasets/tavernbench -a <agent_name> -m "<model_name>"
harbor trial start -p datasets/tavernbench/r1-000 -a <agent_name> -m "<model_name>"
```

The mean reward of a job is the accuracy over its tasks. Each trial's `verifier/score.json` holds the graded score and the answer that was scored.

## Usage: Create Task Directories

```bash
cd src/tavernbench
uv run tavernbench --output-dir ../../datasets/tavernbench
```

Flags:

- `--output-dir` — directory to write the tasks to (default: `datasets/tavernbench` at the repository root)
- `--limit N` — generate only the first N tasks
- `--overwrite` — replace task directories that already exist (they are skipped otherwise)
- `--task-ids ...` — generate only these tasks; accepts upstream ids (`R1-000`), task ids (`r1-000`) and full task names
- `--split eval|full` — `eval` (default) is the paper's 368-task evaluation set, `full` is all 960 tasks
- `--source-dir PATH` — root of a local AutoTavern checkout, used instead of downloading
- `--cache-dir PATH` — where downloaded source files are kept (default: `~/.cache/harbor-adapters/tavernbench`)

A small subset for a smoke test:

```bash
uv run tavernbench --output-dir ../../datasets/tavernbench-smoke --limit 5
```

## Comparison with Original Benchmark (Parity)

Parity experiments have not been run yet. The agent, the model and the number of runs are to be agreed with the Harbor team before any run.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|----------------|--------------|--------------------------------|----------------------------|
| pending | pending | accuracy (%) | pending | 368 tasks (100% of the eval split) | pending | pending |

**Planned setup (Scenario 2).** The original runners make one model call per task and score the reply. One of them, `run_claude`, already uses the Claude Code CLI, but as a single call without tools. A fork of AutoTavern will add a runner that starts Claude Code inside the image of the Harbor task, gives it the text of the Harbor `instruction.md`, reads `/app/answer.json` and scores it with the unchanged `tavernbench.score`. Both sides then use the same pinned Claude Code version, model, timeout and tool settings, with web tools disabled. Proposed order: a 10-task sanity check on both sides, one full run on both sides, then three runs per side. The fork and its commands will be linked here.

Harbor side, from the repository root:

```bash
cd src/tavernbench && uv run tavernbench --output-dir ../../datasets/tavernbench && cd ../..
harbor run -p datasets/tavernbench -a claude-code -m "<model>" \
  --ak version=<claude-code version> --ak disable_web_search=true
```

Checks so far:

- **Oracle with Harbor:** pending. The result and the Harbor version will be recorded here.
- **Verifier flow outside Docker:** for all 368 `eval` tasks and all 960 `full` tasks, running `solution/solve.sh` and then `tests/test.sh` gives reward 1. A missing answer file, a file without a JSON object and a deliberately wrong answer each give reward 0, and the reference answer inside a Markdown code fence gives reward 1.
- **Oracle on the original side:** the unchanged upstream scorer (`python -m tavernbench.score`) gives accuracy 1.000 on the oracle answers of all 368 `eval` tasks and all 960 `full` tasks.
- **Task format:** the task models of Harbor 0.26.0 and 0.27.0 load all generated tasks of both splits; their names are unique.
- **Scoring port:** on all 5,135 model answers released upstream (`tavernbench/results/`, 17 systems), `tests/score.py` gives the same result and the same graded score as `tavernbench.score` does for the same reply. With the original per-call checks described under Notes & Caveats added, the released answers give a Play accuracy of 91.4% for TavernAgent and 63.2% for the best LLM; the paper's abstract reports 91% and at most 63%.

## Notes & Caveats

- **Prompt.** The system line and the prompt are the benchmark's text. The only addition is the closing `## Output` section that names the answer file. The planned parity runs give the original side the same text.
- **Per-call checks are not applied.** The original scorer zeroes a reply when the API call ended early (`finish_reason` of `length`, `timeout` and similar) or spent more than 4,096 output tokens, reasoning included. These checks belong to a single model call and have no counterpart for a file written by an agent. They matter for the published numbers: in the released answer files they turn 102 otherwise correct answers of one system into failures, and 39 of another. Harbor scores of tool-using agents are therefore not comparable with the paper's table of direct model calls.
- **Agents can use tools.** The benchmark was designed as one model call with a small output budget. In Harbor an agent may write and run code before answering. The task image has Python and no game simulator.
- **Public labels.** The labels and the released model answers are public in the upstream repository, and the task network is public because agents need it to reach their model. Disable web tools when evaluating (for Claude Code, `--ak disable_web_search=true`) and inspect trajectories for downloads.
- **Tolerances.** Labels of simulation-based tasks carry a tolerance for sampling noise, so several options can be correct in a `value_*` task. The verifier applies the label's tolerance as the original scorer does.
- **Frozen at patch 36.6.3.** The upstream simulator has since moved to 37.0.2. The benchmark's tasks and labels were not recomputed, and this adapter uses them as released.
- **Not included.** The paper's tool-use protocol (`run_tools`, which lets a model try plays in the simulator from a saved state) and the saved states (`states.jsonl.gz`) are not part of this adapter.
- **`eval` and `full`.** The registered dataset is planned to be the 368-task `eval` split, which is what the paper's results use. The 960-task `full` split is available with `--split full`.
- **Game content.** Card names, card texts and other game data in the prompts are the property of Blizzard Entertainment. The upstream project describes itself as independent, non-commercial research; see its NOTICE.

## Installation / Prerequisites

- Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/) for the adapter. It has no third-party dependencies.
- Harbor CLI, installed separately: `uv tool install harbor`.
- Docker, running, for Harbor trials.
- Network access to `raw.githubusercontent.com` the first time tasks are generated, or a local AutoTavern checkout passed with `--source-dir`.
- Model credentials for agent runs. The oracle needs none.

## Troubleshooting

- **`Could not download ...`** — clone `https://github.com/MCG-NJU/AutoTavern`, check out `fb61201a557ddd36a411cf44aac51ce4d9b606ce` and pass the checkout with `--source-dir`.
- **`does not match the pinned TavernBench release`** — the source files differ from the pinned commit. Use that commit; the task set is fixed to it.
- **`wrote 0 task(s), skipped N existing`** — the output directory already has these tasks. Add `--overwrite` to regenerate them.
- **Reward 0 for an answer that looks right** — open the trial's `verifier/score.json`. `status` says whether the file was missing or held no JSON object, and `answer` shows what was parsed. Types matter: a position must be a number, not a string.

## Citation

```bibtex
@misc{ma2026build,
  title  = {Build, Don't Play: Self-Improving {LLM} Research Agents Develop a Hearthstone
            Battlegrounds {AI} That Rivals Top Players},
  author = {Ma, Changlian and Wang, Limin},
  year   = {2026},
  eprint = {2610.33845},
  archivePrefix = {arXiv},
  primaryClass = {cs.AI},
  url    = {https://arxiv.org/abs/2610.33845v1}
}
```

## Authors & Contributions

This adapter is developed and maintained by [Ming Yin](mailto:ming.yin@duke.edu).

**Issues and Contributions:**
- Submit issues and pull requests to the main repository
- Follow the project's coding style and commit guidelines
