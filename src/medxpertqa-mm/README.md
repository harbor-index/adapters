<!-- DRAFT: the Harbor-Index guide asks contributors to write the README themselves. Treat this as a fact sheet and rewrite it in your own words before requesting review. Delete this comment. -->

## MedXpertQA-MM → Harbor Adapter

## Overview

This adapter converts **MedXpertQA-MM**, the multimodal track of MedXpertQA, into Harbor tasks.

- **Task type:** five-option (A-E) multiple-choice medical questions, each with 1-6 clinical images (radiology, pathology, dermatology, ECGs, charts, ...) plus patient information in the question text.
- **Domain / language:** medicine, English.
- **Size:** 2,000 tasks, one per question in the MM `test` split. Nothing in that split is excluded.
- **Not adapted:** the 5-question MM `dev` split (few-shot demonstrations only) and the MedXpertQA **Text** subset (2,450 text-only questions).
- **Provenance:** [paper (ICML 2025)](https://arxiv.org/abs/2501.18362), [code](https://github.com/TsinghuaC3I/MedXpertQA), [data](https://huggingface.co/datasets/TsinghuaC3I/MedXpertQA). Code and data are MIT licensed.
- **Main changes for agents:** the original harness sends the question and images to a chat API in two turns; here the images are files in the container and the agent writes its answer letter to `/app/answer.txt`. Scoring logic is unchanged. Details are in [Notes & Caveats](#notes--caveats).

## What is MedXpertQA-MM?

MedXpertQA (Zuo et al., ICML 2025) is a benchmark for expert-level medical knowledge and reasoning with 4,460 questions across 17 specialties and 11 body systems. Its MM subset contains exam-style questions built around real clinical images and patient records, rather than simple image-caption QA. Each question is labelled with a medical task (Diagnosis, Treatment, Basic Science), a body system, and a question type (Reasoning or Understanding).

The original metric is **accuracy**: the predicted option letter is extracted from the model response and compared with the label. The leaderboard is at <https://medxpertqa.github.io>.

## Adapter Features

- Downloads `MM/test.jsonl` and `images.zip` from the HuggingFace dataset at a pinned commit (`7e7c465`, override with `--revision`), or reads a local copy via `--source-dir`.
- One Harbor task per question; upstream IDs are kept (`MM-26` → task `mm-26`, registry name `tsinghuac3i/medxpertqa-mm__mm-26`).
- Each task image contains only that question's images, under `/app/images/` with their original file names.
- Instruction reuses the original zero-shot CoT prompt text (system role, `Q: ... A: Let's think step by step.`).
- Verifier (`tests/verify.py`) is a port of the original `answer_cleansing` and `set_correctness` functions; reward is 1 or 0, so the mean reward is the benchmark accuracy.
- `medical_task`, `body_system`, `question_type` are stored in `task.toml` `[metadata]` for per-category breakdowns.
- `--split parity` generates a deterministic 201-task stratified subset.
- **MedXpertQA LLM agent** (`src/medxpertqa_mm/agent.py`, `medxpertqa_mm.agent:MedXpertQALLMAgent`, reported as `medxpertqa-llm@0.1.0`). The original benchmark is not agentic, so the adapter adds a Harbor agent that mirrors upstream's `APIAgent` under `zero_shot_cot`:
  - request 1: system role + `Q: ... A: Let's think step by step.` + the images as base64 `image_url` parts
  - request 2: the same conversation + the first response + `Therefore, among A through E, the answer is`
  - parameters as upstream: `temperature` 0, `max_tokens` 2048, `logprobs` true, `seed` 0 (and upstream's separate branch for o1/o3/deepseek-reasoner names)
  - the second response is written to `/app/answer.txt`; the model has no tools and no shell
  - the request code (`llm_request.py`) has no Harbor imports; against a mock endpoint, the 402 request bodies for the parity subset were identical to those sent by the original harness

## Generated Task Structure

```
medxpertqa-mm/
├── mm-0/
│   ├── task.toml                 # name, authors, metadata, timeouts
│   ├── instruction.md            # question, image paths, answer format
│   ├── environment/
│   │   ├── Dockerfile            # python:3.11-slim, copies images to /app/images
│   │   └── images/               # this question's images (e.g. MM-0-a.jpeg)
│   ├── solution/
│   │   └── solve.sh              # writes the label letter to /app/answer.txt
│   └── tests/
│       ├── test.sh               # runs verify.py, writes /logs/verifier/reward.txt
│       ├── verify.py             # original answer extraction + exact match
│       └── ground_truth.json     # label and options (only mounted for the verifier)
├── mm-1/
└── ...
```

Adapter code layout:

```
src/medxpertqa-mm/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_medxpertqa-mm.yaml
├── run_medxpertqa-mm_parity.yaml
└── src/medxpertqa_mm/
    ├── __init__.py
    ├── adapter.py
    ├── main.py
    └── task-template/
        ├── task.toml
        ├── instruction.md
        ├── environment/Dockerfile
        ├── solution/solve.sh
        └── tests/{test.sh,verify.py}
    # plus agent.py and llm_request.py (the medxpertqa-llm agent)
```

## Run Evaluation / Harness

### Running with Datasets Registry

Available once the dataset is published to the registry:

```bash
# Oracle agent (reference solution)
harbor run -d tsinghuac3i/medxpertqa-mm

# A specific agent and model
harbor run -d tsinghuac3i/medxpertqa-mm -a <agent_name> -m "<model_name>"

# Parity subset
harbor run -d tsinghuac3i/medxpertqa-mm@parity -a <agent_name> -m "<model_name>"
```

### Using Job Configurations

From the repository root, after generating the tasks locally (see [Usage](#usage-create-task-directories)):

```bash
# Full dataset, oracle by default
harbor run -c src/medxpertqa-mm/run_medxpertqa-mm.yaml

# Parity subset with the parity agent/model
harbor run -c src/medxpertqa-mm/run_medxpertqa-mm_parity.yaml

# Or point at the generated directory directly
harbor run -p datasets/medxpertqa-mm -a <agent_name> -m "<model_name>"
```

Results are written to `jobs/` by default.

### Running Individual Trial

```bash
harbor trial start -p datasets/medxpertqa-mm/mm-0
harbor trial start -p datasets/medxpertqa-mm/mm-0 -a <agent_name> -m "<model_name>"
```

## Usage: Create Task Directories

```bash
cd src/medxpertqa-mm
uv sync

# Full dataset (2,000 tasks)
uv run medxpertqa-mm --output-dir ../../datasets/medxpertqa-mm

# Parity subset (201 tasks)
uv run medxpertqa-mm --split parity --output-dir ../../datasets/medxpertqa-mm-parity
```

Flags:

- `--output-dir`: where to write tasks (default `datasets/medxpertqa-mm` at the repo root)
- `--limit`: generate only the first N tasks
- `--overwrite`: regenerate tasks that already exist (existing ones are skipped otherwise)
- `--task-ids`: only these tasks; accepts `mm-26` or `MM-26`
- `--split`: `full` (default) or `parity`
- `--source-dir`: local dataset copy containing `MM/test.jsonl` and `images/` or `images.zip`
- `--revision`: HuggingFace dataset revision to download (default: the pinned commit)

## Comparison with Original Benchmark (Parity)

**Status: parity experiments have not been run yet.** The table and `parity_experiment.json` will be filled in after the plan is confirmed with the Harbor team.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|----------------|--------------|--------------------------------|----------------------------|
| medxpertqa-llm@0.1.0 | TBD | accuracy (%) | TBD | 201 (10.05%) | TBD | TBD |

**Subset.** 201 of 2,000 tasks. Questions are grouped by (`medical_task`, `question_type`); 10% of each group is drawn with `random.Random(42)` after sorting by upstream ID. `--split parity` reproduces it; the same IDs are listed in the fork at `eval/harbor_parity/parity_ids.txt`.

**Approach.** The original harness calls a chat-completion API directly, so both sides send the same requests:

- *Original side:* fork of TsinghuaC3I/MedXpertQA, branch `harbor-parity`. `main.py`, `utils.py`, `config/` are unmodified. `model/api_agent.py` gains one branch that builds the client from `OPENAI_API_KEY` / `OPENAI_BASE_URL`. `harbor_parity/run_original.py` calls upstream's `complete_item` for the 201 IDs.
- *Harbor side:* the `medxpertqa-llm` agent described under Adapter Features; scoring by the task verifier.

Reproduction (fork URL and commit to be added):

```bash
# Original side
git clone -b harbor-parity <fork of TsinghuaC3I/MedXpertQA> && cd MedXpertQA/eval
pip install -r harbor_parity/requirements.txt
export OPENAI_API_KEY=... OPENAI_BASE_URL=...
python harbor_parity/run_original.py --model <model id> --run-name run1

# Harbor side
cd src/medxpertqa-mm
uv run medxpertqa-mm --split parity --output-dir ../../datasets/medxpertqa-mm-parity
cd ../..
PYTHONPATH=src/medxpertqa-mm/src harbor run -c src/medxpertqa-mm/run_medxpertqa-mm_parity.yaml --max-retries 2
```

Score = accuracy in percent (original: `accuracy_percent` in `result.json`; Harbor: mean reward × 100). Report mean ± sample SEM over runs.

## Notes & Caveats

- **Prompt changes.** Original zero-shot CoT uses a system role (`You are a helpful medical assistant.`), a first user turn (`Q: {question}\nA: Let's think step by step.` plus inline images) and a second turn (`Therefore, among A through E, the answer is`). The Harbor instruction keeps the system-role line and the first-turn text verbatim, lists the image paths, and replaces the second turn with an instruction to write the letter to `/app/answer.txt`. This text is what CLI agents read; the `medxpertqa-llm` agent rebuilds upstream's exact two-turn messages from it.
- **Scoring.** `verify.py` applies the original extraction to the content of `/app/answer.txt`: split on the trigger phrase (or ` answer is `), strip the `A through E` phrases, take the first standalone capital `A`-`E`, compare case-insensitively with the label. A missing or empty file scores 0. The port was compared against upstream `eval/utils.py` on 20,000 synthetic responses with identical results.
- **Model-specific upstream paths not ported.** `eval.ipynb` re-parses outputs of QVQ and DeepSeek-R1 with separate rules (`final answer` / `\boxed{}`); those are tied to raw chat responses and do not apply to a file-based answer.
- **Failed requests.** Upstream records a request that failed 10 times as `No answer provided.` and scores it as incorrect. The `medxpertqa-llm` agent raises an error instead, so the trial has no reward and `--max-retries` runs it again; `run_original.py` re-runs such questions the same number of times. Provider refusals (`content_filter`) are scored as incorrect on both sides, as upstream does.
- **Two ways to run a task.** With `medxpertqa-llm` the model gets upstream's messages and never reads `instruction.md` as written. With CLI agents (claude-code, codex, ...) the agent reads `instruction.md`, opens the image files and writes the letter itself; those scores are not comparable to the original numbers.
- **Images.** CLI agents must be able to open image files. Text-only agents will see the question but not the images.
- **Network.** Tasks do not restrict network access, so agents with web tools could search for questions. The benchmark is public.
- **Dataset size.** The generated dataset contains about 2,850 image files; check the size against the `harbor-datasets` GitHub limits and use the HuggingFace mirror if needed.
- **Medical content.** Clinical images can be graphic. Some model providers may refuse or filter individual questions; those count as incorrect, as in the original (`No answer provided`).

## Installation / Prerequisites

- Docker installed and running
- Harbor CLI: `uv tool install harbor`
- Python 3.11+ and `uv`
- Adapter dependencies:
  ```bash
  cd src/medxpertqa-mm
  uv sync
  ```
- Access to `huggingface.co` to download the dataset (public, no token needed), or a local copy passed with `--source-dir`
- API keys for the agent/model you run, exported as environment variables

## Troubleshooting

- **`Image not found in source data`**: the local `--source-dir` is missing `images/` or `images.zip`, or the archive is incomplete. Re-download `images.zip`.
- **`Generated 0 tasks` / `Skipped N existing tasks`**: the output directory already has those tasks; pass `--overwrite`.
- **HuggingFace download fails or is slow**: download `MM/test.jsonl` and `images.zip` manually and use `--source-dir`.
- **Reward is 0 although the agent answered in chat**: the answer must be written to `/app/answer.txt`; the verifier does not read agent logs.
- **Docker build is slow on the first task**: the `python:3.11-slim` base image is pulled once and cached.

## Citation

```bibtex
@article{zuo2025medxpertqa,
  title={Medxpertqa: Benchmarking expert-level medical reasoning and understanding},
  author={Zuo, Yuxin and Qu, Shang and Li, Yifei and Chen, Zhangren and Zhu, Xuekai and Hua, Ermo and Zhang, Kaiyan and Ding, Ning and Zhou, Bowen},
  journal={arXiv preprint arXiv:2501.18362},
  year={2025}
}
```

## Authors & Contributions

This adapter is developed and maintained by [Anas Zafar](mailto:anaszafar98@gmail.com).

**Issues and Contributions:**
- Submit Issues and Pull Requests to [harbor-index/adapters](https://github.com/harbor-index/adapters)
- Follow the project's coding style and commit guidelines
