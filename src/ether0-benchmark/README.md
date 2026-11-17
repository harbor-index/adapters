## ether0-benchmark → Harbor Adapter

## Overview

This adapter converts FutureHouse's **ether0-benchmark** into Harbor tasks. ether0-benchmark is a
chemistry benchmark in which every answer is a molecule written in SMILES notation; answers are
graded deterministically with RDKit-based reward functions, with no LLM judge.

- **Source**: the `test` split of [`futurehouse/ether0-benchmark`](https://huggingface.co/datasets/futurehouse/ether0-benchmark)
  (revision `c7d5e59960087f360bc32a5006bb994324b38c35`, 325 problems, CC-BY-4.0). The reward code comes from
  [`Future-House/ether0`](https://github.com/Future-House/ether0) at commit
  `b77e0d8eb06f62c8528da72bff22d4c7fee11371` (Apache-2.0).
- **Tasks in this adapter: 250.** These are the problems whose reference answer ships with the dataset
  (graded by ether0's `str_eval`, `product_eval`, `formula_eval` and `functional_group_eval` reward
  functions). The remaining 75 problems (25 each of molecule-completion, oracle-solubility and
  retro-synthesis) do not ship a reference answer, so no oracle solution can be provided for them,
  and they are excluded (see [Notes & Caveats](#notes--caveats)).
- **Main adaptation**: the agent receives exactly the prompt of ether0's published baseline (ether0's
  answer-format instruction followed by the problem text), plus one sentence asking it to write the final
  response to `/app/answer.txt`. The verifier scores that file with ether0's own reward code.

| Category | ether0 reward function | Tasks |
|----------|------------------------|-------|
| reaction-prediction | `product_eval` | 25 |
| molecule-name | `product_eval` | 25 |
| molecule-formula | `formula_eval` | 25 |
| simple-formula | `functional_group_eval` | 15 |
| functional-group | `functional_group_eval` | 10 |
| property-regression-pka | `str_eval` | 25 |
| property-regression-ld50 | `str_eval` | 25 |
| property-regression-adme | `str_eval` | 25 |
| property-cat-safety | `str_eval` | 25 |
| property-cat-smell | `str_eval` | 25 |
| property-cat-eve | `str_eval` | 25 |
| **Total** | | **250** |

## What is ether0-benchmark?

ether0-benchmark is the open test set released with *Training a Scientific Reasoning Model for Chemistry*
([arXiv:2506.17238](https://arxiv.org/abs/2506.17238)) by FutureHouse. It covers common chemistry tasks:
reaction prediction, IUPAC-name-to-structure conversion, molecule design under formula or functional-group
constraints, and multiple-choice property questions (pKa, LD50, ADME, safety, smell and target-activity
classes). Each problem's answer is a single molecule. ether0's published baseline prompts a model with
the problem and extracts the last `<answer>...</answer>` block from its reply. It then scores the
answer with the problem's reward function:
- exact molecule matching for reaction products, names and multiple-choice options;
- RDKit checks of molecular formula and functional groups for the design problems.

The benchmark's metric is accuracy, the mean of the binary per-problem rewards.

## Adapter Features

- **Pinned, reproducible source**: downloads the test split at a fixed Hugging Face revision and sorts
  problems by their upstream UUID. Task names are stable: `futurehouse/ether0-benchmark__<uuid>`.
- **Faithful prompt**: `instruction.md` contains ether0's `LOOSE_XML_ANSWER_USER_PROMPT` verbatim, the
  original problem text, and an instruction to write the final response (with `<answer></answer>` tags)
  to `/app/answer.txt`.
- **Faithful grading**: `tests/grade.py` mirrors the published baseline evaluation. It calls
  `extract_answer_loose` on the response, then
  `EVAL_FUNCTIONS[fxn_name](yhat=..., y=answer_info, test=True)`. The reward is 1.0 or 0.0 and is written
  to `/logs/verifier/reward.txt`.
- **Locked verifier dependencies**: at verification time, `tests/test.sh` creates an isolated virtualenv
  and installs ether0 (pinned commit) plus the 66 runtime packages from ether0's `uv.lock`, including
  RDKit 2025.3.2.
  - The install uses a pinned `uv` (0.9.5, run with `--no-config`); it takes about 20 s, versus several
    minutes with pip, and installs identical versions.
  - Graders and reference answers are never present in the agent's environment.
- **Clean agent environment**: the task image is `python:3.11-slim` plus `git`, `curl` and two system
  libraries RDKit needs at import time. No chemistry toolkit is pre-installed for the agent.
- **Oracle**: `solution/solve.sh` writes the dataset's reference answer in ether0's answer format. The
  reference is `answer_info` for exact-match problems and the dataset's `ideal` molecule for constraint
  problems.
- **Optional prebuilt image / local wheelhouse**: `--docker-image` writes `[environment].docker_image`
  into every task, so all tasks share one image built from the template Dockerfile. The
  `ETHER0_VERIFIER_WHEELHOUSE` verifier variable makes `test.sh` install the same pinned wheels from a
  local wheelhouse instead of PyPI/GitHub. Both are off by default.
- No custom agents were added; standard Harbor agents are used.

## Generated Task Structure

```
ether0-benchmark/
├── {task_id}/                    # upstream problem UUID
│   ├── task.toml                 # name, authors, problem_type / upstream_id metadata, timeouts
│   ├── instruction.md            # ether0 answer-format prompt + problem + output-file instruction
│   ├── environment/
│   │   └── Dockerfile            # python:3.11-slim workspace (WORKDIR /app)
│   ├── solution/
│   │   └── solve.sh              # oracle: writes <answer>reference</answer> to /app/answer.txt
│   └── tests/
│       ├── test.sh               # installs pinned verifier deps, runs grade.py
│       ├── grade.py              # scores /app/answer.txt with ether0's reward function
│       ├── config.json           # the problem's ether0 `solution` spec (reward fn, answer info, type)
│       └── requirements-verifier.txt  # ether0's locked runtime dependencies
```

Adapter code layout:

```
src/ether0-benchmark/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_ether0-benchmark.yaml
└── src/ether0_benchmark/
    ├── __init__.py
    ├── adapter.py
    ├── main.py
    └── task-template/
        ├── task.toml
        ├── instruction.md
        ├── environment/Dockerfile
        ├── solution/solve.sh
        └── tests/{test.sh, grade.py, requirements-verifier.txt}
```

## Run Evaluation / Harness

### Running with Datasets Registry

After the dataset is published (requested tag: `v1.0`):

```bash
# Oracle (reference answers)
harbor run -d futurehouse/ether0-benchmark

# A specific agent and model
harbor run -d futurehouse/ether0-benchmark -a <agent_name> -m "<model_name>"
```

### Using Job Configurations

From the repository root, after generating the tasks (see [Usage](#usage-create-task-directories)):

```bash
# Oracle by default; edit the agents block in the YAML to switch agents
harbor run -c src/ether0-benchmark/run_ether0-benchmark.yaml

# Or point at the generated dataset directly
harbor run -p datasets/ether0-benchmark -a <agent_name> -m "<model_name>"

# Resume a previously started job
harbor job resume -p /path/to/jobs/directory
```

Results are saved in `jobs/` by default.

### Running Individual Trial

```bash
# Oracle on one task
harbor trial start -p datasets/ether0-benchmark/<task_id>

# One task with a specific agent and model
harbor trial start -p datasets/ether0-benchmark/<task_id> -a <agent_name> -m "<model_name>"
```

## Usage: Create Task Directories

```bash
cd src/ether0-benchmark
uv sync
uv run ether0-benchmark --output-dir ../../datasets/ether0-benchmark
```

Available flags:
- `--output-dir`: directory to write generated tasks (default `datasets/ether0-benchmark` at the repo root)
- `--limit`: generate only the first N tasks (in sorted task-id order)
- `--overwrite`: overwrite existing task directories
- `--task-ids`: only generate these task IDs (upstream problem UUIDs)
- `--parquet-path`: use a local copy of the test-split parquet instead of downloading the pinned revision
- `--docker-image`: set `[environment].docker_image` to a prebuilt image of the template Dockerfile

## Comparison with Original Benchmark (Parity)

Parity experiments are **pending**. The plan follows the gpqa-diamond adapter:
- Fork `Future-House/ether0` and add a runner that gives a CLI agent the same `instruction.md` text, then
  grades `/app/answer.txt` with the same reward code.
- Run the same agent and model on that original-side runner and on this adapter, using all 250 tasks.
- Report accuracy as mean ± sample SEM over at least two runs per side; three runs are planned.

The agent, model and number of runs will be finalized with the Harbor team, and results will be
recorded in `parity_experiment.json`.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|------------------|--------------|------------------------------|----------------------------|
| TBD | TBD | Accuracy | TBD | 250 (100% of adapted set) | TBD | TBD |

Reproduction:
- **Original side**: the fork, commit and command will be added with the parity results.
- **Harbor side**:
  ```bash
  harbor run -c src/ether0-benchmark/run_ether0-benchmark.yaml -a <agent> -m "<model>"
  ```
- **Metric**: accuracy = mean of the per-task rewards (each 0 or 1).

## Notes & Caveats

- **Excluded problems (75 of 325).** molecule-completion (`valid_mol_eval`), oracle-solubility (`sol_eval`)
  and retro-synthesis (`rxn_forward`) problems do not include a reference answer in the released dataset,
  so the oracle-100% requirement cannot be met for them. The latter two types are also scored through
  ether0's model-based remote reward server.
- **Prompt modification.** The only change to ether0's baseline prompt is the final sentence asking the
  agent to write its response to `/app/answer.txt`. Parity runs apply the same text on both sides.
- **Tools.** ether0's baseline is a single LLM call without tools. Harbor agents run in a container with
  shell access and network access, so they may install packages (e.g. RDKit) themselves. The image does
  not pre-install chemistry toolkits. Parity runs give both sides the same agent and environment.
- **Verifier network access.** `tests/test.sh` downloads about 200 MB of pinned wheels from PyPI and
  ether0 from GitHub on every verification. Verification typically takes about one minute per trial.
  Set `ETHER0_VERIFIER_WHEELHOUSE` (via `--ve`) to a local wheelhouse for repeated or offline runs.
- **Pinned RDKit.** Molecule canonicalization can differ between RDKit releases, so the verifier uses
  ether0's locked RDKit 2025.3.2 rather than the latest release.
- **Reward-function exceptions.** If a reward function raises on a malformed answer, the task scores 0.
  ether0's baseline script would stop with the exception instead.
- **Dockerfile notes.**
  - `APT::Sandbox::User "root"` lets `apt` work in user-namespace runtimes such as rootless Podman with a
    single UID mapping.
  - `libxrender1` and `libxext6` are required by RDKit's wheels at import time.
  - Neither affects scoring.

## Installation / Prerequisites

- Docker (or another Harbor-supported container runtime) installed and running.
- Harbor installed (`uv tool install harbor`).
- The adapter's Python environment:
  ```bash
  cd src/ether0-benchmark
  uv sync
  ```
- Network access to Hugging Face (task generation), and to PyPI and GitHub from task containers
  (verification), unless a local wheelhouse is configured.
- API keys for the agent/model you evaluate, exported as environment variables.

## Troubleshooting

- **Reward 0 with `ImportError: libXrender.so.1`**: the image is missing RDKit's system libraries; rebuild
  from the template Dockerfile (it installs `libxrender1` and `libxext6`).
- **Verifier times out or fails during `pip install`**: the container cannot reach PyPI/GitHub. Check
  proxy settings (pass them with `--ve`), or set `ETHER0_VERIFIER_WHEELHOUSE` to a wheelhouse holding:
  - the wheels in `requirements-verifier.txt`;
  - `uv==0.9.5`;
  - an ether0 wheel built from the pinned commit.
- **Image build fails at `apt-get` under rootless runtimes**: keep the `APT::Sandbox::User` line in the
  Dockerfile.
- **Slow per-task image builds**: build the template Dockerfile once and regenerate tasks with
  `--docker-image <image>` so all tasks reuse it.

## Citation

```bibtex
@misc{narayanan2025ether0,
  title={Training a Scientific Reasoning Model for Chemistry},
  author={Siddharth M. Narayanan and James D. Braza and Ryan-Rhys Griffiths and Albert Bou and Geemi Wellawatte and Mayk Caldas Ramos and Ludovico Mitchener and Samuel G. Rodriques and Andrew D. White},
  year={2025},
  eprint={2506.17238},
  archivePrefix={arXiv},
  url={https://arxiv.org/abs/2506.17238}
}
```

## Authors & Contributions

This adapter is developed and maintained by [Yi Jiang](mailto:lloyd_jahn@sjtu.edu.cn) as part of the
Harbor-Index v2.0 effort.

**Issues and Contributions:**
- Submit issues and pull requests to the main repository
- Follow the project's coding style and commit guidelines
