## AIRS-Bench → Harbor Adapter

## Overview

AIRS-Bench (Meta FAIR) evaluates how well LLM research agents carry out end-to-end machine-learning research. Each task gives the agent a research problem, a public HuggingFace dataset (train split plus test inputs without labels) and a metric; the agent must develop a solution and write `submission.csv`, which is scored on held-out test labels and compared with the state-of-the-art value reported in the literature.

- **Source:** [facebookresearch/airs-bench](https://github.com/facebookresearch/airs-bench), paper [arXiv:2602.06855](https://arxiv.org/abs/2602.06855). The code is released under CC BY-NC 4.0.
- **Size:** 20 tasks, drawn from 17 ML papers and 16 datasets across NLP, code, math, molecules/graphs and time series. This adapter converts all 20 tasks found under `airsbench/tasks/rad/` (one Harbor task per directory).
- **Main adaptation choices:** the original `prepare.py`, `evaluate_prepare.py` and `evaluate.py` of every task are reused unchanged; the instruction is the original `project_description.md` plus a short Harbor I/O note; the reward is the paper's normalized score clipped to [0, 1] (see below). One compatibility shim was needed for APPS (see Notes & Caveats).

## What is AIRS-Bench?

AIRS-Bench ("a Suite of Tasks for Frontier AI Research Science Agents") measures autonomous AI-research ability. A task is a `<problem, dataset, metric>` triplet plus a literature SOTA value. Agents (an LLM plus a scaffold) receive the task description and data, run experiments and submit predictions. The benchmark reports an average *normalized score* across tasks, a valid-submission rate and an Elo rating.

The normalized score uses `phi(s) = -log10(|s - s_opt|)`, where `s_opt` is the best achievable score (1.0 for accuracy-like metrics, 0.0 for errors). It is rescaled so that the worst observed run maps to 0 and the SOTA value maps to 1.

## Adapter Features

- Generates one Harbor task per AIRS-Bench task directly from the upstream repository (cloned to a temp dir, or pass `--airs-repo`).
- Reuses the original task scripts, so data preparation and metric code are identical to upstream.
- Agent-visible data (`/workspace/data`) is built at image build time with the original `prepare.py`; raw data containing test labels is deleted from the image afterwards.
- The verifier downloads the public dataset again at verification time, builds the labelled test set with the original `evaluate_prepare.py`, runs the original `evaluate.py` and converts the metric to a reward in [0, 1] using the paper's normalized score with `estimated_worst_score` and the literature SOTA from each task's `metadata.yaml`. Raw metric and normalized score are written to `/logs/verifier/metrics.json`.
- Oracle: submits the gold test labels (task-specific format handling for SQuAD, DuoRC, code retrieval, time series, ELI5 and APPS). A perfect submission gives reward 1.0.
- Stable task ids: kebab-case of the upstream directory name, e.g. `facebookresearch/airs-bench__graph-regression-zinc-mae`.

## Generated Task Structure

```
airs-bench/
├── {task_id}/
│   ├── task.toml
│   ├── instruction.md                # original project description + Harbor I/O note
│   ├── environment/
│   │   ├── Dockerfile                # builds /workspace/data with the original prepare.py
│   │   └── build/                    # prepare.py, download_data.py, dataset.json (+ aux files)
│   ├── solution/
│   │   ├── solve.sh                  # runs gold.py -> /workspace/submission.csv
│   │   ├── gold.py, download_data.py, evaluate*.py, score_config.json, ...
│   └── tests/
│       ├── test.sh                   # evaluate_prepare.py + evaluate.py + normalized score
│       ├── score.py, score_config.json, download_data.py, evaluate*.py, ...
```

Adapter code layout:
```
src/airs-bench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_airs-bench.yaml
└── src/airs_bench/
    ├── adapter.py
    ├── main.py
    ├── shared/           # download_data.py, score.py, gold.py, pyext.py (shim)
    └── task-template/
```

## Run Evaluation / Harness

### Running with Datasets Registry

After the dataset is published to the registry:

```bash
# Oracle agent (reference solution)
uv run harbor run -d facebookresearch/airs-bench

# Your agent and model
uv run harbor run -d facebookresearch/airs-bench -a <agent_name> -m "<model_name>"
```

### Using Job Configurations

```bash
# Run a job with the default adapter configuration
uv run harbor run -c src/airs-bench/run_airs-bench.yaml -a <agent_name> -m "<model_name>"

# Or with a locally prepared dataset path
uv run harbor run -p datasets/airs-bench -a <agent_name> -m "<model_name>"

# Resume a previously started job
uv run harbor job resume -p /path/to/jobs/directory
```

### Running an Individual Trial

```bash
uv run harbor trial start -p datasets/airs-bench/<task_id>
uv run harbor trial start -p datasets/airs-bench/<task_id> -a <agent_name> -m "<model_name>"
```

## Usage: Create Task Directories

```bash
cd src/airs-bench
uv run airs-bench --output-dir ../../datasets/airs-bench
```

Available flags:
- `--output-dir` — directory to write generated tasks (default `datasets/airs-bench` at the repo root)
- `--limit` — generate only the first N tasks
- `--overwrite` — overwrite existing tasks
- `--task-ids` — only generate specific tasks (upstream directory name or generated id)
- `--airs-repo` — local checkout of `facebookresearch/airs-bench` (cloned to a temp dir if omitted)

## Comparison with Original Benchmark (Parity)

**Status: parity experiments have not been run yet.** The plan (agents, models, number of runs, tasks) is to be agreed with the Harbor-Index team before any run. Results will be recorded in `parity_experiment.json` as mean ± sample SEM of the per-task normalized score, and the original-side and Harbor-side runs must use the same agent, model and settings.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|----------------|--------------|--------------------------------|----------------------------|
| TBD | TBD | normalized score | TBD | TBD | TBD | TBD |

Oracle check (gold labels give the optimum). First run locally with the task scripts outside Docker, then with `harbor run` in Docker:

| Result | Tasks |
|--------|-------|
| reward 1.0 | WSC, Winogrande, SVAMP, FinQA, Yelp, SICK (classification, similarity), ZINC, QM9 (cv, g, r2-abs, u0), ELI5, DuoRC, code retrieval, solar weekly, SQuAD (1.0, normalized 0.99991 because the official metric tops out marginally below 1) |
| reward 1.0 (Docker, `harbor run`, 8 GB VM) | rideshare, APPS (raw Pass@5 0.7168: some dataset reference solutions fail the tests; normalized score 6.09 is clipped to 1.0), Kaggle web traffic (needs the memory patch below) |

The other 17 tasks also reach reward 1.0 in Docker (`harbor run -p <dir> -n 2`, oracle agent, 8 GB Docker VM, 17/17 trials, 0 exceptions). All 20 tasks pass the oracle at 100%.

Reproduction commands (to be filled with the agreed agents and models):

```bash
# Original side: run the agent on the upstream AIRS-Bench tasks (MLGym / aira-dojo harness)
# Harbor side:
uv run harbor run -c src/airs-bench/run_airs-bench.yaml -a <agent> -m "<model>"
```

## Notes & Caveats

- **License:** upstream is CC BY-NC 4.0 (non-commercial). Confirm with the Harbor-Index team that redistribution as a Harbor dataset is acceptable.
- **No upstream reference solutions.** The oracle submits the public test labels; it only validates the pipeline, not that a model can reach that score.
- **Compute:** upstream does not state GPU requirements. The current `task.toml` uses provisional values (4 CPUs, 16 GB RAM, 2 h agent timeout, no GPU) and the image installs CPU PyTorch. Training-heavy tasks (QM9, ZINC, APPS, Yelp) may need GPUs and larger budgets; to be confirmed.
- **Data leakage surface:** the datasets are public on HuggingFace and agents have internet access, so test labels are technically reachable, as in the original setup.
- **Network at verification:** the verifier downloads the dataset, so `network_mode = "public"` is required for it.
- **APPS compatibility shim:** upstream `testing_util.py` imports `pyext`, which does not install on Python ≥ 3.10. The adapter ships a minimal `pyext.py` that provides only `RuntimeModule.from_string`. The APPS gold submission uses the dataset's own reference solutions (first five per problem).
- **Pins:** `datasets==3.6.0` (script-based datasets need `trust_remote_code`), `pandas<2.2` (the Monash `rideshare` loader uses the frequency `H`, rejected by newer pandas), and `numpy<2` with `pyarrow<18` (newer pyarrow needs NumPy 2, which breaks `pandas<2.2`).
- **Memory:** the Kaggle web-traffic task (145k series) and APPS (code execution) are memory heavy. Upstream `evaluate.py` of the Kaggle task builds two full columns as python lists (more than 7 GB), so the adapter applies a memory-only patch that reads those columns in batches (`EVALUATE_PATCHES` in `adapter.py`); the MASE computation is untouched. The oracle for this task submits full-length series (zeros for the training part), as upstream `evaluate.py` expects.
- **Reward:** normalized score is clipped to [0, 1]; the upstream score can exceed 1 when a run beats SOTA. The unclipped value is in `metrics.json`.
- **Prompt:** instructions are the upstream project descriptions unchanged, plus a note about the Harbor paths (`/workspace`).

## Installation / Prerequisites

- Docker installed and running
- Harbor installed (`uv tool install harbor`)
- Python environment for the adapter:
  ```bash
  cd src/airs-bench
  uv sync
  ```
- Internet access (HuggingFace dataset downloads at image build and at verification time)
- API keys for the agents/models you run (export as environment variables)

## Troubleshooting

- *`Cannot connect to the Docker daemon`*: start Docker Desktop before `harbor run`.
- *Dataset generation error for `rideshare`*: use `pandas<2.2`.
- *Out-of-memory on Kaggle web traffic or APPS*: lower `n_concurrent_trials` and give the container more memory.
- *Verifier reward 0 and `metrics.json` shows a parsing error*: check that `submission.csv` follows the header given in `instruction.md` for that task.

## Citation

```bibtex
@article{lupidi2026airsbenchsuitetasksfrontier,
  title={AIRS-Bench: a Suite of Tasks for Frontier AI Research Science Agents},
  author={Alisia Lupidi and Bhavul Gauri and Thomas Simon Foster and Bassel Al Omari and Despoina Magka and Alberto Pepe and Alexis Audran-Reiss and Muna Aghamelu and Nicolas Baldwin and Lucia Cipolina-Kun and Jean-Christophe Gagnon-Audet and Chee Hau Leow and Sandra Lefdal and Hossam Mossalam and Abhinav Moudgil and Saba Nazir and Emanuel Tewolde and Isabel Urrego and Jordi Armengol Estape and Amar Budhiraja and Gaurav Chaurasia and Abhishek Charnalia and Derek Dunfield and Karen Hambardzumyan and Daniel Izcovich and Martin Josifoski and Ishita Mediratta and Kelvin Niu and Parth Pathak and Michael Shvartsman and Edan Toledo and Anton Protopopov and Roberta Raileanu and Alexander Miller and Tatiana Shavrina and Jakob Foerster and Yoram Bachrach},
  year={2026},
  eprint={2602.06855},
  archivePrefix={arXiv},
  primaryClass={cs.AI},
  url={https://arxiv.org/abs/2602.06855}
}
```

## Authors & Contributions

This adapter is developed and maintained by Jiankai Sun (sjkai1@126.com) for Harbor-Index.

**Issues and Contributions:**
- Submit issues and pull requests to the main repository
- Follow the project's coding style and commit guidelines
