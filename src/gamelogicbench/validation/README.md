# Oracle validation for the WIP adapter

Historical Oracle baseline: **PASS — 72/72 tasks at reward 1.0, 1,474/1,474 cells passed, zero exceptions, zero retries.**

The final `oracle-full` run started at 2026-11-18 02:16:47 and finished at 03:16:56 (America/Los_Angeles). Every included task completed exactly once; there are no exclusions, skipped cells or duplicate tasks.

Current acceptance status: **NOT READY**. The negative integrity check failed in both evaluators, and model parity uses a mixed-driver cohort. See [acceptance findings](acceptance.md). Oracle success does not establish tamper resistance.

## Fixed inputs

- Adapter implementation: `8243e330b954bf464a5988ae539c4c862b03ac13`.
- Harness: `NJU-LINK/GameLogicBench@10287322040bc72574f144cb40eccfd6ccb66936`.
- Tasks: `NJU-LINK/GameLogicBench-Tasks@82f51ab34860c0ab3fe63a0ce9ae394ad38234a6`.
- Harbor: `0.23.0`.
- Godot: `4.4.stable.official.4c311cbee`, official AMD64 and ARM64 archives, SHA512 verified by the runtime build.
- Debian: `bookworm-20261104-slim`, multi-platform index `sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc`.
- Local backend: Colima 0.10.3, Ubuntu 24.04, kernel `6.8.0-117-generic`, 6 virtual CPUs, 8 GiB VM memory. Harbor's native ARM64 network sidecar enforces no-network policy.
- Task execution: ARM64 except `repo_amsg_movement_engine`, whose native GDExtension task pins both agent and verifier to AMD64 under Rosetta. Upstream comparisons use the same per-task architecture.

## Reproduce

Use the pinned checkout commands in the adapter README. On an ARM64 backend, enable AMD64 emulation and use a Docker image store that supports multi-platform images.

From `src/gamelogicbench`:

```bash
docker buildx build --platform linux/amd64,linux/arm64 --load \
  -t gamelogicbench-runtime:godot-4.4 runtime
uv sync --locked
uv run --locked python -m unittest discover -s tests
uv run --locked gamelogicbench --harness-dir "$GLB_HARNESS" --tasks-dir "$GLB_TASKS" \
  --output-dir ../../datasets/gamelogicbench
harbor run -c run_gamelogicbench.yaml -a oracle -n 4
```

For the three representative tasks, generate a separate directory using
`--task-ids atom_light_occlusion combo_reverse_dock repo_amsg_movement_engine`.
Generate another clean directory with `--reference naive` for negative calibration.
Both reference datasets go through the real Harbor artifact and separate-verifier path.

## Completed checks

- Representative differential: 114/114 original-harness and real Harbor cell verdicts match, with proper and naive references; no mismatches or infrastructure exceptions.
- Correct references: 57/57 cells pass. Naive references: 18/57 cells pass and 39/57 fail on both sides.
- Prompts: all 72 offline briefs match after workspace path mapping; all game READMEs are byte-identical.

## Evidence scope

The generated task set contains 72 tasks, 410 scenarios and 1,474 scenario-seed cells. Generation was checked against the pinned upstream parser and command builder; all cell coordinates and Press arguments match.

Raw Harbor runs and original-harness per-cell logs remain private because they contain hidden inputs and reference-solution artifacts. Only reviewed task-level and aggregate results belong in this directory.

At the Oracle-only validation recorded here, the upstream adapter validator reported 30 checks passed, no errors and four warnings for pending publication links and unknown future model-parity cost. No model API was used for that validation. The subsequent 2026-11-20 model parity completed all 144 scored task runs; see [results](parity-results.json) and [reproduction, driver fix and excluded-attempt policy](parity-reproduction.md). The current validator reports 30 checks passed, no errors and two warnings for the pending dataset and Hugging Face evidence PR links. Public dataset registration and Hugging Face evidence publication remain pending.

## Final Oracle evidence

- [Oracle summary](oracle-summary.json) and [per-task results](oracle-per-task.csv).
- [Representative differential summary](differential-summary.json): 114/114 matches.
- [Original CLI transcript](oracle-cli-transcript.txt), with local workspace paths redacted and trailing whitespace normalized.
- [User-provided terminal capture](oracle-terminal-capture.png): the terminal displays the saved original `oracle-full` output, 72/72 tasks at reward 1.0, zero exceptions. The login banner and local command paths above the output were cropped; result text is unchanged. See [screenshot provenance](screenshot-provenance.json).
- [Rendered CLI log](oracle-log-render.png) remains an explicitly labelled text rendering, separate from the terminal capture.
- [Run/submission comparison](version-comparison.json): implementation `8243e330` and prior handoff `f3f08e36` have identical Git blobs and modes for all 19 functional files. Later screenshot/evidence updates leave that implementation unchanged.
- [Versions and image IDs](versions.json), [task inclusion list](task-inclusion.csv), [prompt checks](prompt-fidelity.json), and [live network-policy configuration inspection](network-isolation-check.json).

> Terminal capture showing the saved Harbor Oracle output for `oracle-full`: 72/72 tasks at reward 1.0, zero exceptions.
