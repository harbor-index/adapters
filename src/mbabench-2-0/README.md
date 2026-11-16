## MBABench 2.0 → Harbor Adapter

## Overview

MBABench 2.0 asks an agent to do what a junior analyst does: take a business case and a starting
workbook and build a complete, working financial model in Excel. This adapter converts the
**101 public tasks** of [MBABench 2.0](https://github.com/namkoong-lab/MBABench-2.0) (Hugging Face
[`namkoong-lab/MBABench-2.0`](https://huggingface.co/datasets/namkoong-lab/MBABench-2.0), dataset
v1.1) into 101 Harbor tasks, one per upstream `task_id`; no task is excluded.

- **Domain:** financial modelling in Excel (DCF, 3-statement, LBO, debt schedules, bond pricing,
  NAV, comps, forecasting, ...), English. Difficulty: 11 Easy, 40 Medium, 30 Medium-Hard, 20 Hard.
- **Deliverable:** `/workspace/solution.xlsx`, built from `starting_files/<case>.xlsx`, with every
  answer on the workbook's `Questions` sheet as a live formula.
- **Grading:** the official MBABench judge, unmodified: 22 deterministic checks plus a Questions-sheet
  answer check, then a single-pass LLM judge (`openai/gpt-5.6-sol`) over the rest of the 129-criterion
  rubric. Reward = the judge's total score / 100.
- **What maps from upstream:** upstream runs agents through four surfaces (vendor chat products in a
  browser, Excel add-ins, raw model APIs with an Excel tool server, coding agents in Docker). This
  adapter reproduces the **coding-agent surface** (`coding-agents/`, prompt template v13), the one
  whose agents (Claude Code, Codex) exist in Harbor. Prompt, workspace and grading are unchanged.

## What is MBABench 2.0?

MBABench 2.0 ([arXiv:2610.31711](https://arxiv.org/abs/2610.31711), Columbia Business School,
ESB Reutlingen and TensorBlock) is a benchmark of 101 original spreadsheet-construction tasks. The
hardest is estimated to take a modeller with 3+ years of experience 33 hours. Rather than
fill-in-the-blank edits, agents build a model from scratch, and quality is judged on 129 criteria in
12 categories (Accuracy, Structure, Error Checks, Formulas, Formatting, Potential Dangers,
Assumptions, Flexibility, Documentation, Model Outputs & Executive Summary, Purpose & Scope,
Rounding) that cover readability, maintainability and auditability as well as correctness.

The paper reports two metrics. **Score** is the weighted sum of the criteria an attempt satisfies
(0-100), which is what the released judge records as `total_score`. **Pass Rate** counts an attempt
as passing when it satisfies *Final calculation accuracy* and violates at most k_i criteria per
importance tier (paper, Table 1). The top agent in the paper passes 16% of tasks.

## Adapter Features

- **Pinned, verified data.** Downloads `namkoong-lab/MBABench-2.0` at a pinned revision (v1.1) and
  checks every file of the selected tasks against the dataset's `MANIFEST.json` (size + sha256), as
  upstream's `scripts/download_dataset.py` does. `--dataset-dir` reuses a local copy.
- **Byte-identical prompt.** `instruction.md` is upstream's `PROMPT.md` (system prompt
  `system_prompt_coding_v1` + task template v13 + workspace listing, prompt_version 113). The three
  prompt files are vendored byte-exact under `src/mbabench_2_0/prompts/` with md5 pins (the v13 pin is
  upstream's own). This was checked against upstream's `run_task.stage_attempt` for all 101 tasks:
  `instruction.md` and every file in `/workspace` match byte for byte.
- **Same workspace.** `/workspace` holds `PROMPT.md`, `HOUSE_STANDARDS.md` and
  `starting_files/<case>.xlsx`, exactly what upstream seeds.
- **Agent environment = upstream sandbox.** `node:22-bookworm-slim`, python3 with openpyxl and pandas,
  Debian's `libreoffice-calc`; 4 CPUs, 8 GB, 4-hour agent timeout. Egress during the agent phase is
  restricted to `api.anthropic.com` / `api.openai.com` (Harbor allowlist in place of upstream's
  iptables firewall).
- **Separate verifier with artifact handoff.** `/workspace/solution.xlsx` is collected from the agent
  container (`artifacts`) and graded in a dedicated verifier container built from `tests/Dockerfile`.
  The agent never sees the golden workbook or the judge, and the judge's API key never enters the
  agent container.
- **Official judge, unmodified.** The verifier image downloads upstream at commit
  `d3fc95f7964260759632d2a1a9d729092f26c9bb` and installs the exact locked dependency set of
  upstream's `uv.lock` (the root workspace environment `setup.sh` creates). It also installs
  **LibreOffice 25.8.7**, the build the judge's LibreOffice gap list was verified against (Debian's 7.4
  cannot evaluate XLOOKUP, LET or FILTER). `tests/mbabench_verifier.py`:
  1. validates the delivery like upstream's `coding_agent/validate.py`: the file exists, opens with
     openpyxl, has at least one sheet and differs from every seeded input. Otherwise the reward is 0;
  2. records it as an upstream attempt row (`task_attempts.jsonl`) and runs upstream's
     `judge/main_scripts/grade.py --all --model openai/gpt-5.6-sol` against a data root holding only
     this task. That covers the deterministic checks, the harness answer check
     (`--accuracy-check harness`), judge v15 with prompt template 8, rubric_9 + weights, and the
     automatic LibreOffice recalculation for workbooks saved without cached values (anything written
     by openpyxl);
  3. writes `total_score / 100` to `/logs/verifier/reward.txt`, plus `mbabench_result.json` (category
     scores, judge version, cost) and the grading's evidence (`grading/`: scores, verdicts,
     deterministic checks, judge conversation).
- **Failures are not scores.** If the judge cannot finish (API error, LibreOffice unavailable, a
  grading marked failed), the verifier exits non-zero without a reward, so Harbor reports a verifier
  error. Upstream re-runs such gradings instead of scoring them.
- **Oracle.** `solve.sh` copies the golden workbook. A delivery byte-identical to the golden scores
  1.0 without a judge call (`MBABENCH_ORACLE_SHORTCIRCUIT=1`, the default). Set it to `0` to grade the
  golden with the real judge.
- **Parity subset.** `--split parity` generates 30 tasks stratified by difficulty in proportion to the
  full set (Easy 3, Medium 12, Medium-Hard 9, Hard 6; `random.Random(2026)` per stratum, listed in
  `adapter.py`).

## Generated Task Structure

```
datasets/mbabench-2-0/
└── mbabench-2-0-task-<N>/                  # N = upstream task_id (1-101)
    ├── task.toml                           # name namkoong-lab/mbabench-2-0-task-<N>; separate verifier; artifacts
    ├── instruction.md                      # == upstream PROMPT.md (prompt_version 113)
    ├── environment/
    │   ├── Dockerfile                      # agent image (mirror of the upstream coding sandbox)
    │   └── workspace/                      # copied to /workspace
    │       ├── PROMPT.md
    │       ├── HOUSE_STANDARDS.md
    │       └── starting_files/<case>.xlsx
    ├── solution/
    │   ├── solve.sh                        # cp golden.xlsx -> /workspace/solution.xlsx
    │   └── golden.xlsx
    └── tests/                              # build context of the verifier image
        ├── Dockerfile                      # LibreOffice 25.8.7 + upstream judge at the pinned commit
        ├── test.sh
        ├── mbabench_verifier.py
        ├── task_config.json                # seeded-file and golden sha256, prompt_version
        └── data/tasks/task_id=<N>/         # the judge's data root for this task (as downloaded)
            ├── task.json
            ├── rubric_suitability.json
            ├── starting_files/<case>.xlsx
            └── solution_files/<case> - Solution.xlsx
```

Adapter code:

```
src/mbabench-2-0/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_mbabench-2-0.yaml
└── src/mbabench_2_0/
    ├── adapter.py                          # download, verify, generate
    ├── main.py                             # CLI
    ├── prompts/                            # byte-exact upstream prompt files (md5-pinned)
    └── task-template/                      # task.toml, instruction.md, environment/, solution/, tests/
```

## Run Evaluation / Harness

### Running with Datasets Registry

After the dataset is published (pending; see Notes & Caveats):

```bash
# Oracle
harbor run -d namkoong-lab/mbabench-2-0

# An agent (needs the agent's key and OPENAI_API_KEY for the judge)
export OPENAI_API_KEY=...            # judge; OPENAI_BASE_URL for a compatible proxy
harbor run -d namkoong-lab/mbabench-2-0 -a claude-code -m anthropic/claude-haiku-4-5-20251001

# Parity subset
harbor run -d namkoong-lab/mbabench-2-0@parity -a claude-code -m anthropic/claude-haiku-4-5-20251001
```

### Using Job Configurations

```bash
# Oracle (default agent in the config)
harbor run -c src/mbabench-2-0/run_mbabench-2-0.yaml

# Local dataset path
harbor run -p datasets/mbabench-2-0 -a oracle
```

`run_mbabench-2-0.yaml` holds commented `claude-code` and `codex` blocks. The `claude-code` block
carries the environment of upstream's `claudecode_anthropic/claude-haiku-4-5` cohort (harness
defaults, identity pins, telemetry switches).

### Running Individual Trial

```bash
harbor trial start -p datasets/mbabench-2-0/mbabench-2-0-task-1 -a oracle
harbor trial start -p datasets/mbabench-2-0/mbabench-2-0-task-1 -a claude-code \
    -m anthropic/claude-haiku-4-5-20251001
```

Judge settings are read from the host when the verifier starts (`[verifier.env]`):

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | (empty) | Judge key; not needed for oracle runs |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint for the judge |
| `MBABENCH_JUDGE_MODEL` | `openai/gpt-5.6-sol` | Grader label from upstream `judge/judge_identities.yaml` |
| `MBABENCH_ORACLE_SHORTCIRCUIT` | `1` | `0` grades a golden delivery with the judge |

## Usage: Create Task Directories

```bash
cd src/mbabench-2-0
uv sync

uv run mbabench-2-0 --output-dir ../../datasets/mbabench-2-0                  # all 101 tasks
uv run mbabench-2-0 --output-dir ../../datasets/mbabench-2-0 --split parity   # 30-task parity subset
uv run mbabench-2-0 --output-dir /tmp/mbabench --task-ids 1 85 --overwrite    # selected tasks
uv run mbabench-2-0 --dataset-dir /path/to/MBABench-2.0/data --output-dir ... # local dataset copy
```

| Flag | Meaning |
|---|---|
| `--output-dir` | Where tasks are written (default `datasets/mbabench-2-0` at the repo root) |
| `--limit N` | First N tasks by upstream id (after `--split` / `--task-ids`) |
| `--overwrite` | Regenerate existing task directories (otherwise they are skipped) |
| `--task-ids` | Upstream task ids (1-101) |
| `--split` | `full` (default) or `parity` |
| `--dataset-dir` | Use a local dataset copy (holding `MANIFEST.json` and `tasks/`) instead of downloading |

## Comparison with Original Benchmark (Parity)

**Not yet run.** The plan below awaits team approval (agents, models, subset, number of runs). The
table will be filled with mean ± sample SEM over the per-run scores once both sides have run.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Performance | Harbor Performance |
|-------|-------|--------|----------------|--------------|----------------------|--------------------|
| claude-code@2.1.251 | claude-haiku-4-5-20251001 | Score (0-100) | 3 (planned) | 30 of 101 (29.7%) | TBD | TBD |

Proposed setup (identical on both sides):

- Agent: Claude Code pinned to one version on both sides (the original side's sandbox image is built
  with `--build-arg CLAUDE_CODE_VERSION=<same>`). Optionally Codex pinned the same way.
- Model: `claude-haiku-4-5-20251001` (upstream cohort `claudecode_anthropic/claude-haiku-4-5`).
- Prompt v13, workspace, 4-hour wall clock, 4 CPUs / 8 GB, egress limited to the model API.
- Judge: upstream commit `d3fc95f`, judge v15, `openai/gpt-5.6-sol` at effort `none`,
  `--accuracy-check harness`, deterministic checks on, LibreOffice 25.8.7, the same endpoint on both
  sides.
- Metric: per-run mean Score over the subset, where an attempt rejected by delivery validation scores
  0. Matching criterion: the run-score ranges overlap.

Original side:

```bash
git clone https://github.com/namkoong-lab/MBABench-2.0 && cd MBABench-2.0
git checkout d3fc95f7964260759632d2a1a9d729092f26c9bb
./setup.sh && uv run python scripts/download_dataset.py --repo namkoong-lab/MBABench-2.0
cd coding-agents
docker build --build-arg CLAUDE_CODE_VERSION=2.1.251 -t mbabench-coding-agent:v2 docker/
uv run python -m coding_agent.run_sweep --config run_configs/claude_haiku_4_5.yaml \
    --task-ids 1,3,6,10,11,18,28,30,31,33,38,39,41,46,50,52,58,59,62,69,72,73,75,79,82,84,87,88,94,98
cd .. && uv run python judge/main_scripts/grade.py --agent-model-name claudecode_anthropic/claude-haiku-4-5
```

Harbor side:

```bash
cd src/mbabench-2-0 && uv run mbabench-2-0 --split parity --output-dir ../../datasets/mbabench-2-0-parity
cd ../.. && harbor run -p datasets/mbabench-2-0-parity -a claude-code \
    -m anthropic/claude-haiku-4-5-20251001 --ak version=2.1.251
# or: uncomment the claude-code block in src/mbabench-2-0/run_mbabench-2-0.yaml
```

Links: original benchmark <https://github.com/namkoong-lab/MBABench-2.0> · adapter PR, dataset PR
and parity upload: to be added.

## Notes & Caveats

- **Redistribution terms are unconfirmed.** The code is Apache-2.0, but the Hugging Face dataset card
  says `license: other` and names no terms. Each generated task embeds the starting and golden
  workbooks, so confirm with the authors that they may be republished before registering the dataset.
- **Metric.** The reward is the official harness's Score (`total_score / 100`). The paper's
  headline Pass Rate needs each criterion's importance tier, which the released judge does not ship
  (`rubric_9.json` has none), so it is not computed here. Confirm with the authors whether Pass Rate
  must be reported.
- **Oracle short-circuit.** Goldens predate some house-standard checks (the deterministic checks fail
  several on task 1's golden, e.g. fonts, zoom, A1 cursor), so the real judge does not give a golden
  100. The 100% oracle therefore relies on the hash short-circuit, as FinanceAgent's oracle does.
  `MBABENCH_ORACLE_SHORTCIRCUIT=0` exercises the real judge on goldens.
- **House standards file.** The v13 coding pipeline stages
  `coding-agents/coding_agent/prompts/house_standards_v1.md` (dated September 2026). Its comment says
  it is a byte-identical copy of `house_standards/House_Standards_v1.md`, but at the pinned commit
  the latter is a different, October 2026 revision. The adapter ships what the coding pipeline
  actually stages; worth raising upstream.
- **Dataset repo id.** Upstream's `scripts/download_dataset.py` defaults to `namkoong-lab/MBABench`,
  which does not resolve publicly; the dataset lives at `namkoong-lab/MBABench-2.0` (pass `--repo`).
- **Deviations from the upstream coding runner:**
  - Egress is enforced by Harbor's allowlist instead of the in-container iptables firewall; the image
    omits iptables, ipset and dnsutils and adds curl for Harbor's agent installers.
  - Upstream runs the agent CLI as the unprivileged `agent` user (uid 1001); Harbor runs it as the
    container's default user.
  - Upstream records each API call through a trajectory relay; Harbor records trajectories itself.
  - openpyxl and pandas are unpinned in the agent image, as upstream; build both sides' images at the
    same time for parity.
  - Upstream's `needs_review` holds (no `solution.xlsx` but another new workbook; optional fast-success
    guard, off by default) are left for a human. In Harbor such a delivery scores 0.
  - An attempt rejected by delivery validation (missing, unreadable or copied-input workbook) scores 0.
    Upstream records it as `agent_failed` without a grading; confirm with the authors that the paper
    counts these as 0.
- **One performance-only patch to the judge.** The deterministic check *Negatives in parentheses*
  (`judge/detchecks/checks/c65.py`, `_commit`) finds each failing block's first cell by scanning
  every failing cell on the sheet. That is quadratic, and on the multi-million-cell workbooks
  (tasks 85, 89 and 99, with 5-10M cells) it ran for over 80 minutes without finishing. Upstream's
  `group_cells()` only produces fully populated rectangles that cover each cell once, so that cell is
  always the block's top-left corner. `tests/Dockerfile` replaces the scan with that direct lookup,
  which returns the same cell, so every verdict and message is unchanged. The build fails if the
  upstream line ever differs. This should be reported upstream.
- **LibreOffice gaps are upstream behaviour.** Many goldens use dynamic-array spill references
  (`A1#`, `ANCHORARRAY`), which LibreOffice 25.8 cannot evaluate. The judge knows these as gap cells.
  Agent workbooks written by openpyxl have no cached values, so the judge recalculates them in
  LibreOffice, and constructs LibreOffice cannot compute surface the same way they do upstream.
  Checked in the verifier image: LibreOffice agrees with Excel's cached values on 580/580 formula
  cells of the gap-free goldens, and the judge detects the version as 25.8.
- **Cost and time.** Upstream quotes about $3.5 and 2 minutes per grading with `openai/gpt-5.6-sol`.
  Three tasks have multi-million-cell workbooks and grade far slower. Measured locally on their goldens,
  with the patch above and before any model call (2 CPUs, 8 GB): task 83 (1.7M cells) 9 min, task 85
  (7.8M) 46 min, task 89 (5.2M) 107 min. The verifier timeout is therefore 3 h; it is a cap, so
  typical tasks are unaffected.
- **Task 99 needs more memory than 8 GB.** Its golden has ~10M cells, and upstream's answer check loads
  each workbook fully with openpyxl, twice. Grading it was killed for lack of memory at both 8 GB and
  14 GB (peak 13.3 GB). It needs a larger verifier (not measurable on the 16 GB test machine) or must be
  excluded; this is still open.
- **Size.** Each task carries its starting workbook twice (`environment/`, `tests/`) and its golden
  twice (`solution/`, `tests/`); the generated 101-task dataset is about 740 MB.

## Installation / Prerequisites

- Docker (or a cloud environment such as Daytona that supports network allowlists and separate
  verifier environments).
- Harbor CLI: `uv tool install harbor`.
- Python 3.12+ and `uv` for the adapter: `cd src/mbabench-2-0 && uv sync`.
- Network access to Hugging Face for task generation, and to GitHub and documentfoundation.org when the
  verifier image builds.
- API keys: the agent's provider key, and `OPENAI_API_KEY` (optionally `OPENAI_BASE_URL`) for the judge.
  Oracle runs need neither.

## Troubleshooting

- **Verifier error with no reward:** the grading did not complete. `verifier/test-stdout.txt` holds
  `grade.py`'s log; check the judge key and endpoint (`OPENAI_API_KEY`, `OPENAI_BASE_URL`) and the
  grader label.
- **Agent cannot reach its API:** the agent phase is allowlisted to `api.anthropic.com` and
  `api.openai.com`. Add a proxy host with the agent's `extra_allowed_hosts` in the job config.
- **Slow or stalled grading on big workbooks:** the judge runs one LibreOffice at a time and waits
  for at least 25% free memory (upstream `det_checks` guard). Lower `n_concurrent_trials` or give the
  verifier more memory (`[verifier.environment] memory_mb`).
- **Verifier image build fails downloading LibreOffice:** the build fetches
  `LibreOffice_25.8.7.3_Linux_<arch>_deb.tar.gz` from `downloadarchive.documentfoundation.org`
  (x86-64 and aarch64 are supported).

## Citation

```bibtex
@misc{poeltl2026mbabench2,
  title         = {MBABench-2.0: Evaluating AI Agents on Constructing Financial Spreadsheets Beyond Simple Verifiability},
  author        = {Poeltl, Julian and Yen, Thomson and Shea, Patrick and Varma, Shubham Anil and Rastogi, Dhruv and Chen, Zihao and Zhang, Mengshi and Namkoong, Hongseok},
  year          = {2026},
  eprint        = {2610.31711},
  archivePrefix = {arXiv},
  url           = {https://arxiv.org/abs/2610.31711}
}
```

## Authors & Contributions

This adapter is developed and maintained by ayush1298 (munotayush6@kgpian.iitkgp.ac.in).

**Issues and Contributions:**
- Submit issues and pull requests to the main repository.
- Follow the project's coding style and commit guidelines.

## Acknowledgement

MBABench 2.0 tasks, golden solutions, rubric and judge are by the MBABench 2.0 authors (Columbia
Business School, ESB Reutlingen, TensorBlock) and released through Snorkel Open Benchmarks.
