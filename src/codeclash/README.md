# CodeClash → Harbor Adapter

## Overview

This **working draft (v0.2.0)** generates one Harbor descriptor for each of CodeClash's
**21 substantive public software/game arenas**. A shared host coordinator runs native
PvP with named multiplayer rosters, arena-specific budgets and transparent competition.
Rounds, simulations, matchups and repeated tournaments never increase the arena count.

The catalog contains **six published model PvP presets and 15 upstream diagnostic
presets**. The pinned upstream published suite contains 168 configurations (28 model
pairs across six arenas); the catalog selects one pair per published arena. All 21
images build and all 21 arenas complete no-model Harbor smoke runs
with native scorer replay and separate verification. This establishes integration
coverage, not model quality: there is **no oracle pass, model parity result or registry
publication**. Grader isolation remains a high-priority blocker, and inherited native
score-attribution defects in Halite2/Halite3 are preserved and disclosed below.

Source: [CodeClash](https://github.com/CodeClash-ai/CodeClash), pinned to
`f0694c64ecf6abfca2bc867bad2de9333fef5be8`. The harness is MIT-licensed; individual arena
licenses and dependencies are separate. Physical/embodied and video/audio tasks are
outside the contribution scope. The synthetic `Dummy` arena is excluded.

## What is CodeClash?

[CodeClash: Benchmarking Goal-Oriented Software Engineering](https://arxiv.org/abs/2511.00839)
evaluates coding agents that improve programs through repeated competition and feedback.
Native PvP begins with baseline round 0. Players then edit persistent codebases
concurrently, compete again, and receive competition logs before the next edit. The
harness retains code history, agent trajectories, validity, arena scores and winners.

Single-player training and ladder are separate upstream workflows with different round
and carry-over semantics. This adapter implements PvP, including multiplayer and
transparent PvP; it does not reinterpret those other workflows as PvP.

## Adapter Features

- Stable `codeclash/<arena>` identities; standard generator flags, `--list-arenas`, and
  repeatable `--config` for full native YAML/JSON configurations.
- `codeclash.harbor_agent:CodeClashAgent` starts native CodeClash in a separate Python
  process, preserving its resolved configuration and avoiding the shared `codeclash`
  import name. Native `PvpTournament` manages persistent editors, feedback, competition
  and transparent opponent-code snapshots.
- Schema2 manifests retain configuration hashes, source pins, budgets, rosters and
  prompts. The generic image builder uses the full tracked upstream build context,
  including bundled runtimes, and pins cloned source repositories.
- Dynamic ownership: one arena container plus one container per named player, with
  cancellation/startup cleanup audits. Native containers receive no host model API keys
  or host Docker socket. Harbor transfers host-produced evidence to a separate verifier.

| Preset origin | Arena IDs | Meaning |
|---|---|---|
| `published_pvp` (6) | `battlesnake`, `corewar`, `halite`, `huskybench`, `robocode`, `robotrumble` | Resolved upstream model PvP configurations; no Harbor parity claim |
| `upstream_diagnostic` (15) | `ants`, `battlecode23`, `battlecode24`, `battlecode25`, `bomberland`, `bridge`, `chess`, `cyborg`, `figgie`, `gomoku`, `halite2`, `halite3`, `lightcycles`, `paintvolley`, `scml` | Native diagnostic configurations; not published model benchmark results |

Full configurations, Dockerfile identities and arena source pins are in
[`catalog.json`](src/codeclash/catalog.json). Bridge requires four players; Figgie
supports four or five; Bomberland requires an even simulation budget.

| Profile | Native configuration | Purpose |
|---|---|---|
| `benchmark` | Preserves selected players, prompts, rules and budgets | Executes that configuration; a diagnostic preset stays diagnostic |
| `smoke` | Two edit rounds plus baseline, small arena-appropriate budgets, scripted editors | No-model checks of edits, persistence, feedback and evidence handoff |

`model_name` and `opponent_model_name` override models in configuration order only for
exactly two native `mini` players. Other rosters use `player_models`, mapping existing
native player names to model names. Overrides cannot convert `dummy` players into
models; supply a complete native model configuration with `--config`. Mixed positional
and named overrides, and all model overrides in smoke, are rejected.

Metrics retain each player's native score per edit round and its arithmetic mean,
excluding baseline round 0. Two-player tertiary scores (win 1, tie 0.5, loss 0) are added
only when the winner representation supports them. Team winners and multiplayer scores
are preserved. These are not Elo ratings. Smoke, bundled diagnostic presets, and custom
configurations without model players produce metrics prefixed with `diagnostic_`.
Native Mini players using only built-in deterministic test models also remain
diagnostic, even in the `benchmark` profile. Their agent loop can validate execution
without establishing model quality or provider compatibility.
Native scores have arena-specific units and should not be pooled across unrelated arenas.
Two-player tertiary scores must be compared for a fixed evaluated model against a
fixed opponent; averaging both players always produces 0.5.

## Generated Task Structure

```text
datasets/codeclash/
├── ants/
├── battlesnake/
├── bridge/
│   ├── task.toml                 # codeclash/bridge; one arena item
│   ├── instruction.md
│   ├── environment/
│   │   ├── Dockerfile            # Trusted evidence handoff image
│   │   ├── docker-compose.yaml
│   │   └── task-config.json      # Full native config and source provenance
│   ├── solution/solve.sh         # Explicit failure: oracle policy unresolved
│   └── tests/
│       ├── Dockerfile            # Separate verifier
│       ├── docker-compose.yaml
│       ├── task-config.json
│       ├── test.sh
│       ├── contract.py
│       └── evidence.py
└── ...                          # 21 directories when all arenas are selected
```

The runner writes `metadata.json`, `runner-summary.json`, `containers.json`, provenance,
compressed round logs and player edit/trajectory evidence. The coordinator uploads it
to `/codeclash/evidence`; Harbor transfers it to a fresh verifier. Rounds and tournaments
remain records within the arena item.

## Run Evaluation / Harness

### Running with Datasets Registry

This WIP has no registry entry. Use local descriptors for development.

### Local diagnostic smoke

Install both Python environments and build the selected arena image as described below.
From the adapters repository root, generate a targeted smoke dataset, replace both path
placeholders in `run_codeclash_smoke.yaml`, and run:

```bash
uv run --project src/codeclash codeclash --profile smoke --task-ids bridge \
  --output-dir datasets/codeclash-smoke
/path/to/harbor-venv/bin/harbor run -c src/codeclash/run_codeclash_smoke.yaml
```

Build every selected arena's image first. The coordinator chooses
`codeclash-adapter/<arena>:f0694c64ecf6`, allowing mixed-arena datasets without a global
image override. A smoke must retain configured rounds, scripted edit/feedback assertions
and scorer evidence, pass the separate verifier, and verify cleanup of all native
containers. There is no universal simulation-log count: arenas batch matches or retain
selected replay traces differently.

Verification on macOS arm64, Docker 29.5.3 and Harbor 0.23.0 at the pin below:

| Check | Result |
|---|---|
| Unit tests | 73 passed: generation/configs, coordinator, catalog/build recipes, evidence and runtime |
| Native configuration fidelity | All 21 catalog snapshots equal the pinned upstream include resolver's output |
| Generation / Harbor task parsing | 21 descriptors in each of `benchmark` and `smoke` profiles |
| Native images | All 21 built and source labels/image IDs inspected; 20 arm64, RobotRumble amd64 |
| Full Harbor diagnostic smoke | All 21 latest runs passed; 63 native rounds, 92 edit proofs, 67 native containers removed and checked |
| Four-player transparent Bridge | Passed separately: 3 rounds, 8 edits, 24 opponent snapshots, 5 containers removed; does not add an arena |
| Final verifier replay | All 22 latest scenario artifacts pass with unchanged rewards |
| Native scorer fixtures | 8 cases passed, including team winners, negative scores and invalid/no-contest branches |
| Corrupted real evidence | All 10 mutations rejected; untouched Bridge evidence accepted |
| Formatting, lint and package | Ruff passes; wheel/sdist contain required catalog, runtime and task assets |
| Repository structural validator | 28 passed, 1 expected outstanding error: absent `parity_experiment.json` |
| Model / oracle / parity / registry | No external model API calls, no oracle pass, no parity experiments, no registry publication |

Each smoke has baseline round 0 plus two scripted edit/competition rounds. Native
raw logs were independently inspected for completed games; a verifier pass alone was
not counted as working competition. These checks preserve upstream scorer behavior,
including the Halite2/Halite3 attribution bug; they do not establish adversarial integrity.
Figgie/Halite3 baseline bots can emit native action warnings, and PaintVolley's native
fallback can hide action errors. No zero-action-error claim is made.

[`validation/compatibility.json`](validation/compatibility.json) retains per-attempt
results, image/config/source hashes, original failures and recovery evidence. It records
34 standard attempts (29 verifier passes and 5 failures), including an early all-invalid
Halite3 verifier pass that is excluded from competitive coverage. Earlier passes were
rerun after switching the smoke editor to host staging. The final equivalent Git-ref
batching change is covered by a real-Git regression and the RobotRumble recovery.
The report's `native_scoring_caveats` field contains the native scoring audit; references
to `native-scoring-caveats.json` name the original local receipt, whose content is
embedded in that field.
Run the unit suite with:

```bash
/path/to/harbor-venv/bin/python -m unittest discover -s src/codeclash/tests -v
```

### Native Mini execution preflight

The shared native Mini agent loop was also exercised through Harbor with two players
using mini-swe-agent's built-in deterministic model responses. Each attempt completed
nine games across baseline plus two edit rounds, 16 model-query/bash-action steps,
four source-edit/feedback/persistence checks, native scorer replay and the separate
Harbor verifier. All three native containers were removed and their absence checked.
These are predetermined responses with harmless source edits, not external inference
or measured coding ability; no paid API requests were made.

The first attempt exposed a classification bug: test-model results lacked diagnostic
labels. A fresh second attempt with the identical native configuration verified the
fix in runtime provenance, Harbor context and verifier rewards. The self-contained
[`validation/native-mini-preflight.json`](validation/native-mini-preflight.json)
preserves both attempts, the full fixture, reproduction commands, dependency versions
and source/evidence hashes. The earlier 21-arena compatibility report is unchanged
historical evidence; this focused check does not add an arena or establish parity.
To reproduce it, extract `native_config` with the report's command and save
`reproduction.harbor_job_template` as `native-mini-job.json`, replacing its two native
path placeholders. Generate the dataset, then run that job from a credential-free
Harbor environment with `PYTHON_DOTENV_DISABLED=1`.

### Validation on the submission base

Fresh checks against `harbor-index/adapters` main at
`7b4df9deb2381ae32f26e6cebec06aaa2975dc56` are recorded in
[`validation/local-checks.json`](validation/local-checks.json): 73 unit tests, 42
generated tasks parsed by Harbor, 21 exact native configuration comparisons, 22
historical artifact replays, Ruff and package checks. Structural validation reports
28 passes, one error for missing parity results, and zero warnings.

Two fresh Docker runs from this checkout passed: native Mini BattleSnake with test
responses (nine games, 16 query/action steps, four edit/feedback proofs) and transparent
four-player Bridge (nine games, eight edit/feedback proofs, 24 opponent snapshots).
All eight native containers were removed and their absence checked. Runtime imports
and source hashes were verified against this checkout. The older 21-arena image builds
and full smoke runs remain historical evidence; this update does not claim a fresh
21-arena execution or any oracle/model-parity result.

### Native reference validation

A separate reference suite passed **126 checks across all 21 arenas**, each on its
first attempt. For every arena it invokes the pinned native validator on its starter
program, removes the entry point and checks rejection, restores the exact bytes and
revalidates, then checks the native one-valid-player and all-invalid scoring branches.
All 290 reference file/symlink entries were restored, all 67 owned containers were
removed, and the final labeled container inventory was empty. No model API requests
were made. Source hashes remained unchanged during execution.

These are **reference checks**, not winning solutions or an accepted competitive
oracle. The forfeit controls skip tournament simulations; some native validators
still run engine probes. In particular, native all-invalid submissions receive tie
scores, so positive reward alone does not establish valid gameplay. This suite uses
an offline Docker environment shim and lightweight player records; it does not run
the full editing tournament. The earlier full-game checks complement this evidence.

[`validation/reference-validation.json`](validation/reference-validation.json)
records each arena's image/source pins, native implementation hashes, expected and
observed results, reference-byte inventory and cleanup. The exact executed helper
and raw logs remain local artifacts; their hashes are recorded. The published helper
has the same check logic, with clearer report wording, formatting and required CLI
paths, plus checks that reject reused output and empty catalogs and require zero
worker exit codes. Those protections have two regression tests; the current unit
suite passes 75 tests. The 21-arena suite was not rerun after these helper changes.
Reproduce from the repository root using the separate native Python environment
and the 21 prebuilt pinned arena images:

```bash
/path/to/native-venv/bin/python src/codeclash/scripts/run_native_references.py \
  --upstream-repo /path/to/CodeClash \
  --catalog src/codeclash/src/codeclash/catalog.json \
  --plan src/codeclash/validation/native-reference-case-plan.json \
  --output-dir /path/to/fresh-reference-output
```

This helper makes no model calls and refuses existing per-arena attempt directories.
Competitive oracle acceptance and real-model parity remain pending.

### Benchmark configuration and oracle

`run_codeclash.yaml` retains the guide's default `oracle` agent and a commented custom
coordinator example. **The oracle intentionally fails:** the competitive oracle
acceptance procedure remains unresolved.

The native model path uses the selected configuration's agents and models. A `benchmark`
profile does not turn diagnostic presets into model evaluations. Model experiments and
parity need an agreed evaluation plan and resolution of the grading-isolation blocker.
Standard single-agent CLI agents are unsupported by this tournament integration.

## Usage: Create Task Directories

From the adapters repository root:

```bash
# List all arena IDs and preset provenance.
uv run --project src/codeclash codeclash --list-arenas

# Generate all 21 descriptors, including 15 diagnostic presets.
uv run --project src/codeclash codeclash --profile benchmark --output-dir datasets/codeclash

# Select arenas for diagnostic integration checks.
uv run --project src/codeclash codeclash --profile smoke \
  --task-ids bridge corewar --output-dir datasets/codeclash-smoke

# Load complete native model configurations with upstream include semantics.
uv run --project src/codeclash codeclash --profile benchmark \
  --config /path/to/configs/bridge-models.yaml \
  --config /path/to/configs/figgie-models.yaml \
  --config-root /path/to/CodeClash/configs \
  --output-dir datasets/codeclash-custom
```

- `--output-dir` defaults to `datasets/codeclash` under the current directory.
- `--limit` caps arena descriptors, not rounds or matchups.
- `--task-ids` accepts stable lowercase IDs from `--list-arenas`.
- `--overwrite` replaces only directories marked as generated by this adapter.
- `--profile benchmark|smoke` defaults to `benchmark`.
- Repeat `--config` for distinct arenas. Supplying configs selects only those arenas;
  duplicate configs for one arena are rejected to prevent inflated task counts.
- `--config-root` sets the `!include` root, normally pinned upstream `configs/`.
  Includes cannot escape that directory.

Use separate directories for alternate profiles, rosters or budgets. They retain the
same arena identities and cannot count as additional benchmark tasks.

## Comparison with Original Benchmark (Parity)

No model parity experiments have been performed. API inference spend is `$0`; parity
and registry coverage are both **0 arenas**. This table records status only.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Performance | Harbor Performance |
|---|---|---|---|---|---|---|
| Native mini-swe-agent | Pending matched experiment | Native scores; two-player tertiary where defined | 0 | 0 parity-tested arenas | Not run | Not run |

`parity_experiment.json` is intentionally absent. The 21 generated descriptors are not
21 parity-validated arenas; no numerical parity result or uncertainty estimate is claimed.

| Component | Pin |
|---|---|
| CodeClash | `f0694c64ecf6abfca2bc867bad2de9333fef5be8` |
| Arena repositories and bundled sources | Per-arena records in [`catalog.json`](src/codeclash/catalog.json) |
| Harbor compatibility baseline | `99218a4611e3bd76e49ce3b5f977e8c8134ad3ac` |

Original-side reproduction must use pinned native CodeClash with the same resolved
configuration, models, opponents, image/source identities and budgets. Its PvP entry is:

```bash
/path/to/CodeClash/.venv/bin/codeclash run /path/to/effective-native-config.yaml
```

Before using that entry point, make the same pinned image available under native
CodeClash's `codeclash/<arena>` image name and verify its image ID. Otherwise the native
CLI can build a different image. This command is a starting point for the matched
experiment procedure; original-side isolation and seed alignment are still unresolved.

Harbor uses the custom coordinator and generated manifest. The adapter's direct runner
can diagnose its native integration without Harbor:

```bash
/path/to/CodeClash/.venv/bin/python \
  /path/to/adapters/src/codeclash/src/codeclash/runtime/native_runner.py \
  --request /path/to/request.json
```

The request contains `upstream_repo`, `native_image`, complete schema2 `task_config`,
`output_dir`, `model_name`, `opponent_model_name`, `player_models` and unique `run_id`.
Use `null` positional models and `{}` named overrides for smoke. The output directory
must not exist. Comparing this adapter runner with Harbor alone does not prove parity
with the original benchmark.

Before parity, approve the oracle alternative, score aggregation and original-side
reproduction procedure. Align isolation changes, opponent snapshots, seeds and budgets
on both sides. Follow the guide's symmetric ramp-up and repeated-run mean ± sample SEM
reporting; count independent arena items rather than rounds or tournaments.

## Notes & Caveats

- **HIGH blocker: submitted code can tamper with shared-arena grading.** Native submitted
  programs share the arena container with the grader, opponents and match logs.
  BattleSnake's shared root execution has been inspected directly; generic coverage does
  not establish isolation for other arenas. Network isolation, credential exclusion and
  consistency checks do not remove filesystem access. Trusted evaluation needs an agreed
  upstream-compatible isolation design.
- **Native Halite2/Halite3 score attribution is wrong.** Their engines emit zero-based
  player IDs, but the inherited pinned `HaliteArena.get_results` indexes `int(id) - 1`,
  swapping two-player attribution. Raw outcomes and native scores disagree in all three
  rounds of each exercised arena. The adapter faithfully replays this scorer; it does
  not silently fix the benchmark. The evidence report records both sides. Correcting it
  requires an upstream fix and a new, consistently applied baseline before model claims.
- **Competitive oracle is unresolved.** `solution/solve.sh` deliberately fails. No perfect
  oracle or accepted alternative is claimed.
- **Early WIP, not ready for review.** This draft was requested for implementation and
  architecture discussion before the guide's oracle/parity milestone.
- **Generation is not runtime coverage.** All 21 descriptors use the common interface.
  Six presets contain published model PvP configurations; 15 retain upstream diagnostics.
  Image builds and diagnostic execution are verified separately above; model execution
  and parity remain unverified.
- **Only PvP has a backend.** Single-player training competes before editing against an
  earlier version. Ladder adds ranking, advancement and cross-rung carry-over. These modes
  are rejected. `branch`, `branch_init` and remote push overrides are rejected pending a
  pinned opponent-snapshot backend; no fixed-opponent substitute is implied.
- **Local Docker only.** Remote Harbor providers and generic CLI-agent execution are
  unsupported. Native Docker is controlled from the host; Harbor containers get no socket.
- **Separate Python environments are mandatory.** Native CodeClash and the adapter both
  export `codeclash`; installing them together can load the wrong package.
- **Smoke changes execution intentionally.** Scripted edits are staged through host
  Docker copies, so native game images do not need Python for the smoke helper.
  It uses two edit rounds, reduced
  arena-appropriate budgets and scripted edits. Source prompts remain verbatim, including
  hardcoded original round counts; scripted editors do not consume those prompts.
- **Source pinning is not hermetic building.** Image IDs and source identities are recorded,
  but upstream base tags, apt packages and toolchain installers can remain floating.
  Each image needs validation on the target host architecture. RobotRumble's bundled
  engine is x86-64, so its builder explicitly selects `linux/amd64`.
- **Transparent copying preserves an upstream caveat.** In the exercised four-player
  Bridge run, the second round kept initial opponent files at the snapshot root and
  copied the updated files under `workspace/`. The adapter verifies that updated source
  is present and records the full inventory; it does not claim that root-level paths
  refresh or silently change native copy semantics.
- **Seed behavior varies.** Native arena randomness and existing seed settings are
  preserved. There is no agreed common reproducible seed protocol; parity changes must
  be applied symmetrically.
- **Historical smoke is separate.** [`validation/smoke.json`](validation/smoke.json) records
  the earlier schema1 BattleSnake-only implementation and its failed/passing attempts.
  It is not validation of this generalized schema2 runner; use
  [`validation/compatibility.json`](validation/compatibility.json) for current coverage.

## Installation / Prerequisites

Use local Docker, Python 3.12+ and `uv`, with separate environments:

```bash
# Native harness only.
git clone https://github.com/CodeClash-ai/CodeClash.git /path/to/CodeClash
git -C /path/to/CodeClash checkout f0694c64ecf6abfca2bc867bad2de9333fef5be8
uv sync --project /path/to/CodeClash --frozen

# Harbor plus this adapter only.
uv venv --python 3.12 /path/to/harbor-venv
uv pip install --python /path/to/harbor-venv/bin/python \
  'harbor @ git+https://github.com/harbor-framework/harbor.git@99218a4611e3bd76e49ce3b5f977e8c8134ad3ac'
uv pip install --python /path/to/harbor-venv/bin/python -e /path/to/adapters/src/codeclash

# Build one selected arena's source-pinned native image.
/path/to/harbor-venv/bin/python /path/to/adapters/src/codeclash/scripts/build_image.py \
  --arena bridge --upstream-repo /path/to/CodeClash \
  --build-dir /path/to/builds/codeclash-bridge
```

Build each selected arena using a fresh build directory. Optional `--image` defaults to
`codeclash-adapter/<arena>:f0694c64ecf6`; a custom tag requires the same `image` override
in the coordinator. `build_manifest.json` records source pins, Dockerfile hashes,
workspace identities and the resolved image ID. A build alone is not runtime validation.

Packaging prepares BattleCode23/24 Gradle and Halite3 Cargo dependencies and checks a
clean offline rebuild, installs HuskyBench's Cython source-build prerequisite, and
delivers the official GHCup metadata through GitHub's API for Halite2. The manifest
records those metadata bytes/revision. Native game and scorer sources are unchanged.

Replace example paths in commands and YAML files. Model credentials belong only in the
host process environment, never Dockerfiles, task manifests, native config snapshots
or player containers.

The published model presets select the Portkey backend. It additionally requires
`portkey-ai` and appropriate provider credentials in the native environment; the
upstream frozen installation alone does not install that optional SDK. Model-name
overrides do not change backend routing. The contributor program supplies 2077ai API
access and also uses Daytona; compatibility with that route/platform remains untested
in this local-Docker draft and must be settled before the agreed parity experiment.

## Troubleshooting

- **Wrong imports:** keep native CodeClash and the Harbor adapter in separate environments;
  preserve the native venv's `bin/python` path.
- **Missing image/provenance mismatch:** build the selected arena using `build_image.py`.
  A BattleSnake image cannot run another arena.
- **Rejected model override:** use existing native `mini` player names. For dummy presets,
  supply a complete model configuration instead of an implicit conversion.
- **Rejected ladder or branch configuration:** it needs the separate pinned snapshot or
  carry-over backend described above.
- **Existing native output directory:** choose a fresh path; attempts are retained.
- **Failed run/inconsistent evidence:** inspect native logs and the verifier report.
  Partial or inconsistent evidence must not produce a reward.
- **Cleanup failure:** inspect `containers.json` and the audit; target only verified owned
  IDs, never all Docker containers.
- **Oracle fails/no registry entry:** these remain documented WIP limitations.

## Citation

```bibtex
@misc{yang2025codeclashbenchmarkinggoalorientedsoftware,
    title={CodeClash: Benchmarking Goal-Oriented Software Engineering},
    author={John Yang and Kilian Lieret and Joyce Yang and Carlos E. Jimenez and Ofir Press and Ludwig Schmidt and Diyi Yang},
    year={2025},
    eprint={2511.00839},
    archivePrefix={arXiv},
    primaryClass={cs.SE},
    url={https://arxiv.org/abs/2511.00839}
}
```

Credit the [CodeClash paper](https://arxiv.org/abs/2511.00839),
[native repository](https://github.com/CodeClash-ai/CodeClash), and individual arena
repositories in the source catalog.

## Authors & Contributions

Original benchmark: the CodeClash authors and arena contributors.
Adapter implementation: [harshang03](https://github.com/harshang03).

Adapter code belongs under `src/codeclash/` in
[harbor-index/adapters](https://github.com/harbor-index/adapters), with PRs
targeting `main`. Dataset publication is a separate validation step.
