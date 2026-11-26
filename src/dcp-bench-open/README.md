# DCP-Bench-Open adapter pilot

## Overview

Local four-problem pilot for DCP-Bench-Open v0.3.0, using the unchanged native
Python/CPMpy evaluator. On November 23, 2026 the local runtime pilot passed:
native controls 42/42, Harbor oracle 4/4 over 16 distinct instances, and 54/54
instance/control comparisons. Actual Harbor rewards and trial errors were also
checked. Raw evidence is retained in the private contribution workspace; public
evidence upload, full generation and model parity remain outstanding. This is
not a completed contribution.

## What is DCP-Bench-Open?

A constraint modeling benchmark by Kostis Michailidis, Dimos Tsouros and Tias
Guns. The full release has 164 problems and 634 distinct instances. This pilot
selects `abbots_puzzle`, `sudoku`, `aircraft_landing` and `fixed_charge` (16 distinct
instances: 1 + 5 + 5 + 5). Source: https://github.com/DCP-Bench/DCP-Bench-Open

## Adapter Features

The generator preserves each source docstring, publishes the embedded example
and native CPMpy skill, and asks for one `build(instance)` entrypoint. The same
candidate is checked on every distinct selected instance. Reference ASTs supply
oracle models; submitted code is never imported by the verifier host. Native
limits, reason codes, output checks and optimization proof are retained.

Harbor uses a fresh, separate verifier container. Only `/workspace/model.py`
transfers from the agent. A verifier-private Docker daemon runs the original
resource-limited candidate sandbox. The agent has no privileged Compose config,
Docker socket, reference model, solution, full dataset or held-out inputs.

## Generated Task Structure

Each task contains `task.toml`, `instruction.md`, an `environment/` with public
inputs, `solution/` with a trusted oracle/control, and `tests/` with the unchanged
native evaluator, reference data, image archive and thin grading wrapper.
`tests/Dockerfile` bundles the verifier; `tests/docker-compose.yaml` grants only
that separate verifier the privilege required for nested Docker.

## Run Evaluation / Harness

No registry dataset exists yet. With pinned Harbor installed, from this directory:

```sh
harbor run -c run_dcp-bench-open.yaml
# Equivalent local pilot command:
harbor run -p datasets/dcp-bench-open -a oracle
```

Use framework revision `96c86af3bc6d7e92f21fc771d21273be4714c8e9` (0.27.0 source).
It supports separate verifier artifacts and verifier-specific build definitions.
The scaffold was produced by installed Harbor 0.25.0, then updated to the current
adapter contract; scaffold version is not the experiment runtime version.

## Usage: Create Task Directories

Build `dcp-eval/cpmpy_python:v1` from the upstream v0.3.0 source, save it to an
archive, and supply its exact Docker image ID. Source files are checked against
committed SHA-256 pins before loading any trusted reference code.

```sh
uv sync
uv run dcp-bench-open --source-dir /path/to/v0.3.0-source \
  --solver-image-archive /path/to/cpmpy.tar \
  --solver-image-id sha256:ACTUAL_64_HEX_IMAGE_ID \
  --output-dir datasets/dcp-bench-open
```

`--limit`, `--overwrite` and `--task-ids` are supported. Pass `--controls-dir`
with `--control correct` to use the exact native control bytes on both sides;
without it, the correct model is reconstructed from the trusted AST. Task IDs accept source
underscores or generated hyphens. Negative controls use the reviewed control
files independently generated from the same release:

```sh
uv run dcp-bench-open --source-dir /path/to/v0.3.0-source \
  --solver-image-archive /path/to/cpmpy.tar \
  --solver-image-id sha256:ACTUAL_64_HEX_IMAGE_ID \
  --output-dir datasets/dcp-incorrect --control incorrect \
  --controls-dir /path/to/controls
# Also supported: --control unsat
# For suboptimal controls add:
# --control suboptimal --task-ids aircraft_landing fixed_charge
```

Control cases retain benchmark task names but must live in separate output/job
directories. Aircraft's suboptimal control is example-only; fixed-charge uses
all selected IDs and native first-failure behavior. Each task's manifest records
the actual instance selection and control. `--split parity` is deliberately not
implemented before the parity sample is agreed.

## Comparison with Original Benchmark (Parity)

No model parity has run. `parity_experiment.json` is an empty array, intentionally
causing the submission validator's parity-completeness error. Do not replace it
with invented scores. Oracle/control agreement is a prerequisite, not model
parity. The organizer must agree to the agent, model, sample, repeats and budget.

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|---|---|---|---|---|---|---|

No measured rows are available yet.

For native controls, run the same model files and instance IDs with upstream:

```sh
python -m evaluation.check MODEL --problem ID --solver cpmpy_python --instance-ids IDS
```
For Harbor controls, generate the corresponding `--control` dataset and run the
oracle agent. Compare `/logs/verifier/native-result.json` with native result JSON,
including every evaluated instance, reason and unperformed ID. Negative controls
also write `native-instance-results.json`: independent calls on every selected
instance for diagnostics; these do not replace the native aggregate reward. A correct pilot
requires four task rewards of 1 and all 16 instances accepted.

## Notes & Caveats

- **Pilot-only local dependencies:** the solver image must already exist on the
  Docker host, and its `docker save` archive is copied into each generated task.
  These large generated directories belong outside Git. A distributable
  verifier-image strategy remains to be designed for the full adapter.
- Nested Docker requires privileged verifier containers, which Harbor lint
  reports. There is no host socket mount. Agent and candidate remain
  unprivileged; the candidate runs with upstream network, capability, PID,
  memory and read-only limits. Backend support beyond local Docker is untested.
- The Docker binary image defaults to `docker:28.2.2-dind`; override it with
  `--docker-binary-image docker:28.2.2-dind@sha256:...` for immutable builds.
  Record the actual solver and verifier image identities for every run.
- Native grading is output counterexample search, not full model equivalence.
  Overconstrained models can pass; zero-solution models fail. This is preserved.
- `json:0` duplicates the embedded example and is not a selectable ID. Native
  grading stops at the first failure; `instances_checked` counts passes only.
- Extra `name`/`note` metadata is excluded from agent-visible public input.
  The unchanged evaluator still passes its original instance dict to the
  isolated candidate during grading, as native evaluation does.
- No native evaluator modifications, timeout tolerance or scoring substitutions
  are introduced. All source hashes are checked before generation.
- Before model parity, freeze a symmetric agent prompt with explicit field
  semantics and output shapes; this pilot exposes the source docstring and
  example values but does not yet derive a complete field-description schema.
- Full 164-task generation, model parity, registry publication and PRs remain
  outstanding. Package validation is not evidence of runtime correctness.

## Installation / Prerequisites

Linux x86_64 Docker engine, Docker Compose, Python 3.12+, uv, pinned Harbor,
expanded v0.3.0 source (`3f7e121301e0ea4565e20d8ee0660e799686c194`) and a built native
CPMpy image. Upstream solver dependencies are pinned in `runner/requirements.txt`.
The generator has no runtime Python dependencies outside the standard library.

From the adapters repository, validate with:

```sh
uv run python scripts/validate_adapter.py dcp-bench-open
```

## Troubleshooting

Inspect the Harbor trial's `verifier/test-stdout.txt`, `verifier/dockerd.log`,
`verifier/docker-load.log` and `verifier/native-result.json`. Native reason codes
distinguish invalid/suboptimal/no-solution failures from execution/reference
limits and infrastructure errors. A missing reward/result is an infrastructure
failure, not evidence of model inability. Do not count static checks as oracle
success.

## Citation

```bibtex
@misc{michailidis2025dcpbenchopen,
  title={DCP-Bench-Open},
  author={Kostis Michailidis and Dimos Tsouros and Tias Guns},
  year={2025},
  eprint={2506.06052},
  archivePrefix={arXiv}
}
```
https://arxiv.org/abs/2506.06052
Upstream license: Apache-2.0; source attribution remains in the copied dataset
files and `SOURCES.md`.

## Authors & Contributions

Baojian Zhou owns this Harbor contribution. The four-problem pilot wrapper was
prepared with Codex assistance; native evaluator and reference models remain
attributed to their upstream authors. No upstream submission has been made.
