## Overview

This adapter converts **100 DeepPlanning Shopping tasks** into Harbor tasks: all 50 cases in level 1 and all 50 in level 2. The English shopping requests require finding products that satisfy several constraints and adding them to a cart. Travel and Shopping level 3 are outside this adapter's scope.

Code comes from [Qwen-Agent](https://github.com/QwenLM/Qwen-Agent/tree/31a4d36d123688581a9e9744427272b33ce940e0/benchmark/deepplanning); data comes from [Qwen/DeepPlanning](https://huggingface.co/datasets/Qwen/DeepPlanning/tree/213876cce679f993a476d01042e13d111c0e3648). Both are released under Apache 2.0. The adapter pins both revisions, verifies archive checksums, and includes upstream licenses in generated tasks.

Local Smol oracle validation passed **100/100 tasks**, with mean reward **1.0**, zero exceptions and zero retries. Model parity and dataset registration are pending.

## What is DeepPlanning Shopping?

[DeepPlanning](https://qwenlm.github.io/Qwen-Agent/en/benchmarks/deepplanning/) evaluates planning in shopping and travel environments. Shopping agents use search, product-detail, cart and user-profile tools. The upstream deterministic evaluator compares the final cart against reference products and coupons. It reports case accuracy and product match rate.

## Adapter Features

- Copies the upstream shopping tools and evaluator without code changes.
- Preserves each level's system prompt and task query, with the dataset-consistent case 36 query described below.
- Provides `shop.py`, a terminal interface to the original tool registry. Each invocation delegates to an upstream tool and records a function-call transcript. Standard Harbor CLI agents can use this interface.
- Provides `deepplanning_shopping.native_agent:ShoppingFnAgent`, a Harbor wrapper that executes the unchanged upstream agent and API caller inside the agent VM. It preserves function schemas, both stages of the cart-check loop, retries and native transcripts. Only runtime paths and lifecycle setup are adapted.
- Replays reference products through the same tools for the oracle.
- Runs trusted grading in a separate verifier VM. Reference answers and evaluator code are absent from the agent environment.
- Supports local Smol with a pinned prebuilt Python image and an optional Docker build variant. The Smol oracle helper prepares dependencies once and branches clean agent and verifier machines from that parent.
- Records code/data revisions, archive hashes, per-file upstream hashes and query provenance in every generated task.

## Generated Task Structure

```text
datasets/deepplanning-shopping/
└── shopping-level1-case-001/
    ├── task.toml
    ├── instruction.md
    ├── provenance.json
    ├── NOTICE.md
    ├── environment/
    │   ├── shop.py
    │   ├── run_native.py
    │   ├── native_agent/          # Unchanged agent/API caller and source hashes
    │   ├── prompt.json
    │   ├── tools/                 # Unchanged upstream tools
    │   ├── state/                 # Catalog, user profile, empty cart
    │   └── UPSTREAM_LICENSE
    ├── solution/
    │   ├── solve.sh
    │   ├── solve.py
    │   └── reference.json
    └── tests/
        ├── test.sh
        ├── verify.py
        ├── evaluation_pipeline.py  # Unchanged upstream grader
        ├── reference.json
        └── UPSTREAM_LICENSE
```

The Docker variant also includes `environment/Dockerfile` and `tests/Dockerfile`. Smol tasks use `environment.docker_image` and loose-file upload; Harbor's Smol provider does not build Dockerfiles. Task names are stable: `qwen/deepplanning-shopping-level{1,2}-case-{001..050}`.

Adapter code lives in `src/deepplanning-shopping/`, with its Python package and task templates under `src/deepplanning_shopping/`.

## Run Evaluation / Harness

Registry publication is pending. Registry commands become available after dataset registration; use the local workflow below for this draft.

Run these commands from `src/deepplanning-shopping/` after installation and task generation:

```bash
# Full oracle run on local Smol, sharing dependency preparation
uv run deepplanning-shopping-oracle --tasks-dir datasets/deepplanning-shopping --jobs-dir jobs --job-name shopping-oracle --concurrency 4

# Validate every saved result, reward file and upstream grader report
uv run deepplanning-shopping-check-oracle jobs/shopping-oracle --expected 100 --output validation/oracle-summary.json

# Reference job configuration (installs dependencies per agent VM)
uv run harbor run -c run_deepplanning-shopping.yaml

# One oracle task
uv run harbor trial start -p datasets/deepplanning-shopping/shopping-level1-case-001 -a oracle -e smol --ek target=local
```

The reference YAML defaults to the oracle. After selecting an agent and model, override with `-a codex -m <model>` and configure that provider's credentials. The oracle uses no inference API or credentials. Results are saved under `jobs/`.

After agreeing model parity settings with Harbor, run the native agent with the same `models_config.json` used on the original side:

```bash
uv run harbor run -p datasets/deepplanning-shopping -a deepplanning_shopping.native_agent:ShoppingFnAgent -m MODEL_ID --ak model_config_path=/absolute/path/models_config.json --ak max_llm_calls=400 -e smol --ek target=local --ek auto_checkpoint=false
```

Use the exact API model ID as both the config key and `model_name`, and export the credential variable named by `api_key_env`. Only the selected model configuration is uploaded; credential values are passed through execution environment variables. The wrapper collects `native-run.txt`, `native-messages.json`, `native-config.json` and `native-summary.json` under the trial's agent logs. `max_llm_calls` applies **per stage**, matching upstream (up to twice that many model calls per task). `execution_timeout_sec` defaults to 1800 and must fit the Harbor trial timeout.

Offline checks use scripted responses with external networking blocked during agent execution:

```bash
DEEPPLANNING_SOURCE_DIR=/path/to/Qwen-Agent DEEPPLANNING_DATA_DIR=/path/to/archive-cache uv run pytest -q
uv run python tests/run_native_smol.py --task datasets/deepplanning-shopping/shopping-level1-case-001 --jobs-dir jobs --job-name native-offline-level1
```

The Smol test uses the original OpenAI SDK against a loopback HTTP fixture and the real isolated grader. Setup can download packages from PyPI. These checks use a dummy key and no paid inference API.

For Docker, generate a separate dataset using `--environment docker`, then run:

```bash
uv run harbor run -p datasets/deepplanning-shopping-docker -a oracle -e docker
```

## Usage: Create Task Directories

```bash
cd src/deepplanning-shopping
uv sync --locked
uv run deepplanning-shopping --output-dir datasets/deepplanning-shopping
```

Generation downloads approximately 1.7 MB of compressed case databases and obtains the pinned Qwen-Agent source using Git. Data is cached under `~/.cache/harbor-adapters/deepplanning/<data-revision>/`.

| Flag | Behavior |
|---|---|
| `--output-dir PATH` | Defaults to `datasets/deepplanning-shopping` relative to the current directory |
| `--limit N` | Generate the first N tasks in stable level/case order |
| `--task-ids ID ...` | Select IDs such as `shopping-level2-case-007` |
| `--overwrite` | Replace selected tasks bearing this adapter's provenance marker |
| `--source-dir PATH` | Use a local Git checkout containing the pinned source commit; committed files are read with `git archive` |
| `--data-dir PATH` | Cache directory containing the pinned `.tar.gz` archives; checksums are always verified |
| `--environment smol` or `docker` | Generate prebuilt-image tasks or Docker build contexts; default `smol` |
| `--split full` or `parity` | All 100 tasks or the first five cases per level (10 tasks); the latter is a proposed diagnostic subset |

## Comparison with Original Benchmark (Parity)

**Model parity has not been run.** Coordinate agents, models, budgets and at least two runs per side with the Harbor team before experiments. `parity_experiment.json` will be added with measured results. The 10-task `parity` selection is available for discussion; no parity subset has been approved or published.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Performance | Harbor Performance |
|---|---|---|---|---|---|---|
| Pending agreement | Pending agreement | Case accuracy; product match rate | 0 | Pending agreement | Not measured | Not measured |

The oracle evidence in [validation/oracle-summary.json](validation/oracle-summary.json) covers all 100 tasks on Harbor 0.25.0 and smolmachines 1.32.4, using local microVMs. Every trial used a separate verifier. Mean case accuracy and aggregate product match rate were both 1.0. Raw trial evidence and reproduction details are included under [validation/](validation/README.md).

Upstream reproduction starts at Qwen-Agent commit `31a4d36d123688581a9e9744427272b33ce940e0`. Install `benchmark/deepplanning/requirements.txt`, then pin `openai==3.50.0`, `numpy==2.2.6` and `rank-bm25==0.2.2` to match the wrapper runtime. Extract the pinned database archives into `benchmark/deepplanning/shoppingplanning/` and apply the case 36 query fix described below. Configure the agreed model in `benchmark/deepplanning/models_config.json` and its API-key environment variable. The native function-calling baseline runs with:

```bash
cd benchmark/deepplanning/shoppingplanning
SHOPPING_AGENT_MODEL=<configured-model> SHOPPING_LEVELS='1 2' SHOPPING_WORKERS=4 SHOPPING_MAX_LLM_CALLS=400 bash run.sh
```

The native Harbor wrapper runs the same upstream `ShoppingFnAgent` and `call_llm.py` byte-for-byte. Use the native-agent command above with the same model configuration, per-stage limit, SDK version, case set and query fix. The wrapper reads the original system prompt and query from `prompt.json`; terminal transport instructions are reserved for CLI agents. Two local Smol integration checks (one per level) passed with a scripted loopback model, exercising the original SDK and real isolated grading. [Native offline evidence](validation/native-offline-summary.json) documents protocol/runtime checks; measured model parity is pending.

The exact experiment plan remains subject to team agreement. A separate CLI-agent parity comparison also needs a matching CLI runner on the original side, with symmetric terminal instructions and settings.

Harbor-side reproduction uses the generation and evaluation commands above, with the agreed `-a` and `-m` settings. Preserve every run's raw reports. Harbor's scalar reward is upstream `case_score`; the mean across cases is case accuracy. Compute upstream product match rate as `sum(matched_count) / sum(expected_count)` from verifier reports, rather than averaging per-case fractions. Report the same case set and metrics on both sides, with mean and sample SEM across repeated runs.

## Notes & Caveats

- **Case 36 query:** the pinned repository's level 1 metadata requests "two specific size 36 items". The released dataset query requests size 44, and its reference products use size 44. The adapter accepts this one exact discrepancy and uses the dataset query. Other discrepancies fail generation. `provenance.json` marks this case `dataset-v1.1`. The fix is proposed in [Qwen-Agent PR #1071](https://github.com/QwenLM/Qwen-Agent/pull/1071) and included in `validation/upstream-case36.patch`; apply it on the original side for comparisons.
- **Prompt and interaction:** native runs consume only the original system prompt and query through the upstream function-calling loop. For terminal agents, `instruction.md` adds terminal tool instructions and a `finish` command. Terminal agents control their own loop and can inspect the catalog files; CLI parity requires symmetric treatment on the original side.
- **Native configuration:** only OpenAI-compatible endpoints supported by the pinned upstream caller are accepted. The wrapper preserves upstream sampling and retry behavior, including omission of zero temperature in API arguments. Set the same configuration and SDK version on both sides. Token/cost totals remain unset because the original runner does not aggregate usage; retain provider billing records for parity costs.
- **Scoring:** upstream grading uses reference product IDs and coupons. Extra products are reported but do not reduce `case_score` when all expected products match. The adapter preserves this behavior and reports completion separately.
- **Scope:** all 100 level 1-2 cases are included. Shopping level 3 and Travel require separate adaptation and validation.
- **Runtime coverage:** all oracle trials ran on Smol. Docker output has generation/schema coverage; Docker execution remains unverified.
- **Smol packaging:** separate verification inherits the pinned Python image. Leaving the verifier's `docker_image` unset lets Harbor upload trusted test files. An explicit verifier image tells Harbor that tests are already bundled. Smol output omits Dockerfiles for loose-file upload; Docker build contexts remain in the adapter templates and Docker output.
- **Network:** initial image/dependency downloads require network access. `shop.py` installs pinned NumPy and rank-bm25 if absent. The Smol oracle helper installs them once before cloning machines. Native setup also installs OpenAI SDK 3.50.0. No benchmark service deployment, cloud account or model key is required for oracle validation.
- **Draft status:** registry publication and model parity are pending. The structural validator reports the missing `parity_experiment.json` until real parity results are recorded.

## Installation / Prerequisites

Requires Git, uv, Python 3.12+, Harbor **0.25.0** and smolmachines **1.32.4**. `uv sync --locked` installs the tested dependencies. The image is pinned to the multi-platform `python:3.12-slim-bookworm` manifest digest in `adapter.py`.

For local Smol, use a supported Apple Silicon macOS host or Linux host with KVM. Check availability with:

```bash
uv run python -c 'import smol; print(smol.local_availability())'
```

The full oracle run used Apple Silicon macOS with concurrency 4 and separate 1-vCPU, 512-MB agent/verifier machines. Docker is needed only for the optional Docker variant. Provider-specific credentials are needed for model-agent runs.

## Troubleshooting

| Symptom | Action |
|---|---|
| Local Smol unavailable | Check virtualization support/KVM and `smol.local_availability()` |
| Checksum mismatch | Remove the indicated cached archive and regenerate; retain checksum verification |
| Pinned commit absent locally | Fetch the pinned revision in `--source-dir`, or omit that option to clone automatically |
| `/tests/test.sh` missing on Smol | Regenerate with `--environment smol`; keep the verifier image unset and omit Dockerfiles in generated Smol tasks |
| Existing task directory | Use `--overwrite` for this adapter's tasks or choose a fresh output directory |
| Dependency download overhead | Use `deepplanning-shopping-oracle` to share one clean dependency-prepared parent |
| Oracle validation fails | Inspect `exception_info`, verifier `error.txt`, `report.json` and agent `oracle.txt`; infrastructure failures remain errors |

## Citation

Cite the original DeepPlanning benchmark by Yinger Zhang, Shutong Jiang, Renhao Li, Jianhong Tu, Yang Su, Lianghao Deng, Xudong Guo, Chenxu Lv and Junyang Lin. Generated tasks credit these authors and retain the upstream Apache 2.0 license.

```bibtex
@misc{zhang2026deepplanning,
  title={DeepPlanning: Benchmarking Long-Horizon Agentic Planning with Verifiable Constraints},
  author={Yinger Zhang and Shutong Jiang and Renhao Li and Jianhong Tu and Yang Su and Lianghao Deng and Xudong Guo and Chenxu Lv and Junyang Lin},
  year={2026},
  eprint={2601.18137},
  archivePrefix={arXiv},
  primaryClass={cs.AI},
  url={https://arxiv.org/abs/2601.18137}
}
```

## Authors & Contributions

Adapter: [Leonardo Gonzalez](mailto:leonardo@kumandayintelligence.com), Kumanday Intelligence. Contributions include task packaging, native-agent lifecycle integration, terminal tool transport, reference replay, isolated verification, provenance and local Smol validation. Submit adapter issues and pull requests to [Harbor adapters](https://github.com/harbor-index/adapters).
