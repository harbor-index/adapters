# TabArena

## Overview

The `tabarena` adapter generates the official **TabArena-Lite v0.1** selection: 51 datasets, one native outer split each (repeat 0, fold 0). Adapter and dataset names are both `tabarena`.

## What is TabArena?

[TabArena](https://github.com/autogluon/tabarena) is a living benchmark for tabular machine learning. Systems fit on training data and predict held-out rows. This adapter pins source revision `bfa8c50c17885e5b722d75ce5bcb5db3822257a3`, preserving native splits, class ordering, test permutation and scoring.

## Adapter Features

Agents receive typed `train.json` and public feature metadata. They fit a model and write `/workspace/submission/predict.py` plus fitted artifacts and a reproducible `train.py`. The frozen predictor runs as `python predict.py QUERY_DIRECTORY OUTPUT_JSON`; it reads feature-only `test.json` and returns `classes` and an N×K `predictions` matrix (N×1 for regression).

Raw `native_error` is binary 1−AUROC, multiclass log loss, or regression RMSE; lower is better. Errors remain per dataset. `metric_optimal` compares against the same scorer's exact optimum, including its floating-point log-loss floor. It is an oracle diagnostic, not learned-model performance.

## Generated Task Structure

```text
tabarena/<task-id>/
├── task.toml, instruction.md
├── environment/Dockerfile, data/  # training inputs
├── solution/                    # privileged reference outputs
└── tests/                       # protected labels and native verifier
```

## Run Evaluation / Harness

Registry publication is pending; use local tasks. All commands below assume the working directory is `src/tabarena`. After installation and generation:

```bash
harbor run -c run_tabarena.yaml
```

The Modal config defaults to oracle and includes a standard Codex alternative. Select an authorized Modal profile/environment first. Its summary expects 51 tasks; for a deliberately selected subset, set `TABARENA_EXPECTED_TASKS` to the planned count. Missing rewards cannot increase coverage. Confirm distinct task IDs in job results before claiming full coverage.

## Usage: Create Task Directories

```bash
uv run tabarena --output-dir datasets/tabarena
```

Use `--limit`, `--task-ids` or `--overwrite` as needed. Downloads default to `$XDG_CACHE_HOME/harbor/tabarena` or `~/.cache/harbor/tabarena`; `--cache-dir` overrides this. Original metadata bytes, source URLs and hash pins are bundled.

## Comparison with Original Benchmark (Parity)

Formal model parity has not run; no parity results or publication links exist yet. Native oracle cross-validation instructions are in [scripts/NATIVE.md](scripts/NATIVE.md). To reproduce Harbor oracle validation (not model parity):

```bash
harbor run -c run_tabarena.yaml
```

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|
| Pending | Pending | Native error per dataset | 0 | 51 | Not measured | Not measured |

## Notes & Caveats

The oracle uses privileged reference outputs through ordinary prediction and scoring; it does not demonstrate learned generalization. The adapter adds a CLI-agent fitting interface, not the paper's prescribed model/HPO experiments.

Test features are withheld until prediction; test labels remain in a separate verifier environment. Prediction runs unprivileged with a read-only bundle and no network. Fitting allows only the harness-selected inference host; package-install hosts are available during setup only. Codex is the validated standard-agent route; other agents require separate validation. Runtime limits are 600 seconds fitting, two CPUs, 4 GiB memory, 180 seconds prediction and a 256 MiB bundle.

Only the Lite split is included. Dataset licenses and citations are preserved; review redistribution terms before publishing generated data. No upstream scoring fixes are applied.

## Installation / Prerequisites

Requires Python 3.12, uv and Modal credentials. From the repository root:

```bash
cd src/tabarena
uv sync --locked
uv tool install 'harbor[modal]==0.26.0' --with 'modal==1.6.4'
```

## Troubleshooting

Run from `src/tabarena` to resolve config paths. Modal permission errors require an authorized profile/environment. Hash mismatches indicate corrupt or changed source assets. Invalid predictor outputs are reported in verifier logs.

## Citation

```bibtex
@article{erickson2026tabarena,
  title   = {TabArena: A Living Benchmark for Machine Learning on Tabular Data},
  author  = {Erickson, Nick and Purucker, Lennart and Tschalzev, Andrej and Holzm{\"u}ller, David and Desai, Prateek and Salinas, David and Hutter, Frank},
  journal = {Advances in Neural Information Processing Systems},
  volume  = {38},
  year    = {2026}
}
```

## Authors & Contributions

Benchmark: Nick Erickson, Lennart Purucker and coauthors cited above. Adapter: Joachim Baumann (joachimbaumann1@gmail.com).
