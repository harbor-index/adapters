# Engineering validation

Except for the two `parity-*.json` files described at the end of the list, these
artifacts are implementation checks, **not model parity**, and no model API
calls were made to produce them. The Harbor jobs ran on Harbor 0.27.0 (the PyPI
release) with the local Docker backend, with grading in the separate verifier
environment.

- `differential.json`: the pinned original AgentGym clients, wrappers and
  LMRL-Gym implementations compared with the adapter: 50 tasks, 350 scenarios,
  3,280 state comparisons, and 50 successful reference episodes. Reproduce with
  `bash scripts/run_upstream_check.sh` from the adapter directory.
- `harbor-oracle.json`: the full 50-task Docker job's unmodified aggregate result
  and selected per-trial fields: task checksum, verifier environment mode,
  verifier reward, and a summary of the transcript Harbor collected from the
  game sidecar.
- `harbor-nop-smoke.json`: Maze 00 and Wordle 00 with Harbor's no-op agent. Both
  must receive zero reward with no infrastructure exception.
- `harbor-wrong-submission.json`: the same two tasks with a known-wrong
  submission that loses the real episode and then plants a winning transcript
  and reward files in every location the agent container can write. Both must
  receive zero reward, and the collected transcript must be the sidecar's
  losing one.
- `harbor-native-smoke.json`: Maze 00 and Wordle 00 through the native Harbor
  agent using a local mock Chat Completions service that replays reference
  actions. It validates transport and conversation wiring, not model ability.
- `original-native-smoke.json`: the same two tasks through the original-side
  runner against the unmodified upstream `lmrlgym` server (built from
  `scripts/Dockerfile.original`), with the same mock service and reference
  actions. It validates the original-side protocol end to end, not model ability.
- `oracle-terminal.txt` and `oracle-terminal.svg`: captured Harbor terminal
  output of the oracle job and a rendering of that text for the PR. Harbor's
  trailing hints, which only print local job-directory paths, are omitted.
- `parity-compare.json`: output of `scripts/parity_compare.py` on the three
  parity runs per side: per-run scores, mean ± sample SEM and the range-overlap
  result, for all 50 tasks and per game. These come from real model runs; the
  main README describes how the model was called.
- `parity-cross-replay.json`: output of `scripts/cross_replay.py` on the same
  six runs. Every reply recorded on one side was replayed through the other
  side's environment without calling a model: 300 episodes, 1,537 steps, no
  mismatches.

After installing Harbor 0.27.0 and this adapter as described in the main README,
generate and run the full oracle job:

```bash
uv run agenteval-games --output-dir /tmp/agenteval-generated
harbor run -p /tmp/agenteval-generated -a oracle \
  --job-name agenteval-oracle-50 --jobs-dir /tmp/agenteval-harbor-jobs \
  --n-concurrent 4 --max-retries 0
```

Run the no-op negative control with a new job name:

```bash
harbor run -p /tmp/agenteval-generated -a nop \
  -i agenteval-maze-00 -i agenteval-wordle-00 \
  --job-name agenteval-nop-smoke-01 --jobs-dir /tmp/agenteval-harbor-jobs \
  --n-concurrent 2 --max-retries 0
```

Run the known-wrong submission through the same environment and verifier. The
helper rewrites only `solution/` in a dedicated copy of the two tasks, so the
task checksums differ from the oracle job while `environment/` and `tests/` are
byte-identical:

```bash
uv run agenteval-games --output-dir /tmp/agenteval-wrong \
  --task-ids agenteval-maze-00 agenteval-wordle-00
uv run python scripts/make_wrong_submission.py --dataset-dir /tmp/agenteval-wrong
harbor run -p /tmp/agenteval-wrong -a oracle \
  --job-name agenteval-wrong-submission-01 --jobs-dir /tmp/agenteval-harbor-jobs \
  --n-concurrent 2 --max-retries 0
```

In each trial directory, `artifacts/manifest.json` records the transcript as
collected from the `game` service, and `artifacts/game-evidence/transcript.json`
is the exact file the verifier replayed.

Run the two mock-model wiring checks, one per side. Neither contacts a model
provider. The original-side check needs the upstream server running; see the
main README for the checkout and `docker build` commands:

```bash
uv run python scripts/check_native_harbor.py \
  --harbor /tmp/agenteval-harbor/bin/harbor --dataset-dir /tmp/agenteval-generated \
  --jobs-dir /tmp/agenteval-harbor-jobs --job-name agenteval-native-smoke-01
docker run --rm -d -p 127.0.0.1:36001:36001 agenteval-original-server
uv run python scripts/check_native_original.py \
  --upstream-dir /path/to/AgentGym --environment-base-url http://127.0.0.1:36001 \
  --dataset-dir /tmp/agenteval-generated --output-dir /tmp/agenteval-original-smoke \
  --run-name original-native-smoke-01
```

Harbor's static task checks report no findings on any of the 50 generated tasks
with the default rules. Its Compose checks need a recent Docker Compose plugin:

```bash
harbor lint /tmp/agenteval-generated/*
```

Job timestamps are preserved exactly as emitted by Harbor. Trial configuration,
host paths, and environment variables are not included in the selected per-trial
fields. The full parity and oracle job directories are published with the
parity artifacts linked from the main README.
