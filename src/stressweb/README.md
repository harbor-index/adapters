# StressWeb

## Overview

This adapter generates 12 Note website tasks for Harbor from two published queries and six website variants. Each trial starts a fresh Note API, React website and Chromium context. No external model service is needed to run a reference solution.

## What is StressWeb?

StressWeb measures whether a web agent can finish the same task when the interface or its interaction rules change. Its full release contains 149 queries across 10 sites and seven conditions. The Note subset retains the original query text and per-query evaluators. [Paper](https://arxiv.org/abs/2604.16385)

## Adapter Features

The agent uses the `browser` command to inspect the upstream Playwright DOM observation, view screenshots and perform the original actions. A separate browser service owns the upstream Node API, React application, browser context and trajectory. Its website and backend listen on loopback. The only network interface offered to the agent by this service accepts browser actions and observations. Grading runs in a fresh verifier environment and reads the browser service's artifact directly.

Each reference solution interacts with visible controls and checks the rendered outcome. Note Q1 creates and saves a bold document. Note Q4 creates a folder in Personal Workspace. The original evaluator produces a checkpoint vector, a checkpoint fraction and a binary `overall_success`. Harbor exposes the latter as the primary reward and saves the detailed upstream result in `/logs/verifier/checkpoints.json`. For checkpoint comparisons across queries, sum passed checkpoints and divide by total checkpoints.

## Generated Task Structure

Each task contains the agent instruction, task configuration, a Compose environment with an agent and browser service, a browser-only reference solution and the unchanged query evaluator. The agent image contains the browser command. The browser image contains the published website, original controller and runtime recorder. The evaluator is packaged under `tests/` and the reference under `solution/`.

## Run Evaluation / Harness

From this adapter's directory, create the tasks and run the oracle using a Compose-capable Harbor provider. Run `nop` separately for a no-action control.

```bash
uv run stressweb --output-dir ./tasks --cache-dir ./inputs
harbor run -p ./tasks -a oracle -e docker
harbor run -p ./tasks/note-q1-clean -a nop -e docker
```

On Harbor 0.26.0 with Daytona, all 12 reference solutions scored 1.0 and passed all 48 upstream checkpoints. The two clean no-action controls both scored 0.0. Q1 starts with one of four checkpoints satisfied and Q4 starts with zero. Running the same frozen tasks in the native browser harness also produced 12/12 successes and 48/48 checkpoints. [Harbor summary screenshot](evidence/oracle.png) · [Per-task Harbor rewards and checkpoints](evidence/oracle.json)

For a local browser check on a machine with Chromium and Node installed, the following commands execute the generated solution and the same evaluator. Each output directory must be new.

```bash
python scripts/native_trial.py ./tasks/note-q1-clean ./native-oracle
python scripts/native_trial.py ./tasks/note-q1-clean ./native-nop --nop
```

## Usage: Create Task Directories

```bash
uv sync --frozen
uv run stressweb --output-dir ./tasks --cache-dir ./inputs
uv run stressweb --output-dir ./one --cache-dir ./inputs --task-ids note-q1-clean
```

The generator supports `--limit`, `--task-ids`, `--overwrite` and `--split parity`. The proposed parity split contains the same 12 paired task IDs. Its scope is recorded in `src/stressweb/manifest.json`. The generator verifies all source assets against `sources.json` and reuses verified cached files.

## Comparison with Original Benchmark (Parity)

Formal parity is pending team confirmation. The proposed comparison covers Q1 and Q4 paired across all six adapted conditions. It uses the same NoteWebsiteAgent prompt, screenshot history, DOM/action protocol, 100-step budget, multimodal model and query evaluators on the original and Harbor sides. The proposed sequence is a paired sanity run followed by three runs per side. A standard CLI-agent experiment would separately exercise the browser command. The exact model ID, parity scope and CLI sample are subject to confirmation.

A driver for each side is provided in `scripts/parity/`. Point `STRESSWEB_UPSTREAM` to the pinned checkout's `StressWeb` directory. Use the same `STRESSWEB_MODEL`, `STRESSWEB_MODEL_BASE_URL` (OpenAI-compatible API base) and `STRESSWEB_MODEL_API_KEY` in the host environment on both sides. The client implements the upstream `test_api.call_openai` interface and sends the original multimodal messages with a 16,384-token response budget. Both drivers load the same hash-verified agent source with literal braces escaped in two JSON examples.

After model and scope confirmation, the following runs one paired task. Repeat it with a new output directory for each trial and each ID in the approved plan. The original driver launches the published EnvironmentManager and NoteWebsiteAgent. The Harbor agent invokes the same model policy and response parser, downloads observations through the browser command, and dispatches the parsed actions to the trusted controller. Its screenshot and DOM arrive via separate browser commands. The original runner obtains them in one observation call. Verify the approved model and observation timing in the first paired sanity run.

```bash
export PYTHONPATH="$PWD/scripts/parity${PYTHONPATH:+:$PYTHONPATH}"
task=note-q1-clean
python scripts/parity/original.py "./tasks/$task" "./original-r1/$task"
harbor run -p "./tasks/$task" -a harbor_note:StressWebNoteAgent -m "$STRESSWEB_MODEL"
```

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|---|---|---|---:|---:|---|---|
| Pending | Pending | Overall success and checkpoint success | 0 | 12 proposed | Pending | Pending |

## Notes & Caveats

The scope uses the first document-creation query and first in-workspace folder-creation query. Each is paired with all published Note site variants: `clean`, `chaos`, `failed`, `perturbed`, `semanticE` and `semanticS`. Each task uses its own variant's backend fixture, frontend entry point and npm lockfiles. The seventh upstream condition, `dom`, depends on a runtime perturbation selection missing from the released batch runner. Query 3 requires creating a workspace, an operation unsupported by the published popup variant. The six website variants use the original controller's default filter and perturbation lists. Batch-specific overrides require author confirmation for formal parity.

The browser has a 1920×1080 viewport. Its recorder saves browser-produced raw HTML, screenshots and executed actions. On a no-action trial it supplies the initial page as the final state. The clean initial page receives one of four upstream checkpoints on Q1 and zero of four on Q4. The binary reward requires every checkpoint. The Note application and browser keep their upstream stochastic behavior. Separate trials can exercise different disruptions.

## Installation / Prerequisites

Task generation uses Python 3.11+ and uv. Initial asset acquisition requires access to the pinned GitHub source and Hugging Face dataset. Container runs require Harbor with separate-verifier and Compose service artifact support. The browser image includes Node 20.20.2, Python 3.11, Playwright 1.65.2, Chromium and Pillow 13.0.0. The frontend uses the upstream lockfile with npm's legacy peer dependency resolver. Install Playwright and Pillow at these versions in the Python environment running the parity drivers.

## Troubleshooting

A local native run needs Node, Playwright, Pillow and Chromium with its system libraries. Install the specified Python packages and run `python -m playwright install --with-deps chromium` on a supported host. Inspect `browser.log`, `oracle.log` and the per-query `checkpoints.json` for individual native trials. A missing browser artifact causes a verifier error.

## Citation

```bibtex
@misc{bai2026stressweb,
  title = {StressWeb: A Diagnostic Benchmark for Web Agent Robustness under Realistic Interaction Variability},
  author = {Haoyue Bai and Dong Wang and Long Chen and Bingguang Hao and Pengyang Shao and Yonghui Yang and Yicheng He and Chenyi Zhuang},
  year = {2026},
  eprint = {2604.16385},
  archivePrefix = {arXiv},
  primaryClass = {cs.SE}
}
```

## Authors & Contributions

StressWeb and the published website environments are by the benchmark authors credited above. Harbor adapter contributor: Haoyue Bai. [Pinned upstream source](https://github.com/inclusionAI/AWorld-RL/tree/be52dbf33051c9b86e8e4d3c4e2394548906c75b/StressWeb) · [Pinned dataset](https://huggingface.co/datasets/HuggingSelf/StressWeb/tree/4cda241d145b2026365c3f122cccc44123798327)
