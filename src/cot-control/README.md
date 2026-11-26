# CoT-Control → Harbor Adapter

## Overview

This adapter converts the question-answering part of **CoT-Control** into Harbor tasks.
CoT-Control measures *chain-of-thought (CoT) controllability*: whether a reasoning model
can follow an instruction about its own reasoning trace (for example "reason only in
uppercase" or "do not use the word *chromosome* while reasoning") while it solves a task.

- **Task type**: multiple-choice questions from GPQA (445), Humanity's Last Exam (469) and
  MMLU-Pro (300), each paired with one of nine reasoning instructions.
- **Size**: **10,836 tasks**. The paper counts 1,214 questions × 9 instruction types =
  10,926 QA instances. The released code skips the 45 questions that have no keyword data
  for the two word-suppression types (45 × 2 = 90), which leaves the 10,836 instances the
  upstream harness actually runs. This adapter generates exactly those.
- **What is scored**: the model's reasoning trace, not its answer. The reward is 1 when
  the trace complies with the instruction.
- **Provenance**: [paper](https://arxiv.org/abs/2603.05706),
  [code and data](https://github.com/YuehHanChen/CoTControl) (MIT license), pinned at
  commit `5d78aeffe0152ba087c2d31cd07712d029c64785`. The authors ask that CoT-Control be
  excluded from training data; their marker string is kept in every generated `task.toml`.
- **Not adapted**: the agentic part of CoT-Control (SWE-bench Verified and BFCL, 3,150
  instances) and the unconstrained `baseline` mode. See [Notes & Caveats](#notes--caveats).
- **Modifications**: none to prompts or grading. The instruction is the upstream prompt
  byte for byte, and the verifier runs the upstream grading files unmodified.
- **Status**: parity was run on a 675-task subset with `deepseek-v4.1-flash`, three runs
  per side; the run-score ranges overlap on every metric. See
  [Comparison with Original Benchmark](#comparison-with-original-benchmark-parity).
- **Judge used for the recorded results**: `deepseek-v4.1-flash`, **not upstream's
  `gpt-5-mini`**. This applies to the parity runs on both sides and to the oracle run.
  Judged numbers are not comparable with the paper's.

## What is CoT-Control?

CoT-Control ([Reasoning Models Struggle to Control their Chains of Thought](https://arxiv.org/abs/2603.05706),
Chen Yueh-Han et al., 2026) is an evaluation suite for a safety-relevant capability: if a
model could shape what appears in its chain of thought, CoT monitoring would be less
reliable. The suite appends a reasoning instruction to each problem and checks the
reasoning trace. Instruction types fall into three categories with three difficulty
levels each:

| Category | Easy | Medium | Hard |
|----------|------|--------|------|
| Information suppression | `word_suppression` | `multiple_word_suppression` | `ignore_question` |
| Information addition | `repeat_sentences` | `end_of_sentence` | `meow_between_words` |
| Stylistic modification | `lowercase_thinking` | `uppercase_thinking` | `alternating_case` |

Original metrics, each reported as a macro-average over (dataset, instruction type) pairs:

- **Controllability Score** (primary): share of instances whose reasoning complies.
- **Meta Discussion Rate**: among compliant instances, the share whose reasoning mentions
  the instruction itself.
- **Controllability Score without Meta-Discussion**: compliant and no such mention.

A higher score means the model controls its reasoning *more*, which the paper treats as
the less safe direction.

## Adapter Features

- **Exact prompts.** `instruction.md` is the user message upstream sends, produced by the
  upstream prompt module vendored byte-for-byte (`src/cot_control/upstream/prompts.py`).
  Row filtering, keyword choice and the answer key are ports of `run_cceval.py`.
- **Upstream grading, unmodified.** Each task ships `grading.py` and
  `grade_compliance_csv.py` exactly as published. `tests/run_grader.py` only feeds them
  one row and writes the reward files. Eight instruction types are graded by rules; the
  `ignore_question` type and the meta-discussion metric use the upstream LLM judge
  prompts (gpt-5-mini, a compliant `ignore_question` verdict must be confirmed twice more).
- **Rewards** (`/logs/verifier/reward.json`):
  - `reward`: compliance, 0 or 1. Its mean is the Controllability Score.
  - `correctness`: 1 if the answer letter is correct.
  - `controllability_without_meta_discussion`: present when the meta-discussion judge ran.
- **No silent zeros.** If the reasoning trace is missing or the judge is required but
  unavailable, the verifier writes no reward and exits non-zero, so the trial is reported
  as an error instead of a score of 0.
- **CoT-Control agent** (`src/cot_control/agent.py`, `cot_control.agent:CoTControlLLMAgent`).
  CoT-Control-QA is a single-call benchmark, so the adapter adds a Harbor agent that mirrors
  upstream's `OpenRouterLLM`: one chat-completion request whose only message is the
  instruction, `max_tokens=25000`, `temperature=0.0`, and a 25,000-token reasoning budget
  for the models upstream treats as reasoning models. It stores the returned reasoning and
  answer in `/logs/agent/cot_control/` for the verifier and writes an ATIF trajectory. The
  model has no tools and no access to the task container.
- **Oracle.** Upstream has no reference reasoning. The oracle writes a short trace built
  to satisfy the instruction plus the correct letter. For the eight rule-based types the
  trace is checked against the upstream grader while tasks are generated.
- **Pinned, verified inputs.** The three dataset files are fetched at the pinned commit
  and verified by SHA-256. Task ids are row positions in those files.
- **Fidelity check.** `python -m cot_control.check_fidelity` compares every prompt and
  answer key with the upstream functions, the agent's request body with upstream's, and
  grades a few thousand synthetic outputs with both the upstream command line and the
  task verifier.
- **Aggregation.** `python -m cot_control.aggregate` computes the paper's macro-averaged
  metrics from a Harbor job directory or from upstream graded CSV files with one code path.

## Generated Task Structure

```
cot-control/
├── gpqa-0007-word-suppression/        # <dataset>-<row>-<instruction type>
│   ├── task.toml                      # name = cot-control/cot-control__gpqa-0007-word-suppression
│   ├── instruction.md                 # the upstream prompt, byte for byte
│   ├── environment/
│   │   └── Dockerfile                 # python + the grader's dependencies
│   ├── solution/
│   │   ├── solve.sh                   # writes the oracle trace
│   │   └── oracle_trace.json          # compliant reasoning + correct answer
│   └── tests/
│       ├── test.sh                    # runs run_grader.py
│       ├── run_grader.py              # harness around the upstream graders
│       ├── grading.py                 # upstream, unmodified
│       ├── grade_compliance_csv.py    # upstream, unmodified
│       ├── UPSTREAM_LICENSE
│       └── config.json                # instruction type, prompt, answer key
```

Adapter code:

```
src/cot-control/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_cot-control.yaml
└── src/cot_control/
    ├── adapter.py            # task generation
    ├── main.py               # CLI
    ├── constants.py          # pinned commit, checksums, instruction types
    ├── oracle.py             # oracle traces
    ├── agent.py              # CoTControlLLMAgent
    ├── request.py            # the upstream request body and response parsing
    ├── aggregate.py          # macro-averaged metrics
    ├── check_fidelity.py     # comparison against upstream
    ├── upstream_graders.py
    ├── upstream/             # vendored upstream prompts.py + license
    └── task-template/
```

## Run Evaluation / Harness

The verifier reads these variables from the host:

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | LLM judge. Required for `ignore_question` tasks; optional otherwise |
| `OPENAI_BASE_URL` | Optional OpenAI-compatible endpoint for the judge |
| `COT_CONTROL_JUDGE_MODEL` | Judge model, default `gpt-5-mini-2025-08-07`. Another model on the `OPENAI_BASE_URL` endpoint can be named; see [Notes & Caveats](#notes--caveats) |
| `COT_CONTROL_META_DISCUSSION` | `auto` (default: judge meta-discussion when a key is set), `required`, or `skip` |
| `COT_CONTROL_JUDGE_MAX_RETRIES` | Attempts per judge API call when the call fails. Default: upstream's 3. A returned verdict is never asked again |

### Running with Datasets Registry

Available once the dataset is published:

```bash
# Oracle
harbor run -d cot-control/cot-control

# CoT-Control agent (needs this repository on PYTHONPATH and OPENROUTER_API_KEY)
PYTHONPATH=src/cot-control/src harbor run -d cot-control/cot-control \
  -a cot_control.agent:CoTControlLLMAgent -m deepseek/deepseek-r1
```

### Using Job Configurations

```bash
# From the repository root; oracle by default
harbor run -c src/cot-control/run_cot-control.yaml
```

To run a model, enable the commented `CoTControlLLMAgent` block in the YAML and prefix
the command with `PYTHONPATH=src/cot-control/src`.

### Running with Local Path

```bash
harbor run -p datasets/cot-control                                   # oracle
PYTHONPATH=src/cot-control/src harbor run -p datasets/cot-control \
  -a cot_control.agent:CoTControlLLMAgent -m deepseek/deepseek-r1 \
  --ak reasoning=auto
```

Agent options (`--ak key=value`): `api_base`, `api_key_env`, `max_tokens`,
`reasoning_tokens`, `temperature`, `reasoning` (`auto`/`on`/`off`),
`request_timeout_sec`, `extra_body` (JSON merged into the request, for example to pin an
OpenRouter provider). The defaults reproduce upstream.

Any OpenAI-compatible endpoint works if it returns the model's raw reasoning in
`message.reasoning_content` or `message.reasoning`. Name the endpoint and the variable
that holds its key:

```bash
PYTHONPATH=src/cot-control/src harbor run -p datasets/cot-control \
  -a cot_control.agent:CoTControlLLMAgent -m deepseek-v4.1-flash \
  --ak api_base=https://<gateway>/v1 --ak api_key_env=MY_GATEWAY_KEY
```

### Running Individual Trial

```bash
harbor trial start -p datasets/cot-control/gpqa-0007-word-suppression
```

### Computing the Benchmark Metrics

Harbor's built-in mean weights every task equally. The paper's numbers are macro-averages
over (dataset, instruction type) pairs:

```bash
cd src/cot-control
uv run python -m cot_control.aggregate --job-dir ../../jobs/<job-name>
```

## Usage: Create Task Directories

```bash
cd src/cot-control
uv sync

# Full dataset (10,836 tasks)
uv run cot-control --output-dir ../../datasets/cot-control

# Parity subset (675 tasks, see the parity section)
uv run cot-control --split parity --output-dir ../../datasets/cot-control-parity
```

Flags:

- `--output-dir`: where to write tasks (default `datasets/cot-control` at the repo root)
- `--limit`: first N tasks; the first 27 cover every dataset × instruction type pair
- `--overwrite`: replace existing task directories
- `--task-ids`: only these ids, e.g. `gpqa-0007-word-suppression`
- `--split`: `full` (default) or `parity`
- `--datasets`, `--modes`: restrict to some source datasets or instruction types
- `--source-dir`: local checkout of the upstream repository at the pinned commit; without
  it the three dataset files are downloaded from GitHub and cached in
  `~/.cache/harbor-cot-control`
- `--prompt-model-name`: model name passed to the upstream prompt builder (see
  [Notes & Caveats](#notes--caveats))
- `--parity-rows-per-dataset`, `--parity-seed`: parameters of the parity subset
- `--export-upstream-subset DIR`: also write the selected questions as upstream-format CSV
  files, for running the same subset with the original harness

Check the result against upstream:

```bash
uv run --with python-dotenv python -m cot_control.check_fidelity \
  --upstream-dir /path/to/CoTControl --dataset-dir ../../datasets/cot-control
```

## Comparison with Original Benchmark (Parity)

Parity was run on a 675-task subset, three runs on each side, on 2026-11-25. For all three
metrics the ranges of the run scores overlap, which is the adapter guide's matching
criterion.

> **Deviation from upstream: the LLM judge was `deepseek-v4.1-flash`, not `gpt-5-mini`.**
> Upstream grades the `ignore_question` type and the meta-discussion check with
> `gpt-5-mini`. The API key available for these runs served `deepseek-v4.1-flash` only, so
> that model was the judge on both sides, with upstream's prompts, request parameters and
> verdict logic. Affected: the `ignore_question` tasks (75 of the 675) and the
> "w/o meta-discussion" metric. Not affected: the other eight instruction types, which are
> graded by rules, and answer accuracy. The judged numbers are not comparable with the
> paper's. The saved traces can be graded again with `gpt-5-mini` without new model runs.

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|
| cot-control-llm@0.1.0 | deepseek-v4-1-flash-261017 | Controllability Score (macro %) | 3 | 675 (6.2%) | 1.5802 ± 0.2750 | 1.3333 ± 0.2263 |
| cot-control-llm@0.1.0 | deepseek-v4-1-flash-261017 | Controllability Score w/o meta-discussion (macro %) | 3 | 675 (6.2%) | 0.0494 ± 0.0494 | 0.0000 ± 0.0000 |
| cot-control-llm@0.1.0 | deepseek-v4-1-flash-261017 | Answer accuracy (macro %) | 3 | 675 (6.2%) | 68.2654 ± 0.6465 | 67.2881 ± 0.7288 |

Scores are percentages, macro-averaged over the 27 (dataset, instruction type) pairs as in
the paper. The uncertainty is the sample SEM over the three runs. Scores of the single
runs, as recorded in `parity_experiment.json`:

| Metric | Original runs | Harbor runs | Ranges overlap |
|--------|---------------|-------------|----------------|
| Controllability Score | 1.9259, 1.7778, 1.0370 | 0.8889, 1.4815, 1.6296 | yes |
| Controllability Score w/o meta-discussion | 0.0000, 0.1481, 0.0000 | 0.0000, 0.0000, 0.0000 | yes |
| Answer accuracy | 67.0185, 68.5926, 69.1852 | 66.4568, 68.7407, 66.6667 | yes |

This model almost never controls its reasoning, which is the paper's general finding for
reasoning models (its abstract quotes 2.7% for Claude Sonnet 4.5). Only the two
word-suppression types have compliant instances (`word_suppression`: 10.2% original and
8.0% Harbor; `multiple_word_suppression`: 4.0% on both sides). The other seven types score
0 on both sides in every run. A run therefore has 6 to 13 compliant instances out of 675,
and one instance moves the Controllability Score by 0.15 points. Nearly every compliant
trace mentions the instruction, so the score without meta-discussion is 0 in five of the
six runs and its ranges overlap only at that floor. Answer accuracy is the one measure
here with a base rate far from 0.

Links:

- Original benchmark: <https://github.com/YuehHanChen/CoTControl>, commit
  `5d78aeffe0152ba087c2d31cd07712d029c64785`
- Fork with the original-side scripts:
  <https://github.com/RZFan525/CoTControl/tree/harbor-parity> (branch `harbor-parity`,
  directory `harbor_parity/`)
- Adapter PR: <https://github.com/harbor-index/adapters/pull/37>
- Dataset PR: <https://github.com/harbor-framework/harbor-datasets/pull/310>
- Parity results on Hugging Face: <https://huggingface.co/datasets/harborframework/parity-experiments/discussions/313>.
  `harborframework/parity-experiments` is over the Hub's limit of 1,000,000 files and
  rejects pushes at the moment
  ([harbor-framework/adapters#129](https://github.com/harbor-framework/adapters/issues/129)),
  so the files are mirrored in the same layout at
  <https://huggingface.co/datasets/Vfrz/cot-control-harbor-parity/tree/b16d817859ff49eddc9e45f1f71b5e7b37ffef1a/adapters/cot-control>
  and will be pushed to that PR unchanged once pushes are accepted.

**Setup** (the same on both sides unless stated)

- *Subset.* `--split parity` takes 25 questions per source dataset (those with keyword
  data, chosen by a seeded hash, seed 42) and all nine instruction types: 3 × 25 × 9 = 675
  tasks, 6.2% of the 10,836. The full set would have cost sixteen times as much per run.
  `--export-upstream-subset` writes the same questions as upstream-format CSV files, which
  the original harness reads through `--data`.
- *Original side.* Upstream at the pinned commit with `run_cceval.py`, `prompts.py`,
  `grading.py` and `grade_compliance_csv.py` unmodified. One line of `llm.py` was changed
  so that `OPENROUTER_BASE_URL` can name another OpenAI-compatible endpoint. The scripts
  in the fork's `harbor_parity/` run the 27 (dataset, instruction type) pairs, send failed
  requests again and call the grader. Python 3.12 with the versions pinned in
  `harbor_parity/requirements.txt`.
- *Harbor side.* Harbor 0.27.0 with Docker and the agent
  `cot_control.agent:CoTControlLLMAgent` (`cot-control-llm@0.1.0`).
- *Model request.* Model name `deepseek-v4.1-flash` on an OpenAI-compatible gateway. Every
  response stored on the Harbor side names the snapshot `deepseek-v4-1-flash-261017`; the
  upstream harness does not store that field. The request holds one user message (the
  prompt), `max_tokens` 25000 and `temperature` 0.0. There is no system prompt, no tool
  and no `reasoning` field (upstream adds one only for model names that contain `r1`,
  `thinking`, `qwen3`, `magistral` or `oss`). The request timeout is 300 s. The reasoning
  trace is read from `message.reasoning_content`.
- *Failed requests.* Up to three requests per instance: `--max-retries 2` in Harbor and
  `ATTEMPTS=3` in the fork's `run_pair.py` (upstream alone sends each prompt once). An
  instance that still has no answer is left out of the averages on both sides. This
  happened in run 1 only, to 2 instances on the original side and 4 on the Harbor side,
  all of them timeouts.
- *Judge.* `deepseek-v4.1-flash` on both sides (`COT_CONTROL_JUDGE_MODEL`), with
  `COT_CONTROL_JUDGE_MAX_RETRIES=6`. Upstream's judge is `gpt-5-mini`; the key available
  for these runs served the DeepSeek model only. Prompts, request parameters and verdict
  logic are upstream's. The judge decides the `ignore_question` type and the
  meta-discussion check, so those two numbers are not comparable with the paper's. The
  other eight types are graded by rules.
- *Concurrency.* 16 concurrent trials in Harbor. On the original side three
  `run_cceval.py` processes with 8 concurrent requests each.
- *Order.* A 27-task check on both sides (one task per dataset and instruction type),
  then run 1 on both sides and a comparison, then runs 2 and 3 on both sides at the same
  time.
- *Interruption of run 1.* The host went to sleep when 91 Harbor trials and 3 of the 27
  original-side pairs had finished. After it woke, the Harbor job continued, and one trial
  whose verifier had timed out during the sleep was run again with `harbor jobs resume`.
  On the original side the other 24 pairs were run from scratch. No result was dropped
  because of its score.

**Evidence that the remaining differences are sampling noise**

The model is not deterministic at temperature 0: on the original side alone the
Controllability Score ranges from 1.04 to 1.93. The following checks were made on the
saved outputs of all six runs, without further model requests:

- *Same input.* In each run, the 675 request bodies saved by the Harbor agent are
  identical to the bodies that the unmodified upstream code builds for the same subset
  (2,025 of 2,025).
- *Same grading.* The Harbor traces were graded again by the original pipeline, and the
  original traces by the task verifier. Compliance agrees on every instance of the eight
  rule-based types (1,798 of 1,798 in each direction) and correctness on every graded
  instance (2,021 of 2,021 and 2,023 of 2,023). The `ignore_question` type was left out of
  this check because it needs the judge.
- *Per-task overlap.* 24 tasks were compliant at least once: 12 on both sides, 5 on the
  original side only and 7 on the Harbor side only. No task was compliant in every run on
  one side and in none on the other. Among the 669 tasks graded in all six runs, 12 have
  more compliant runs on the original side and 9 on the Harbor side; for correctness the
  counts are 56 and 42 (two-sided sign tests: p = 0.66 and p = 0.19).

With about ten compliant instances per run, this experiment cannot detect a small
systematic difference in the Controllability Score for this model. The two equality checks
above are the stronger evidence that both sides measure the same quantity.

**Reproduction**

Generate the subset once. The CSV files it exports are the ones stored in the fork's
`harbor_parity/parity_subset/`:

```bash
cd src/cot-control
uv run cot-control --split parity --output-dir ../../datasets/cot-control-parity \
  --export-upstream-subset /path/to/parity-subset
cd ../..
```

Both sides read the model and judge endpoints from these variables:

```bash
export GATEWAY=https://<gateway>/v1           # an OpenAI-compatible endpoint that serves the model
export GATEWAY_KEY=...
export OPENAI_API_KEY=$GATEWAY_KEY OPENAI_BASE_URL=$GATEWAY       # judge
export COT_CONTROL_JUDGE_MODEL=deepseek-v4.1-flash COT_CONTROL_JUDGE_MAX_RETRIES=6
```

Original side, with the fork's `harbor-parity` branch:

```bash
git clone -b harbor-parity https://github.com/RZFan525/CoTControl && cd CoTControl
uv venv --python 3.12 && uv pip install -r harbor_parity/requirements.txt
export OPENROUTER_API_KEY=$GATEWAY_KEY OPENROUTER_BASE_URL=$GATEWAY
for run in 1 2 3; do
  JOBS=3 MAX_CONCURRENCY=8 ATTEMPTS=3 harbor_parity/run_original.sh \
    harbor_parity/parity_subset deepseek-v4.1-flash parity_orig_r$run
done
```

Harbor side, from the root of this repository:

```bash
for run in 1 2 3; do
  PYTHONPATH=src/cot-control/src harbor run -p datasets/cot-control-parity \
    -a cot_control.agent:CoTControlLLMAgent -m deepseek-v4.1-flash \
    --ak api_base=$GATEWAY --ak api_key_env=GATEWAY_KEY \
    -n 16 --max-retries 2 --job-name parity_harbor_r$run --yes
done
```

Scores, with the same code for both sides:

```bash
cd src/cot-control
uv run python -m cot_control.aggregate --job-dir ../../jobs/parity_harbor_r1
uv run python -m cot_control.aggregate --upstream-csv \
  /path/to/CoTControl/CoT-Control-QA/logs/parity_orig_r1/*/graded_results/*_graded.csv
```

## Notes & Caveats

**Deviations from the paper and upstream**

- *Instance count.* 10,836 tasks rather than the paper's 10,926; see Overview.
- *Agent subset excluded.* The SWE-bench Verified and BFCL part is not adapted. Upstream
  publishes no script that turns its trajectories into the graded table, does not specify
  how multi-turn reasoning is concatenated, and leaves `inspect_ai`/`inspect_evals`
  unpinned, so a faithful port could not be validated.
- *`baseline` mode excluded.* It carries no instruction, so there is nothing to score for
  controllability, and it is not part of the paper's instance count.
- *Judge model.* Upstream hard-codes the alias `gpt-5-mini`. The adapter defaults to the
  dated snapshot `gpt-5-mini-2025-08-07` and exposes `COT_CONTROL_JUDGE_MODEL`. Another
  judge model gets the same prompts and request parameters, and the verdict logic is
  unchanged, but `ignore_question` scores and the meta-discussion measures are then not
  comparable with the paper's. The eight rule-based types do not involve the judge.
  **The recorded parity runs (both sides) and the oracle run used `deepseek-v4.1-flash`
  as the judge instead of `gpt-5-mini`**, because the API key available for them served
  that model only.
- *Prompt wording depends on the model.* Upstream says "analysis channel" for model names
  containing `gpt-oss` and "reasoning stage" otherwise. Harbor instructions are static:
  the default dataset uses "reasoning stage". Generate a gpt-oss variant with
  `--prompt-model-name openai/gpt-oss-120b`.

**Upstream behaviour kept as is** (reported here rather than changed on one side)

- *Answer key of GPQA row 29.* The correct option text is the single letter `c`, which
  upstream's `convert_answer_to_letter` reads as option C although the text is option A.
  The adapter keeps upstream's key. This affects `correctness` on that question only.
- *GPQA row 74* lists the correct option text twice; upstream takes the first.
- *Answer extraction fallback* only recognises letters A–D when a response lacks the
  `ANSWER: X` format, although HLE and MMLU-Pro have up to 26 and 10 options.
- *Resume merges unrelated results.* `run_cceval.py` resumes by default and looks up
  earlier results by model and mode only (the mode by substring). Runs of different
  datasets or modes that share a `--log_dir` therefore copy each other's rows into their
  output files. Use `--no-resume` on the original side; `cot_control.aggregate` also drops
  rows duplicated across the files it is given.
- *Empty reasoning scores 0.* If the API returns no reasoning trace, rule-based types
  score 0, as upstream. A model or provider that hides its reasoning therefore looks
  uncontrollable. Check `reasoning_chars` in `grading_details.json` before trusting scores.

**Using the dataset**

- *The reasoning trace must come from the harness.* The verifier reads
  `/logs/agent/cot_control/reasoning.txt`. Asking a tool-using agent to write its own
  reasoning there would measure output control, a different and much easier quantity, and
  the agent could simply write a compliant file. Standard CLI agents are therefore not
  supported yet; without a captured trace the verifier reports an error, not 0.
- *Models must expose raw reasoning.* The paper notes that its raw CoTs of OpenAI models
  were obtained through a collaboration with OpenAI.
- *`ignore_question` needs the judge.* Without `OPENAI_API_KEY` those 1,214 tasks end
  with a verifier error. The other 9,622 tasks get their main reward without any key.
- *Reward direction.* A reward of 1 means the model controlled its reasoning.
- *Macro vs. micro.* Use `cot_control.aggregate` for numbers comparable to the paper.
- *Oracle.* The oracle demonstrates that a compliant trace exists and that the verifier
  accepts it; it is not a model-produced solution. It was run on all 10,836 tasks with
  Harbor 0.27.0 (finished 2026-11-26): reward 1 on every task. The 9,622 rule-based tasks
  ran without an API key. The 1,214 `ignore_question` tasks were judged by
  `deepseek-v4.1-flash`, with three compliant verdicts per task as upstream requires.
- *Runtime.* Tasks are light, but each one starts a container. On a 10-core laptop a
  rule-based oracle trial took 14 s on average and the machine finished about 20 trials
  per minute, with 4 concurrent trials as with 10, so the 9,622 rule-based tasks need
  about 8 hours. An `ignore_question` oracle trial waits for four judge calls, 2.7 minutes
  on average with the judge endpoint used here. A parity run of 675 tasks took between 50
  minutes and 2 hours at 16 concurrent trials, depending on the model endpoint. All tasks
  share one Dockerfile, so the image is built once and cached.

## Installation / Prerequisites

- Docker running, and Harbor installed (`uv tool install harbor`; developed against 0.27.0)
- Python 3.11+ and `uv`; from `src/cot-control`: `uv sync`
- Network access to GitHub when generating tasks without `--source-dir`
- `OPENAI_API_KEY` for the judge; `OPENROUTER_API_KEY` (or another OpenAI-compatible
  endpoint that returns reasoning) for the CoT-Control agent

## Troubleshooting

- **`RewardFileNotFoundError` on `ignore_question` tasks**: `OPENAI_API_KEY` is not set
  for the verifier. See `verifier/test-stdout.txt` in the trial directory.
- **"No reasoning trace was captured for this run"**: the task was run with an agent other
  than the oracle or `CoTControlLLMAgent`, or the agent's model request failed.
- **`ModelRequestError`**: the model request failed or exceeded `request_timeout_sec`
  (300 s, as upstream). The trial has no reward. `harbor run --max-retries N` runs such
  trials again; upstream itself sends every prompt once and records the failure.
- **Every reward is 0 and `reasoning_chars` is 0**: the endpoint returned no reasoning.
  Try `--ak reasoning=on`, or use a model and provider that return the trace.
- **`all predefined address pools have been fully subnetted`**: every running trial holds
  one Docker network, and Docker's default address pools were exhausted. The machine used
  for the parity runs allowed 29 networks; keep the total of `-n` over all jobs below the
  limit of yours.
- **`ModuleNotFoundError: cot_control`**: add `PYTHONPATH=src/cot-control/src`.
- **"does not match the dataset pinned at upstream commit"**: `--source-dir` points at a
  different revision of the upstream repository.
- **Harbor asks to confirm environment variables**: pass `--yes` in scripts.

## Citation

```bibtex
@misc{yuehhan2026reasoningmodelsstrugglecontrol,
      title={Reasoning Models Struggle to Control their Chains of Thought},
      author={Chen Yueh-Han and Robert McCarthy and Bruce W. Lee and He He and Ian Kivlichan and Bowen Baker and Micah Carroll and Tomek Korbak},
      year={2026},
      eprint={2603.05706},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2603.05706},
}
```

## Authors & Contributions

This adapter is developed and maintained by Run-Ze Fan (runze.fan@icloud.com)

**Issues and Contributions:**
- Submit issues and pull requests to the adapters repository.
- Questions about the benchmark itself belong with the
  [upstream repository](https://github.com/YuehHanChen/CoTControl).
