## PHYBench → Harbor Adapter

## Overview

PHYBench tests whether a model can solve original, text-only physics problems (mechanics, electricity and magnetism, thermodynamics, optics, modern and advanced physics) and give the result as a single symbolic expression. This adapter turns the **100 publicly released PHYBench problems that include reference answers** (`PHYBench-fullques_v1.json`) into 100 Harbor tasks, one task per problem.

- Source: [Hugging Face `Eureka-Lab/PHYBench`](https://huggingface.co/datasets/Eureka-Lab/PHYBench) at revision `d6d91c787b7abb865eb2490a328bf85a9f5095f0`; scorer from [phybench-official/phybench](https://github.com/phybench-official/phybench) at commit `d9db3ec7246f3678aaee65d44a32649ad93beea2`. Both are MIT-licensed.
- Task count: 100. The paper describes 500 problems, but the other 400 are released as questions only (`PHYBench-onlyques_v1.json`) with no answers, so they cannot be graded and are not included.
- Main adaptation: the paper's prompt is kept word for word. We add two sentences (write the response to `/app/answer.txt`; only the last `\boxed{}` is graded), and the original-side parity runner uses the same text.

## What is PHYBench?

PHYBench ([Qiu et al., 2025, arXiv:2504.16074](https://arxiv.org/abs/2504.16074)) is a set of original physics problems written and reviewed by physics students at Peking University. Every problem has one unambiguous symbolic answer. Models are prompted to reason step by step and put the final LaTeX expression in `\boxed{}`. The paper reports two metrics:

- **EED score** (0–100): both expressions are converted to SymPy and simplified, then compared with a tree edit distance (an extended Zhang–Shasha algorithm). The score is 100 for an equivalent expression, `60 − 100·r` for relative distance `0 < r < 0.6`, and 0 otherwise.
- **Accuracy**: the fraction of problems with EED score 100.

## Adapter Features

- Downloads the pinned dataset revision and the pinned upstream EED scorer (`EED/EED.py`, `EED/extended_zss.py`, `EED/latex_pre_process.py`). The scorer is copied verbatim into each task's `tests/eed/`. `--data-file` and `--eed-dir` accept local copies for offline generation.
- Grading follows the paper. `tests/phybench_answer.py` takes the content of the last `\boxed{...}` in `/app/answer.txt` (or the whole file when it has no box) and calls upstream `EED(reference, answer)`. The parity fork imports this same file, so extraction and scoring are identical on both sides.
- Rewards are written to `/logs/verifier/reward.json`, with the primary reward also in `reward.txt`: `reward = eed_score / 100` (the primary reward), `eed_score` (0–100) and `accuracy` (0 or 1).
- Grading is isolated. `[verifier] environment_mode = "separate"` and `artifacts = ["/app/answer.txt"]`: only the answer file is copied into a fresh verifier container built from the same image. The agent never sees `tests/`, the reference answer or the reference solution, and cannot change the scorer or its Python packages. `test.sh` deletes any old reward file, runs `/usr/local/bin/python3 -I`, and exits non-zero without writing a reward if scoring itself fails.
- Agent-visible files (`instruction.md` and the image) do not name the benchmark and do not contain source ids, URLs or answers.
- Task names are stable and registry-safe: `phybench/phybench-<source id>`, for example `phybench/phybench-495`.

## Generated Task Structure

```
phybench/
├── phybench-495/
│   ├── task.toml                 # name, authors, timeouts, separate verifier, artifacts
│   ├── instruction.md            # paper prompt + "write your response to /app/answer.txt"
│   ├── environment/
│   │   └── Dockerfile            # python:3.11-slim + pinned sympy 1.14.0, latex2sympy2_extended 1.10.1, numpy, timeout-decorator
│   ├── solution/
│   │   └── solve.sh              # writes the reference answer to /app/answer.txt
│   └── tests/
│       ├── test.sh               # runs score.py, writes /logs/verifier/reward.json
│       ├── score.py
│       ├── phybench_answer.py    # boxed-answer extraction + EED call (shared with the parity fork)
│       ├── ground_truth.json     # reference answer
│       └── eed/                  # upstream EED scorer, verbatim, plus its LICENSE
└── ...
```

Adapter code:

```
src/phybench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_phybench.yaml             # oracle (default) and commented agent blocks
├── phybench_parity_codex.yaml    # parity configuration
└── src/phybench/
    ├── __init__.py
    ├── adapter.py
    ├── main.py
    └── task-template/
```

## Run Evaluation / Harness

Run from the registry once the dataset is published:

```bash
# Oracle
harbor run -d phybench/phybench

# An agent and model
harbor run -d phybench/phybench -a codex -m "MODEL"
```

Run from locally generated tasks (development and reproduction):

```bash
# Oracle on all 100 tasks (default config)
harbor run -c src/phybench/run_phybench.yaml

# Parity configuration
harbor run -c src/phybench/phybench_parity_codex.yaml

# Single task
harbor trial start -p datasets/phybench/phybench-495
```

Results go to `jobs/`. Each trial's `verifier/reward.json` holds `reward`, `eed_score` and `accuracy`, and `verifier/details.json` holds the extracted answer.

## Usage: Create Task Directories

```bash
cd src/phybench
uv run phybench --output-dir ../../datasets/phybench
```

Flags:
- `--output-dir`: where tasks are written (default `datasets/phybench` at the repository root)
- `--limit N`: only the first N tasks, sorted by source id
- `--overwrite`: replace existing task directories
- `--task-ids`: only these tasks, e.g. `--task-ids 495 phybench-25`
- `--data-file`, `--eed-dir`: use local copies instead of downloading

## Comparison with Original Benchmark (Parity)

PHYBench does not ship an inference harness: the paper queries each model once with a fixed prompt, extracts the boxed answer and scores it with EED. To compare like with like, the original side runs the same CLI agent outside Harbor, in a fork of the upstream repository ([`dreiw2580/phybench`, branch `harbor-parity`](https://github.com/dreiw2580/phybench/tree/harbor-parity)). For each problem it starts a container from the same image, runs `codex exec` with the same pinned version, model, flags and instruction text, reads `/app/answer.txt`, and scores it with the fork's own upstream `EED/EED.py` through the same `phybench_answer.py`.

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|
| codex@TODO_VERSION | TODO_MODEL | EED score | 3 | 100 (100%) | TODO | TODO |
| codex@TODO_VERSION | TODO_MODEL | Accuracy (%) | 3 | 100 (100%) | TODO | TODO |

Values are mean ± sample SEM over runs; raw run scores are in `parity_experiment.json`. Oracle: 100/100 tasks at reward 1.0, 0 exceptions (Harbor 0.27.0, Docker; see Notes & Caveats).

Reproduce the original side (in the fork):

```bash
cd harbor_parity
docker build -f Dockerfile.agent --build-arg CODEX_VERSION=TODO_VERSION -t phybench-codex:TODO_VERSION .
export OPENAI_API_KEY=... OPENAI_BASE_URL=...
python3 run_codex_original.py --model TODO_MODEL --codex-version TODO_VERSION --run-name run1 --workers 8
python3 run_codex_original.py --model TODO_MODEL --codex-version TODO_VERSION --run-name run2 --workers 8
python3 run_codex_original.py --model TODO_MODEL --codex-version TODO_VERSION --run-name run3 --workers 8
```

Reproduce the Harbor side:

```bash
cd src/phybench && uv run phybench --output-dir ../../datasets/phybench && cd ../..
harbor run -c src/phybench/phybench_parity_codex.yaml   # repeat for 3 runs
```

Links: [adapter PR](https://github.com/harbor-index/adapters/pull/44), [dataset PR](https://github.com/laude-institute/harbor-datasets/pull/TODO), [parity artifacts](https://huggingface.co/datasets/harborframework/parity-experiments/discussions/TODO).

## Notes & Caveats

- **Subset.** Only the 100 problems with public answers are adapted. The 400 question-only problems cannot be graded.
- **Prompt.** The paper prompt (Appendix D) is unchanged. Two sentences are added on both sides: write the response to `/app/answer.txt`, and only the last `\boxed{}` is graded. The paper's single LLM call is replaced by a CLI agent on both sides, which can use a shell and Python (SymPy is installed in the image).
- **Answer extraction.** The paper extracts the boxed answer but publishes no extraction code. `phybench_answer.py` uses the last `\boxed{...}` (or `\fbox{...}`). The same function is applied to the reference answer, because four reference answers (ids 95, 401, 409, 505) are themselves wrapped in `\boxed{}`; every other reference passes through unchanged apart from surrounding whitespace.
- **Oracle.** `solve.sh` writes the reference answer verbatim, so the extracted answer is identical to the extracted reference, and upstream `EED` returns 100 through its exact-match check. Result on Harbor 0.27.0 with Docker: 100/100 at reward 1.0, 0 exceptions.
- **Scorer exceptions.** If upstream `EED` raises on a malformed answer, that answer scores 0. The same file is used on both sides.
- **Parity infrastructure.** Harbor side: Daytona sandbox, with codex installed by Harbor at run time. Original side: local Docker, with the same codex version preinstalled in the image and an 1800 s timeout per task. Agent flags, instruction, image packages and scorer are identical.
- **Missing answer.** If the agent does not write `/app/answer.txt`, the task scores 0, as an unanswered response does upstream. If the scorer itself crashes, `test.sh` writes no reward and the trial is reported as a verifier failure, not as a score of 0.
- **Scorer behaviour inherited from upstream.** EED returns 0 when the answer is more than three times longer than the reference, or when either expression contains `\int` or `\sum`. SymPy simplification has a 30 s limit and equality checking a 10 s limit (`timeout_decorator`, Linux only).

## Installation / Prerequisites

- Docker (or a Daytona or Modal account for cloud runs)
- Harbor: `uv tool install harbor`
- Adapter dependencies (standard library only): `cd src/phybench && uv sync`
- For parity: an OpenAI-compatible API key and base URL for the chosen model

## Troubleshooting

- `Failed to download ...` during generation: Hugging Face or GitHub is unreachable. Download `PHYBench-fullques_v1.json` and a checkout of the upstream repository, then pass `--data-file` and `--eed-dir`.
- A trial ends with no reward: read `verifier/test-stdout.txt` in the trial directory. This means scoring crashed, which is different from a wrong answer.
- Slow verification: SymPy simplification of long expressions can take up to about 40 s per task. The verifier timeout is 600 s.

## Citation

```bibtex
@article{qiu2025phybench,
  title={PHYBench: Holistic Evaluation of Physical Perception and Reasoning in Large Language Models},
  author={Qiu, Shi and Guo, Shaoyang and Song, Zhuo-Yang and Sun, Yunbo and others},
  journal={arXiv preprint arXiv:2504.16074},
  year={2025}
}
```

## Authors & Contributions

This adapter is developed and maintained by [dreiw2580](https://github.com/dreiw2580).

**Issues and Contributions:**
- Submit issues and pull requests to [harbor-index/adapters](https://github.com/harbor-index/adapters).

## Acknowledgement

> API inference compute for running parity tests is generously supported by [2077AI](https://www.2077ai.com/) (https://www.2077ai.com/).
