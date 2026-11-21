# AgentEval Games → Harbor Adapter

## Overview

This adapter converts the complete **Maze test split (25 tasks)** and **Wordle test
split (25 tasks)** from AgentGym's AgentEval dataset into 50 interactive Harbor
tasks. These are two selected splits, not the full AgentGym suite. The adapter
preserves the published test IDs, original instructions, seeded game behavior,
action parsing, and final-step reward.

Status: **WIP**. All 50 reference solutions passed through the real Harbor/Docker
harness with zero exceptions, graded by a separate verifier on Harbor 0.26.0.
Model parity, Harbor team review, and dataset registration remain pending. The
empty `parity_experiment.json` records that no model experiment has been run; it
must not be interpreted as a passing parity result.

## What is AgentEval Games?

[AgentGym](https://github.com/WooooDyy/AgentGym) evaluates agents across interactive
environments. Its [AgentEval dataset](https://huggingface.co/datasets/AgentGym/AgentEval)
provides the two 25-item test lists used here. Maze tasks navigate a fixed double-T
maze from different starting positions. Wordle tasks play LMRL-Gym's 40-word,
six-attempt candidate-state game, with a distinct random seed per test item.

The score is the original **last-step reward**, averaged across episodes: 1 for
success, 0 otherwise. Maze allows 15 model turns in AgentGym's published test
runner; Wordle has an eight-turn evaluator limit and terminates internally after
at most six guesses.

## Adapter Features

- Offline generation from pinned, bundled test lists, prompts, vocabulary, and
  licensed game sources. Stable IDs are `agentgym/agenteval-maze-00` through `-24`
  and `agentgym/agenteval-wordle-00` through `-24`.
- An isolated game sidecar holds the seed, authoritative state, and trajectory.
  The agent container receives only the terminal `game` client. Every submitted
  action, including an invalid action, consumes an original benchmark turn.
- Grading runs in a **separate verifier environment** built from
  `tests/Dockerfile`, never in the agent's container. The sidecar records the
  authoritative transcript on its own filesystem. After the agent phase Harbor
  stops the agent container, collects the transcript directly from the sidecar,
  and the verifier replays every action against its own copy of the benchmark
  code. The agent container shares no volume with the sidecar or the verifier,
  so workspace files, forged rewards or transcripts, rewritten clients, and
  forged HTTP responses cannot change the graded episode.
- Maze reference solutions use shortest paths. Wordle reference solutions use
  the first remaining vocabulary candidate and update candidates from public
  feedback. They do not require access to the next random target.
- The terminal transport supports standard Harbor agents. A native conversation
  agent and original-side runner preserve AgentGym's user/assistant initialization
  and action/observation dialogue for coordinated model parity.

## Generated Task Structure

```text
agenteval-maze-00/
├── task.toml
├── instruction.md
├── environment/
│   ├── Dockerfile                 # agent client only
│   ├── Dockerfile.game            # private game sidecar
│   ├── docker-compose.yaml
│   ├── game.py
│   ├── config.json
│   ├── runtime/
│   └── data/
├── solution/
│   ├── solve.sh
│   ├── solve.py
│   └── actions.json
└── tests/
    ├── Dockerfile                 # separate verifier image
    ├── test.sh
    ├── verify.py
    ├── config.json
    ├── runtime/
    └── data/
```

Only the files explicitly copied by `environment/Dockerfile` enter the agent
image. The game configuration and game implementation enter the separate sidecar.
Harbor supplies `solution/` to oracle trials. `tests/` is the build context of
the verifier image; it is never uploaded into the agent container.

`task.toml` sets `[verifier].environment_mode = "separate"` and declares one
artifact, `{ source = "/game-evidence/transcript.json", service = "game" }`.
That file is the only input the verifier takes from a trial, and Harbor keeps a
copy at `artifacts/game-evidence/transcript.json` in the trial directory.

## Run Evaluation / Harness

Registry evaluation is not available until the Harbor team publishes the dataset.
The intended registry name is `agentgym/agenteval-games`; no registry version is
claimed by this PR.

For local oracle verification, from the adapters repository root:

```bash
harbor run -p /tmp/agenteval-games -a oracle --n-concurrent 4
```

Or generate into `datasets/agenteval-games` and use the checked-in reference config:

```bash
harbor run -c src/agenteval-games/run_agenteval-games.yaml
```

On November 20, 2026, the full oracle run completed **50/50 tasks with reward 1**
(mean 1.000, zero exceptions, zero retries) in 9 minutes 44 seconds on Harbor
0.26.0, with every trial graded in the separate verifier environment. Two
negative controls on one task from each game returned **0 on both**: a
no-action agent, and a known-wrong submission that loses the real episode and
then plants a winning transcript and reward files inside the agent container.
See [full oracle results](validation/harbor-oracle.json),
[no-action controls](validation/harbor-nop-smoke.json),
[wrong-submission controls](validation/harbor-wrong-submission.json), and the
[captured terminal output](validation/oracle-terminal.txt).

![Actual Harbor oracle command output](validation/oracle-terminal.svg)

Each task requires Docker Compose because the private game service must remain
outside the agent container, and a Harbor release that supports separate
verifiers and per-service (`service = ...`) artifact collection. The tasks are
validated on Harbor 0.26.0 with the local Docker backend; single-container and
cloud backends have not been validated. A task does not require a GPU or a
model-based judge.

## Usage: Create Task Directories

```bash
cd src/agenteval-games
uv sync --locked
uv run agenteval-games --output-dir /tmp/agenteval-games
```

The full default emits exactly 50 tasks. Generate one split or a small subset with:

```bash
uv run agenteval-games --output-dir /tmp/agenteval-wordle --split wordle
uv run agenteval-games --output-dir /tmp/agenteval-smoke --task-ids agenteval-maze-00 agenteval-wordle-00
uv run agenteval-games --output-dir /tmp/agenteval-smoke --task-ids lmrlgym_maze_0 lmrlgym_wordle_0 --overwrite
uv run agenteval-games --output-dir /tmp/agenteval-first --limit 2
```

`--task-ids` accepts registry-safe names, namespaced names, or upstream item IDs.
Unknown IDs fail before any task is written. Existing selected task directories
require `--overwrite`. Generation preserves the upstream order, so `--limit 2`
selects the first two Maze tasks rather than a balanced sample.

## Comparison with Original Benchmark (Parity)

**No model parity has been run.** Agents, exact model IDs, temperature, generation
limits, number of runs, and API provisioning must be coordinated with the Harbor
team first. The required sequence is a 5–10-task sanity check on both sides, one
full 50-task run on both sides, then at least two total runs per side (three
preferred). Report each side's mean ± sample SEM and raw run arrays. Deterministic
replay and oracle success are engineering checks, not model parity.

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|
| Pending coordination | Not selected | final-step reward | 0 | 50 planned | Not run | Not run |

Run deterministic checks without model API calls:

```bash
# Lightweight checks; optional Harbor/upstream integration checks may be skipped.
uv run python -m unittest discover -s tests -v
# All 24 tests, with the pinned Harbor environment and upstream checkout available.
AGENTEVAL_UPSTREAM_DIR=/path/to/AgentGym /tmp/agenteval-harbor/bin/python -m unittest discover -s tests -v
# Independent, pinned Python 3.9.12 comparison with the original implementation.
bash scripts/run_upstream_check.sh
# Actual Harbor + Docker, but a local deterministic mock model (no API charges).
uv run python scripts/check_native_harbor.py \
  --harbor /tmp/agenteval-harbor/bin/harbor --dataset-dir /tmp/agenteval-games \
  --jobs-dir /tmp/agenteval-harbor-jobs --job-name native-smoke-01
```

The complete unit/integration suite passed **24/24 with no skips**. A separate
real Harbor/Docker smoke test also exercised the native agent on one Maze and one
Wordle task using a **local mock model endpoint**, achieving 2/2 successful
reference episodes with no exceptions ([results](validation/harbor-native-smoke.json)).
This validates native-agent wiring and
failure recording, not a model's capability or model parity. The smoke script
requires a fresh job name and records its configuration, mock requests, and
assertion result beside the Harbor job directory.

The independent checker loads the pinned original game implementations and
AgentGym wrappers, compares initial prompts and step responses, and exercises
successful, invalid, exhausted, repeated-letter, parsing, and truncated episodes.
It passed **350 scenarios and 3,280 state comparisons across all 50 tasks**,
including 50/50 original-engine reference solutions. The machine-readable result
is in [validation/differential.json](validation/differential.json).

After the team agrees on API settings, install the adapter in the same Python
environment as Harbor and fill in `parity_agenteval-games.yaml`. Its model name is
a deliberate placeholder; its numeric values are examples, not approved settings.
The API key stays on the host via `OPENAI_API_KEY`; `OPENAI_BASE_URL` optionally
selects an OpenAI-compatible endpoint. Use identical values on both sides.

```bash
# Harbor native conversation side; model calls are billable.
/tmp/agenteval-harbor/bin/harbor run -c parity_agenteval-games.yaml
```

For the original side, check out AgentGym at the commit below, initialize its
pinned LMRL-Gym submodule, and follow the original
[environment setup instructions](https://github.com/WooooDyy/AgentGym/blob/3ef9235d23e68e7c2920c5422ad957dc8ced5c6c/agentenv-lmrlgym/README.md).
Start the unmodified upstream `lmrlgym --host 127.0.0.1 --port 36001` server from
that Python 3.9.12 environment. Then run the shared native policy from this adapter:

```bash
uv run python -m agenteval_games.original_runner \
  --upstream-dir /path/to/AgentGym \
  --environment-base-url http://127.0.0.1:36001 \
  --model 'TEAM_APPROVED_EXACT_MODEL_ID' \
  --temperature 0 --max-tokens 2048 --top-p 1 \
  --request-timeout-sec 120 --environment-timeout-sec 30 --episode-timeout-sec 1500 \
  --run-id 1 --output-json /path/to/original-run1.json
```

Replace the illustrative model/settings with the agreed values. Use `--task-ids`
with the same selected IDs on both sides for sanity checks, and unique output/run
names for repeats. The original runner verifies the local source commits and
prompts, but the operator must ensure that the HTTP server was actually launched
from that checkout. It does not replay the adapter engine as the original side.
Both runners log model requests/responses, effective settings, usage, and failures;
uncertain HTTP steps are not retried or silently scored as losses. The prospective
shared policy also imposes an explicit episode deadline (illustratively 1,500
seconds, within Harbor's 1,800-second agent timeout). Unlike upstream APIAgent's
unbounded error retries, errors and deadline expiration stop the run without a
score. This policy is identical on both sides and is not presented as a
reproduction of historical model scores.

The [original benchmark](https://github.com/WooooDyy/AgentGym/tree/3ef9235d23e68e7c2920c5422ad957dc8ced5c6c)
and the bundled provenance manifest identify the original side. Model result
uploads, a parity fork/PR, dataset PR, and registry publication do not exist yet.

## Notes & Caveats

- **Wordle is adaptive, not standard fixed-target Wordle.** Each valid guess draws
  a new word from the current candidate set using the seeded RNG. Invalid guesses
  consume an attempt but not a random draw. Repeated letters use the upstream
  per-character position rules rather than conventional Wordle multiplicities.
  The original prompt is retained even though it describes a hidden word.
- The original `</s>` handling removes five characters although the suffix is
  four characters long. This adapter preserves that behavior, case sensitivity,
  the first `Action:` segment, and ASCII-space-only Wordle normalization. It does
  not silently fix upstream behavior.
- Maze's original engine supports 50 internal steps, but the published AgentGym
  **test evaluation stage** applies 15 turns. The adapter uses that cap and keeps
  the original `done` flag separate from adapter `episode_finished` truncation.
  Wordle's six-guess internal limit takes precedence over its eight-turn cap.
- The terminal adapter adds commands and public transport metadata to make the
  task usable by shell agents. These transport instructions are not a claim of
  prompt parity with the original native conversation. Native parity uses the
  original role messages and observation text, without transport instructions.
- Source/runtime pins: AgentGym `3ef9235d23e68e7c2920c5422ad957dc8ced5c6c`,
  LMRL-Gym `83abeedb3a461c3d7d20572b73318a259d85f2ac`, AgentEval
  `1f090d52ef01a83a889da02699e34a500e35a274`, Python **3.9.12** inside tasks,
  and Harbor **0.26.0**. The Python base image is digest-pinned in all three
  Dockerfiles (agent, sidecar, verifier). Adapter generation uses Python 3.11+.
- Exclusions: all other AgentGym environments, their training splits, and any
  expanded/randomized Maze or Wordle instances. The two selected test splits
  themselves are complete; no task was dropped or sampled.
- The Wordle core is vendored with only its `Cache` import made relative. Maze's
  fixed-map transition and observation logic are extracted to remove unrelated
  training, JAX, and image-generation dependencies; differential checks cover
  their reachable behavior under the 15-turn test protocol.
- Original AgentGym and LMRL-Gym MIT notices are included. AgentEval's pinned
  dataset has no separate license declaration in its card; the adapter records
  that provenance without assigning it an invented dataset license.
- The grading boundary is Harbor's separate verifier plus Docker's container
  separation: the agent container reaches the sidecar only through its HTTP API
  and shares no volume with it, and Harbor reads the transcript from the
  sidecar itself. The transcript is not cryptographically signed, because its
  authenticity comes from that collection path rather than from anything the
  agent container handles. A compromised container runtime or Harbor host is
  outside this boundary.
- `parity_experiment.json` intentionally contains `[]`. The repository's completed
  adapter validator rejects an empty experiment array; this is an explicit WIP
  gap until real, coordinated experiments can populate it. No fabricated scores,
  matching agents, costs, or publication links are supplied.

## Installation / Prerequisites

The generator requires Python 3.11+ and `uv`; evaluation additionally requires
Docker with Compose and the external Harbor CLI. To reproduce the compatibility
baseline in a separate environment:

```bash
uv venv --python 3.12 /tmp/agenteval-harbor
uv pip install --python /tmp/agenteval-harbor/bin/python 'harbor==0.26.0'
uv pip install --python /tmp/agenteval-harbor/bin/python -e .
/tmp/agenteval-harbor/bin/harbor --version
```

Run these commands from `src/agenteval-games`. Task generation needs no network
access after dependencies are installed. Docker image builds need access to the
pinned Python image; the sidecar and verifier images also install one pinned
Python package. Standard Harbor agents install
their own CLI prerequisites and need their usual model credentials; oracle and
deterministic checks do not use model credentials.

## Troubleshooting

- **Episode already finished:** create a new Harbor trial. There is deliberately no
  reset endpoint that would let an agent retry the same hidden episode.
- **Invalid Wordle guess:** use one of the 40 supplied lowercase vocabulary words.
  Invalid guesses still count toward the six-attempt limit.
- **Reward stays zero:** inspect `artifacts/game-evidence/transcript.json` in the
  trial directory and the original step observations. A final answer file or
  edited local score is not evaluated.
- **Transcript is missing or replay fails:** check the `game` entry in
  `artifacts/manifest.json` and `verifier/test-stdout.txt`. Do not switch the
  verifier to shared mode or accept a transcript from the agent container as a
  workaround.
- **Game host is unavailable:** run with the Docker backend and check the game
  sidecar health. The terminal client uses the Compose service name `game`.
- **Structural validator reports missing parity evidence:** expected for this WIP;
  complete coordinated experiments rather than inserting placeholder metrics.

## Citation

```bibtex
@misc{xi2024agentgym,
  title={AgentGym: Evolving Large Language Model-based Agents across Diverse Environments},
  author={Zhiheng Xi and Yiwen Ding and Wenxiang Chen and Boyang Hong and Honglin Guo and Junzhe Wang and Dingwen Yang and Chenyang Liao and Xin Guo and Wei He and Songyang Gao and Lu Chen and Rui Zheng and Yicheng Zou and Tao Gui and Qi Zhang and Xipeng Qiu and Xuanjing Huang and Zuxuan Wu and Yu-Gang Jiang},
  year={2024},
  eprint={2406.04151},
  archivePrefix={arXiv},
  primaryClass={cs.AI}
}
```

## Authors & Contributions

AgentGym and AgentEval: Zhiheng Xi, Yiwen Ding, Wenxiang Chen, and the AgentGym
contributors. The underlying LMRL-Gym environments are credited to Charlie Snell
and the LMRL-Gym contributors, with their MIT notice retained.

Harbor adapter: Zonglin DI ([ElegantLin](https://github.com/ElegantLin)).
