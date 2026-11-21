# AgentAbstain Harbor adapter

## Overview

This adapter packages the public [AgentAbstain](https://github.com/AntiQuality/agentabstain) corpus for Harbor. AgentAbstain tests whether a tool-using model recognizes when it should stop, ask for clarification, or report that a request cannot be completed safely.

The source release contains 526 task variants arranged as 263 strict `act`/`abstain` pairs. The pairs cover eight scenario categories and 42 executable MCP environments. Each pair has one native task YAML for the should-act variant and one for the should-abstain variant. The adapter keeps the native task YAML, initial state, DAG, persisted `critical_actions`, `available_tools`, and `tool_overrides` for each variant.

The source code is MIT licensed and the Hugging Face dataset is CC BY 4.0. The adapter records source commit `cfc3faf7ab1cfd4892cde1158d6e43b2f312ddc3` and dataset revision `842228426c2a703347396501af61c7890972c7ee` in `SOURCE_MANIFEST.json`.

## What is AgentAbstain?

AgentAbstain is described in [AgentAbstain: Do LLM Agents Know When Not to Act?](https://arxiv.org/abs/2607.10059), a NeurIPS 2026 paper. It combines a deterministic commit check over successful critical tool calls with an LLM judge over the final response. A should-act task passes when all persisted critical actions were successfully called. A should-abstain task passes when none were successfully called. The response judge checks whether the response recognized the scenario and abstained when appropriate.

## Adapter Features

- Generates all 526 variants or reproducible `smoke` and `parity` subsets from the public dataset snapshot.
- Preserves native pair IDs while using registry-safe neutral task names such as `agentabstain/ambiguous_action_specification-preview_001-variant-a`.
- Runs the original environment implementations in a private per-task runtime sidecar. The main agent container receives only public tool schemas and an HTTP bridge.
- Writes an append-only execution log to a named volume mounted read-only in the agent container.
- Runs grading in a separate verifier image. The verifier reads only the declared final-response and runtime-log artifacts. Harbor also collects its conventional `/logs/artifacts` manifest, which this verifier does not read.
- Uses the persisted native `critical_actions` list for the commit check and preserves informational versus operational combined scoring.
- Reads the final assistant response from the declared `/logs/agent/final_response.txt` artifact. The verifier retains trajectory parsing for local/shared diagnostics, but separate verification uses the explicit response handoff.

The Harbor instruction presents the original system prompt and user request in separate labeled sections because the standard Harbor task format has one instruction file. This is an adapter-level prompt transport difference and must be applied symmetrically in any parity comparison.

## Generated Task Structure

```text
{task_id}/
├── task.toml
├── instruction.md
├── environment/
│   ├── Dockerfile                 # agent-facing main container
│   ├── runtime.Dockerfile         # private stateful sidecar
│   ├── docker-compose.yaml
│   ├── agentabstain_cli.py
│   ├── runtime_server.py
│   ├── task_runtime.json          # copied only into the sidecar image
│   └── abstention_factory/         # copied only into the sidecar image
├── solution/
│   ├── solve.sh
│   ├── oracle.py
│   └── source_task.yaml
└── tests/
    ├── Dockerfile
    ├── test.sh
    ├── verifier.py
    ├── source_task.yaml
    └── task_metadata.json
```

`tests/` and `solution/` are available to the separate verifier or only to OracleAgent. The main agent image does not copy grader code. The verifier receives the declared artifact paths in `task.toml`; Harbor's conventional artifact manifest is collected separately and ignored by the grading code.

## Usage: Create Task Directories

The adapter is a standalone uv project. Download the public dataset once, then generate tasks.

```bash
cd src/agentabstain
uv sync
uv run agentabstain \
  --data-dir /path/to/agentabstain/data \
  --output-dir /path/to/datasets/agentabstain
```

Useful development subsets are one pair for a smoke test and a reproducible 47-pair slice covering all 42 environments.

```bash
uv run agentabstain --data-dir /path/to/data \
  --output-dir /tmp/agentabstain-smoke --split smoke --overwrite

uv run agentabstain --data-dir /path/to/data \
  --output-dir /tmp/agentabstain-parity --split parity --overwrite
```

`--limit N` selects the first N source rows and may produce a partial pair. Use `--split smoke` or `--task-ids category/preview_NNN` when pair completeness matters. `--task-ids` accepts a source pair ID, a category-qualified source ID, or a generated neutral ID. Category qualification avoids collisions among repeated preview numbers.

Run an individual Harbor trial from the Harbor repository.

```bash
uv run harbor trial start \
  -p /path/to/datasets/agentabstain/<task_id> \
  -a <agent> -m <model>
```

A real judge requires `OPENAI_API_KEY`, an OpenAI-compatible `OPENAI_BASE_URL` when needed, and `AGENTABSTAIN_JUDGE_MODEL`. For local structural and Oracle smoke tests only, set `AGENTABSTAIN_JUDGE_STUB=expected`. Stub results are diagnostic and do not count as benchmark parity.

## Run Evaluation / Harness

The verifier writes `agentabstain_metrics.json` and the scalar reward to `/logs/verifier`. The `combined` metric follows the source semantics. Informational tasks use the response judge. Operational tasks require both the commit check and the response judge. Pair-level paired accuracy and CAR must be aggregated after both variants have run, using the shared `pair_id` in `tests/task_metadata.json`.

The adapter has a verified full generator, sidecar Docker build, separate-verifier task package, and synthetic verifier checks. Local release validation covers the pinned two-variant smoke, a reference and known-wrong verifier pair, and structural checks over the full generated corpus. The Harbor v0.26.0 full Oracle sweep is tracked separately while the review evidence is assembled. The local Harbor checkout resolves tag `v0.26.0` to commit `3cb62739`.

## Comparison with Original Benchmark (Parity)

Formal original-to-Harbor parity with two or more runs per side is still pending. The current `parity_experiment.json` intentionally contains no scores or dataset/parity contribution links until those runs and contribution links exist. The proposed pilot uses four complete pairs, then expands to a maintainer-approved pair-complete 5–10% subset. The same pair IDs will run on the original and Harbor sides with a maintainer-approved lower-cost agent/model, at least two independent runs per side, and no offline stub. We will aggregate paired accuracy and CAR by `pair_id`, report means and sample SEM, and compare the original and Harbor score vectors with MSE. The maintainer-approved MSE threshold determines review readiness. Raw submissions, logs, and grading results will be retained with the dataset and parity links. Exact agent/model settings, judge endpoint, and parity key remain pending maintainer confirmation. The Harbor judge uses an OpenAI-compatible Chat Completions request with temperature 0. A parity report must record the judge endpoint, prompt transport, number of runs, and whether the offline stub was used.

```bash
# Generate the deterministic 47-pair development slice
uv run agentabstain --data-dir /path/to/agentabstain/data \
  --output-dir /tmp/agentabstain-parity --split parity --overwrite

# Original benchmark side, from an AgentAbstain checkout, after the team
# approves the harness and model settings for the same source pair IDs
python -m src.scripts.run_inference \
  --runtime-config /path/to/agentabstain/src/configs/<runtime-config>.yaml \
  --task-config /path/to/agentabstain/src/configs/<task-config>.yaml \
  --workers 4
python -m eval.runner --provider <provider> --model <model>

# Harbor side with the same pair IDs and approved agent/model settings
uv run harbor run -p /tmp/agentabstain-parity -a <agent> -m <model> \
  --n-concurrent 4
```

| Agent | Model | Metric | Runs | Dataset size | Original | Harbor |
|---|---|---:|---:|---:|---:|---:|
| pending | pending | paired accuracy / CAR | pending | pending | pending | pending |

## Notes & Caveats

- The sidecar prevents the agent from reading or modifying initial state, task metadata, critical actions, or the runtime process. The agent can read the execution log because tool results are part of the observable trajectory.
- The separate verifier receives `/logs/agent/final_response.txt` and `/runtime_logs/execution.json` through the task artifact list. Grading does not run in the agent workspace. The runtime log is collected from the `runtime` compose service.
- The adapter exposes the native tools through a CLI bridge because the generated Harbor task has no native MCP server entry point. The instruction preserves the source system prompt and user request in labeled sections, adds an Available Tools bridge section, and requires the final response artifact. These are adapter transport changes, so parity runs must use the same bridge and prompt transport on both sides.
- The adapter's judge uses an OpenAI-compatible Chat Completions request, while the released evaluator currently uses the Responses API. This transport difference is recorded for parity control, and published parity must keep endpoint behavior fixed or update the adapter judge to match.
- Harbor verifier runs do not receive the source runner's `run_error_kind`. Missing responses, required artifacts, judge credentials, or unrecovered judge API/parse errors produce a reward of `0` instead of being excluded from the source denominator. Transient judge failures receive bounded retries before that result. This is a known parity limitation and must be reported with formal scores.
- Native `critical_actions` is the grading source of truth. The adapter does not recompute it from the DAG or tool schema.
- `SOURCE_MANIFEST.json` records the source code commit and dataset revision used for this adapter. The generator does not fetch or verify a remote revision, so parity and release builds must supply that pinned snapshot explicitly through `--data-dir`.
- Oracle and agents should write the final response to `/logs/agent/final_response.txt` because the separate verifier receives that explicit handoff.
- The public source has no released AbstainGen generator. This adapter uses the released task and environment snapshot.
- An exploratory Codex smoke on the pinned act pair timed out while searching for opaque record IDs that are not enumerable through that task's public tools. This is recorded as an agent-discoverability signal and is not used as a parity score.

## Installation / Prerequisites

- Python 3.11 or newer and `uv`
- Harbor v0.26.0 with Docker Compose support. The separate-verifier task contract was validated against commit `3cb62739`.
- Docker Desktop or a Docker daemon with the generated compose services enabled
- The public AgentAbstain dataset snapshot
- Judge credentials for real response scoring

## Troubleshooting

- If Docker cannot mount trial logs, keep the Harbor trials directory under a host-shared path such as the workspace.
- If the verifier writes reward `0`, inspect `/logs/verifier/agentabstain_metrics.json` for the commit and judge sub-results.
- If the runtime healthcheck fails, build the generated `runtime.Dockerfile` directly and inspect the runtime container logs.

## Citation

```bibtex
@inproceedings{liu2026agentabstain,
  title     = {AgentAbstain: Do LLM Agents Know When Not to Act?},
  author    = {Liu, Xun and Zhang, Yi Evie and Kasprova, Vira and Rabbani, Parisa and Zahraei, Pardis Sadat and Zhang, Tianyu and Ebrahimpour-Boroojeny, Ali and Chandrasekaran, Varun},
  booktitle = {Advances in Neural Information Processing Systems},
  year      = {2026},
  url       = {https://arxiv.org/abs/2607.10059}
}
```

## Authors & Contributions

The Harbor adapter is maintained by the BLE team. Issues and pull requests belong in the [Harbor adapters repository](https://github.com/harbor-index/adapters).
