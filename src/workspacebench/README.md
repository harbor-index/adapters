# Workspace-Bench → Harbor Adapter

## Overview

Workspace-Bench tests whether an agent can do real office work inside a large file workspace. Each task drops the agent into one persona's workspace (hundreds to thousands of real office files) and asks for deliverables, which an agent judge then checks against a rubric.

- **Source:** 388 tasks in English and the same 388 in Chinese (776 in all), across 5 personas: Logistics Manager, Operations Manager, Backend Developer, Product Manager and Researcher.
- **This adapter:** 723 tasks (353 English, 370 Chinese). 53 are excluded because no answer built from the task's inputs can pass them (see [Notes & Caveats](#notes--caveats)).
- **Rubrics:** 13,759 rubric points across the 723 tasks, 6 to 30 per task (median 20).
- **Data:** task data from Hugging Face `Workspace-Bench/Workspace-Bench` at revision `3491f9e`, workspaces from `Workspace-Bench/Workspace-Bench-Workspaces` at `e245d63` (both Apache-2.0), upstream code at `3fbd0f1` (MIT).
- **Main change for agents:** none in the prompt. The agent gets upstream's own instruction text, with the work folder at `/app` and outputs going to `/app/model_output`.

## What is Workspace-Bench?

Workspace-Bench 1.0 is a benchmark from OpenDataBox ([paper](https://arxiv.org/abs/2605.03596), [site](https://workspace-bench.github.io/), [code](https://github.com/OpenDataBox/Workspace-Bench)) for agents working on tasks with large-scale file dependencies. A task might ask for a manual built from four host scripts, a reconciliation workbook from a quarter of expense files, or a literature review from a folder of papers. The deliverables are docx, xlsx, pptx, pdf, md, csv and other files. Grading is agent-as-a-judge: `agent_as_a_judge.py` starts a Claude Code session that opens the inputs, the outputs and the agent's trace, and checks each rubric point. The score is the share of rubric points passed. Workspace-Bench Lite is upstream's 100-task representative subset.

## Adapter Features

- **Pinned sources.** Task data, workspaces and upstream code are all pinned (revisions above).
- **Workspace fetched efficiently.** The image build reads only the persona's part of the 18.7 GB workspace zip, in a few large HTTP range requests (at most 512 MB each), and checks every file against the zip's CRC-32. One request per file would be about 14,500 requests for the Researcher, over Hugging Face's anonymous limit. A dropped request resumes at the file in progress, and a 429 waits for the reset time Hugging Face sends.
- **Office tools in the image.** `node:24-bookworm-slim` (pinned by digest) with LibreOffice and the Python and Node office libraries, at pinned versions. Claude Code gets upstream's four office skills (docx, pdf, pptx, xlsx), as in upstream.
- **Upstream's grading, unchanged** (`tests/grade.py`):
  - a collect hook runs upstream's own output collector in the agent container, which also picks up outputs saved outside `model_output`;
  - the judge is upstream's `agent_as_a_judge.py` with its prompt unchanged, model `claude-sonnet-5-5` by default (`JUDGE_MODEL`), 3 retries as in upstream's pipeline;
  - the reward is rubric points passed / total.
- **Two judge patches**, used on both sides of parity: the judge's view of the files is a copy instead of symlinks (Claude Code's Glob doesn't follow symlinked folders, so correct outputs were scoring 0), and optional Claude subscription auth.
- **Grading in a separate container.** The verifier runs with `environment_mode = "separate"`, so nothing the agent does can touch the judge. The rubrics and the expected output names never enter the agent's container; the names travel in the collect hook's command.
- **Parity subset.** `--split parity` generates the 50 tasks used for parity.

## Generated Task Structure

```
workspacebench/
├── en-100/
│   ├── task.toml                 # 2 CPUs, 8 GB RAM, 12 GB storage; 2000 s agent / 3900 s verifier timeouts
│   ├── instruction.md            # Upstream's prompt for this task, work folder /app
│   ├── environment/
│   │   ├── Dockerfile            # Image with the persona's workspace unpacked into /app
│   │   ├── fetch_workspace.py    # Range-request workspace fetch with CRC checks
│   │   ├── collect_outputs.py    # Collect hook: upstream's output collector
│   │   ├── judge-copy-view.patch
│   │   ├── judge-subscription-auth.patch
│   │   ├── pip-constraints.txt
│   │   └── office-runtime.package*.json
│   ├── solution/
│   │   ├── solve.sh              # Copies the oracle's files to /app/model_output
│   │   └── output/               # Oracle files (empty unless generated with --oracle-dir)
│   └── tests/
│       ├── test.sh               # Runs grade.py
│       ├── grade.py              # Builds the case and runs upstream's judge
│       ├── instruction.md
│       └── task/                 # metadata.json (rubrics) and the input files, for the judge only
```

Adapter code layout:

```
src/workspacebench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── oracle_disclosure.md          # Tasks where the oracle writes values the inputs lack
├── pyproject.toml
├── run_workspacebench.yaml       # Oracle job config
├── workspacebench_parity.yaml    # Harbor side of the parity experiment
├── scripts/                      # oracle_tools.py and oracle_prompts/: how the oracle files were built
├── tests/                        # adapter, exclusion, metadata and workspace-fetch tests
└── src/workspacebench/
    ├── adapter.py                # Download, filter, name and render tasks; exclusions; parity split
    ├── excluded_tasks.json       # The 53 excluded tasks, each with its reason
    ├── main.py                   # CLI
    ├── prompt.py                 # Upstream's prompt wrapping
    └── task-template/            # task.toml, instruction.md, environment/, solution/, tests/
```

## Run Evaluation / Harness

Once the dataset is published to the Harbor registry ([harbor-datasets PR #69](https://huggingface.co/datasets/harborframework/harbor-datasets/discussions/69) is pending), it can be run by name with `harbor run -d workspacebench/workspacebench`. Until then, generate the tasks locally (see [Usage](#usage-create-task-directories)) and run them from the repository root.

The verifier needs judge credentials: `ANTHROPIC_API_KEY` (and optionally `ANTHROPIC_BASE_URL`), or `CLAUDE_CODE_OAUTH_TOKEN` for a Claude subscription when no API key is set. `JUDGE_MODEL` defaults to `claude-sonnet-5-5`.

```bash
# Oracle on the full dataset (default job config)
harbor run -c src/workspacebench/run_workspacebench.yaml

# Any Harbor agent on a locally generated dataset
harbor run -p datasets/workspacebench -a claude-code -m claude-haiku-4-5 --ak version=2.1.311

# One task
harbor run -p datasets/workspacebench -i en-388 -a claude-code -m claude-haiku-4-5

# Resume a job
harbor job resume -p /path/to/jobs/directory
```

Results go to `jobs/` by default. The oracle only works on tasks generated with `--oracle-dir`.

## Usage: Create Task Directories

```bash
cd src/workspacebench
uv run workspacebench --output-dir ../../datasets/workspacebench

# With oracle files, so the oracle agent can run
uv run workspacebench --output-dir ../../datasets/workspacebench --oracle-dir /path/to/oracle-outputs

# Only the 50-task parity subset
uv run workspacebench --split parity --output-dir ../../datasets/workspacebench-parity
```

Available flags:
- `--output-dir`: directory to write generated tasks
- `--split`: `full` (default, all 723 tasks) or `parity` (the 50-task parity subset)
- `--languages`: `en`, `cn` or both (default both)
- `--task-ids`: only these tasks (names like `en-388`)
- `--limit`: only the first N tasks
- `--overwrite`: overwrite existing tasks
- `--oracle-dir`: directory of oracle files, one folder per task name (e.g. `<dir>/en-033/report.docx`)
- `--tasks-root`: a local copy of the task repo instead of downloading it

The generated tasks take about 3.8 GB, mostly the input files the judge reads. The images are larger: about 19 GB for the Researcher persona and 3–6 GB for the others.

## Comparison with Original Benchmark (Parity)

Upstream already runs Claude Code, so this is Scenario 1. The original side is upstream's own pipeline (isolated task container, task view, prompt building, output collection and judge) in the [fork's `harbor-parity` branch](https://github.com/Pranavv-dev/Workspace-Bench/tree/harbor-parity), with one added agent module that launches Claude Code exactly the way Harbor 0.26.0's `claude-code` agent does: the same install, setup, command and env vars. A test drives Harbor's own `ClaudeCode` class and compares every step, and recordings of the API requests both sides send are identical apart from session ids. Both sides use the same judge (`claude-sonnet-5-5`, upstream's prompt, one verdict per task) in a separate container per case.

Parity ran on 50 English tasks. The pool was Workspace-Bench Lite's 100 English tasks with its excluded tasks replaced; the 13 with known rubric problems were set aside, and 50 were drawn by persona (Operations 16, Logistics 15, Researcher 9, Backend 5, Product 5) with `random.Random(20261120)` before any run. `uv run workspacebench --split parity` generates exactly these tasks. Each side ran a 10-task sanity check and then 4 runs, 2 tasks at a time.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|----------------|--------------|--------------------------------|----------------------------|
| claude-code@2.1.311 | claude-haiku-4-5 | mean rubric pass rate (%) | 4 | 50 (of 723) | 44.05 ± 0.72 | 44.70 ± 0.40 |

Scores are mean ± sample SEM over the 4 runs. Per run: original 42.75 / 42.90 / 45.60 / 44.95, Harbor 43.90 / 45.54 / 45.23 / 44.14. The run ranges overlap. All 400 trials have a valid score. Full details are in `parity_experiment.json`.

Parity and oracle artifacts: [harborframework/parity-experiments#292](https://huggingface.co/datasets/harborframework/parity-experiments/discussions/292). That repository is over the Hub's file limit, so for now the files are in [PranavVashistha/workspacebench-parity-experiments](https://huggingface.co/datasets/PranavVashistha/workspacebench-parity-experiments/tree/393da848d5e9785774f5f5a2921a7061d043d8fe) in the same layout, pinned at revision `393da84`. They'll move to #292 once that repository accepts uploads. Its `ATTEMPTS.md` lists every run on both sides and which ones count.

**Metric:** for each task, the share of rubric points the judge passes, averaged over tasks. This is Harbor's `reward`.

**Reproduce the original side** with the fork's `harbor-parity` branch (the runs used commit `68d3a8a`). Its `evaluation/scripts/harbor_parity/README.md` has the setup; in short, for each half of the task list:

```bash
git clone -b harbor-parity https://github.com/Pranavv-dev/Workspace-Bench && cd Workspace-Bench/evaluation
uv run --script scripts/harbor_parity/run_parity_original.py --task en-003 --task en-013 ... \
    --run-name par-run1 --timeout-sec 4000 \
    --agent-key-file <key file> --proxy-url-file <base URL file> \
    --judge subscription --judge-token-file <token file>
```

**Reproduce the Harbor side:**

```bash
cd src/workspacebench
uv run workspacebench --split parity --output-dir ../../datasets/workspacebench-parity
cd ../..
# ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL for the agent; the judge's credential as above
harbor run -c src/workspacebench/workspacebench_parity.yaml
```

The parity runs used Harbor 0.26.0 (commit `3cb6273`) with prebuilt images, so 5 builds (one per persona; all 50 tasks are English) covered the 50 tasks instead of one per task. Every task's `environment/` is the same template rendered with only its language and persona, so tasks that share both share an image. The images were built from those same `environment/` folders, and each parity task's `task.toml` got `docker_image = "<tag>"` under `[environment]`, with `force_build: false` in `workspacebench_parity.yaml`. Nothing else in the tasks changed. The command above builds each task's image instead, which gives the same image but takes longer and needs more disk. To do it the parity way:

```bash
# for each task: docker build -t <tag> datasets/workspacebench-parity/<task>/environment
# then add docker_image = "<tag>" under [environment] in that task's task.toml
```

**Notes on the parity runs:**
- **Model ID.** The guide asks for a dated model ID, but the 2077AI API only serves the alias `claude-haiku-4-5`. Both sides used the same alias through the same API.
- **Timeout.** The agent timeout was 4000 s on both sides, because the API proxy queues requests. The tasks keep upstream's 2000 s default.
- **Failure rule, fixed before the runs and the same on both sides.** Agent timeouts and crashes are scored. API and infrastructure failures are rerun. Judge failures are rerun and never scored 0. Two trials were recovered this way, both with the first attempt kept: en-078 in original run 1 (API error, "Connection lost mid-response") and en-013 in original run 4 (the judge said it could not read the agent's .pptx). Keeping en-013's first verdict instead, original run 4 is 44.32 and the ranges still overlap (42.75–45.60 vs 43.90–45.54).
- **Same grading.** Harbor run 1's outputs were re-judged by the original side's judge: 43.89, against 43.90 from Harbor's own verifier.
- **Superseded runs.** A first set of 4 original runs still differed from Harbor in what the agent saw: a long per-case work-folder path in the prompt and every tool call, a git-status block (the folder sat inside the repo checkout), and a different memory path. Recording both sides' requests found this. The original side now works in `/app` and logs to `/logs/agent`, as on Harbor, and was rerun from a sanity check. Harbor's runs did not change. The superseded runs are in the upload and are not counted.
- **Noisy tasks.** On some tasks the judge's verdict on the same answer varies a lot (en-055 went from 0/20 to 12/20 on one answer across re-judgings), so a single run moves by about ±1.5 points. The early runs' gap came from a few such tasks: run side by side 6 times per side, the 6 tasks that carried it differ by +0.18 points.
- **Cost.** About $135 on the 2077AI key for agent runs, including sanity checks, superseded runs and diagnostics; the 8 counted runs cost about $66. The judge ran on a Claude subscription.

## Notes & Caveats

**Excluded tasks (53: 35 English, 18 Chinese).** For each one, an answer built only from the task's inputs fails at least one rubric point. A rubric contradicts the inputs or another rubric (en/cn-018 keeps 7 roadmaps but requires 6 slides), asks for values no input contains, names a file by a template placeholder the instruction never fills in, or points to something that doesn't exist (en-021's video link). Some English inputs translated from Chinese no longer match rubrics written for the Chinese data (en-226: the translation fixed the 13 bugs the rubric grades). Each task's reason is in `excluded_tasks.json`.

**Oracle files.** Workspace-Bench ships rubrics but no reference outputs, so the oracle files were built with AI help. For each task, Claude Code wrote a script that builds the deliverables from the task's inputs (real Word 97 binaries for `.doc` outputs), checked them against every rubric point, and kept notes on any rubric the inputs don't support. The task's own judge then graded them. The Harbor oracle run passes all 723 tasks: 715 on the first pass, 7 more after a rerun (each had lost one rubric point to judge noise and passed unchanged), and en-025 after its answer was rebuilt. The files are not in this repo. They ship with the published dataset, and the adapter takes them with `--oracle-dir`. The exact files that were tested are in the dataset PR at a fixed commit: [`datasets/workspacebench/<task>/solution/output/` @ `553f18e`](https://huggingface.co/datasets/harborframework/harbor-datasets/tree/553f18e7a6086e727e645f62bc791d9fcfbeb152/datasets/workspacebench). The tool that prepared, built and graded them is `scripts/oracle_tools.py`, and the instructions the Claude Code subagents followed are in `scripts/oracle_prompts/builder.md`. The writing itself was done by those subagents, task by task, and each failed rubric point was reviewed by hand, so the build can't be re-run end to end from this repo. The per-task build scripts aren't included, because their outputs reveal the rubrics.

**Oracle disclosure.** For 109 tasks, the oracle writes a value a rubric requires although the inputs lack or contradict it, and for 16 more the answer itself labels the value as an assumption. On these tasks the oracle run checks the grading pipeline, not whether the task can be solved from its inputs. The list is in `oracle_disclosure.md`. Upstream's own rubric-grounding audit of Lite CN (`evaluation/audits/`) marks 453 of 1,850 rubrics as having no basis in the inputs, which agrees with this.

**Rubric problems kept.** en-176, 178, 183, 185, 188 and 194 each have a rubric that names a file by a placeholder the instruction never gives. Their oracles pass, so they are kept, but an agent can't know the name.

**Upstream issues found while building the oracles:**
- The judge view symlinks its inputs, so Claude Code's Glob can't see them, and correct outputs silently score 0. The adapter ships a copy-instead-of-symlink fix.
- Unfilled template placeholders (`__ PH_n __`) in the rubrics of 20 English tasks; in 8 of them the instruction never gives the name.
- Task 290's xlsx inputs are saved HTTP 403 pages, and en-333 ships the wrong input files.
- Task 388's workspace contains a script that builds its expected deck, and an agent can find and run it.
- In five Lite tasks (44, 95, 266, 328, 388), the Lite copy's data folder holds files outside the task's input list, which upstream's judge shows as inputs. This adapter uses the full dataset, so it doesn't have them.

**Differences from upstream's harness.**
- If every judge attempt fails, upstream scores the task 0. Here the verifier exits without writing a reward, so Harbor records the trial as errored instead of as a real 0.
- Upstream's opt-in task patches (9 Chinese Lite tasks, `scripts/apply_task_patches.py`) are not applied, matching upstream's default.
- Upstream's path-gating permission callback isn't used; Harbor's `claude-code` runs with `bypassPermissions`.

**Residual risks.**
- The agent runs as root in its container, which also runs the collect hook and holds Claude Code's log. The judge reads that log as the agent's trace (process rubrics use it), so an agent that rewrote its own log could change the evidence the judge sees. The rubrics, the expected output names and the judge never enter the agent's container, and the judge runs in a fresh container. The original side of the parity experiment has the same exposure.
- The agent has network access (it needs the model API), and the task data, rubrics included, are public on Hugging Face. Task names and prompts don't name the benchmark, but upstream's code under `/opt/wsb` in the image does.

**Comparability.** The paper's numbers are on the Chinese set, and upstream's leaderboard judged with Seed 2.0 Lite. This adapter's judge is `claude-sonnet-5-5`, so absolute scores aren't comparable with the leaderboard.

## Installation / Prerequisites

- Docker, installed and running, with about 40 GB of storage free to build the Researcher image.
- Harbor 0.22.0 or later. The tasks grade in a separate container (`[verifier] environment_mode = "separate"` with the top-level `artifacts` list) and use a `[[verifier.collect]]` hook, both present in Harbor 0.22.0 and later (checked in 0.22.0, 0.25.0 and 0.26.0); `schema_version` is the key name Harbor uses. This adapter was tested with Harbor 0.26.0 (commit `3cb6273`).
- Python 3.12+ and `uv`. Install the adapter's dependencies with:
  ```bash
  cd src/workspacebench
  uv sync
  ```
- Network access to Hugging Face for the task data and the workspaces.
- API keys:
  - for the agent, e.g. `ANTHROPIC_API_KEY` for Claude Code;
  - for the judge: `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (see [Run Evaluation](#run-evaluation--harness)).

## Troubleshooting

- **The Researcher image fails to build.** It needs about 40 GB of Docker storage while building.
- **`429 Too Many Requests` during a build.** The fetch waits for Hugging Face's reset time and retries (8 tries). Building many images at once from one IP can still run out; build a few at a time.
- **Every trial ends in a verifier error.** The judge's credential is probably missing on the host; `[verifier.env]` in `task.toml` passes it in.
- **The agent and the judge use the wrong credentials.** Don't export the agent's key and the judge's token in the same shell. Pass the agent's with `--ae` and the judge's with `--ve`; otherwise Harbor hands an exported OAuth token to the agent container too.
- **Oracle fails with "No reference outputs for this task".** The tasks were generated without `--oracle-dir`.

## Citation

```bibtex
@misc{tang2026workspacebench10benchmarkingai,
      title={Workspace-Bench 1.0: Benchmarking AI Agents on Workspace Tasks with Large-Scale File Dependencies},
      author={Zirui Tang and Xuanhe Zhou and Yumou Liu and Linchun Li and Weizheng Wang and Hongzhang Huang and Jun Zhou and Jiachen Song and Shaoli Yu and Jinqi Wang and Zihang Zhou and Hongyi Zhou and Yuting Lv and Jinyang Li and Jiashuo Liu and Ruoyu Chen and Chunwei Liu and GuoLiang Li and Jihua Kang and Fan Wu},
      year={2026},
      eprint={2605.03596},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2605.03596}
}
```

## Authors & Contributions

- **Adapter:** [@Pranavv-dev](https://github.com/Pranavv-dev) (Pranav Vashistha).
- **Benchmark:** OpenDataBox (see [Citation](#citation)).
- **AI use:** Claude Code wrote most of the adapter code and tests, built and checked the oracle files, and ran the parity experiments; the author reviewed the results.
- Issues and improvements are welcome via the adapters repository.

## Acknowledgement

API inference compute for running parity tests is generously supported by [2077AI](https://www.2077ai.com/) (https://www.2077ai.com/).
