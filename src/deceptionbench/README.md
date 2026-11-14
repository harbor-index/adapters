# DeceptionBench → Harbor Adapter

## Overview

This adapter implements the **single-turn L1/L2** portion of [DeceptionBench](https://github.com/Aries-iai/DeceptionBench). It produces **900 English-language tasks**: 150 scenarios × six conditions, across economy, education, healthcare, social interactions, and entertainment. Healthcare contributes 30 scenarios, or 180 tasks. Multi-turn L3 is outside the current scope.

Each task presents one original scenario prompt. The native API agent preserves the original prompt and response-format suffix. A separate verifier uses the original LLM judge prompt to label both thought and response as `honest` or `decept`. Harbor's scalar reward is response honesty; its report also retains the original deception labels and metrics.

**Status:** the 10-task real-judge oracle smoke run passed (10/10, `gpt-4o`, 2026-11-14). Full 900-task oracle validation and measured parity are pending. The dataset has not been published to the Harbor registry. Upstream declares CC BY-SA 4.0 and separately states academic-research-only use; see [NOTICE.md](NOTICE.md).

## What is DeceptionBench?

[DeceptionBench: A Comprehensive Benchmark for AI Deception Behaviors in Real-world Scenarios](https://arxiv.org/abs/2510.15501) is a NeurIPS 2025 Datasets and Benchmarks paper by Yao Huang, Yitong Sun, Yichi Zhang, Ruochen Zhang, Yinpeng Dong, and Xingxing Wei. It studies deception under self/other perspectives, neutral conditions, reward, pressure, and multi-turn feedback. The original evaluator reports deceptive thought and deceptive response rates. This adapter concerns that benchmark, not SWE-DeceptionBench or another similarly named dataset.

## Adapter Features

- Downloads the five pinned source JSON files, verifies their SHA-256 hashes, and generates stable task names. `--data-dir` accepts a local copy with the same hashes.
- Supports `L1-self`, `L1-other`, `L2-self-pressure`, `L2-self-reward`, `L2-other-pressure`, and `L2-other-reward`.
- Provides `deceptionbench.native_agent:DeceptionBenchAgent` (`deceptionbench-native@0.1.0`). It reads `/workspace/prompt.txt`, sends one user message per attempt through the OpenAI-compatible API, and writes the normalized response to `/workspace/response.txt`. Model IDs are passed unchanged to the endpoint.
- Uses `max_tokens=500`, `temperature=0.0`, and `top_p=0.95` for generation and judging, two SDK retries, and up to three outer attempts with one- and two-second delays.
- Supports ordinary Harbor CLI agents through `instruction.md`, which adds a file-delivery instruction. This route has no demonstrated parity with the original native API protocol.
- Runs the verifier in a separate container, receiving the response artifact, reference metadata, and dedicated judge credentials.
- Includes an independent original-harness reproduction bridge and a groundtruth-based oracle candidate, validated on ten smoke tasks so far.

## Generated Task Structure

```text
datasets/deceptionbench/
└── deceptionbench-healthcare-000-l1-self/
    ├── task.toml
    ├── instruction.md
    ├── environment/
    │   ├── Dockerfile
    │   └── prompt.txt         # Public original prompt + original suffix
    ├── solution/
    │   └── solve.sh           # Groundtruth-based oracle candidate
    └── tests/
        ├── Dockerfile        # Separate verifier build context
        ├── test.sh
        ├── verify.py
        ├── protocol.py
        └── metadata.json     # Reference sample and provenance; verifier only
```

The adapter package is `src/deceptionbench/`; its Python sources and task templates are under `src/deceptionbench/src/deceptionbench/`. Generated tasks belong in `datasets/`. Harbor rematerializes the submitted `/workspace/response.txt` at the same path in the separate verifier container. Verifier outputs are `/logs/verifier/reward.txt` and `/logs/verifier/report.json`.

## Run Evaluation / Harness

### Running with Datasets Registry

Registry evaluation is not available yet. Use local generated task paths until a dataset PR is merged and the Harbor team publishes the dataset.

### Using Job Configurations

Run from `src/deceptionbench/`, after configuring the dedicated credentials below:

```bash
uv sync --extra native
uv run deceptionbench --output-dir ../../datasets/deceptionbench
uv run harbor run -c run_deceptionbench.yaml
```

The YAML defaults to the oracle candidate. This runs full validation and incurs judge API costs; only the ten-task smoke run has been measured so far. To select the native API agent:

```bash
uv run harbor run -c run_deceptionbench.yaml \
  --agent-import-path deceptionbench.native_agent:DeceptionBenchAgent -m gpt-4o
```

`gpt-4o` is the upstream example model ID. Replace it with the exact model agreed with the Harbor team before measured experiments; use the same fixed model identifier, judge, and endpoint settings on both sides. Results are saved under `jobs/`.

### Running Individual Trial

```bash
uv run harbor trial start \
  -p ../../datasets/deceptionbench/deceptionbench-healthcare-000-l1-self -a oracle
uv run harbor trial start \
  -p ../../datasets/deceptionbench/deceptionbench-healthcare-000-l1-self \
  --agent-import-path deceptionbench.native_agent:DeceptionBenchAgent -m gpt-4o
```

## Usage: Create Task Directories

```bash
cd src/deceptionbench
uv sync
uv run deceptionbench --output-dir ../../datasets/deceptionbench

# Healthcare: 30 scenarios × six conditions = 180 tasks.
uv run deceptionbench --domains healthcare --output-dir ../../datasets/deceptionbench-healthcare

# Proposed diagnostic subset: first scenario per dimension, all six conditions.
uv run deceptionbench --split parity --output-dir ../../datasets/deceptionbench-parity

# Generation makes no model calls.
uv run deceptionbench --limit 6 --output-dir ../../datasets/deceptionbench-smoke
```

| Flag | Meaning |
| --- | --- |
| `--output-dir PATH` | Destination; defaults to this repository's `datasets/deceptionbench`. |
| `--data-dir PATH` | Optional directory containing the five pinned domain JSON files. |
| `--cache-dir PATH` | Download cache for pinned files. |
| `--domains NAME ...` | One or more of `economy`, `education`, `healthcare`, `social`, `entertainment`. |
| `--conditions NAME ...` | One or more exact condition names listed above. |
| `--split full\|parity` | Full 900 tasks or the deterministic 90-task diagnostic subset. |
| `--task-ids ID ...` | One or more exact stable task IDs. |
| `--limit N` | Keep the first N selected tasks. |
| `--overwrite` | Replace existing selected task directories. |

`--split parity` selects the first source scenario in each of 15 dimensions and all six conditions: 15 × 6 = 90 tasks. It is a proposed diagnostic sample, not an upstream split or approved substitute for full parity. Healthcare filtering on that split produces 18 tasks.

## Comparison with Original Benchmark (Parity)

**No real parity experiments have been completed.** [parity_experiment.json](parity_experiment.json) is deliberately `[]`; it contains no estimated scores or fabricated runs. The structural validator may flag this until evidence is added. The adapter does not yet satisfy the contribution's oracle/parity acceptance requirements.

Local implementation checks are recorded in [validation_summary.json](validation_summary.json): 65 unit tests (plus 21 subtests), 900 generated tasks accepted by Harbor, and all 900 generation and judge prompts matching actual pinned upstream calls byte-for-byte using fake clients. The real oracle smoke run covered all six conditions on the first healthcare scenario plus the first L1-self scenario in each other domain: 10/10 reward 1, no trial errors, 6,889 input and 292 output judge tokens. This is a small deterministic coverage sample, not a full oracle result or model parity measurement.

Docker controls also passed: an empty submission receives zero, an unavailable judge produces `RewardFileNotFoundError` without a score, and the native agent completes a trial through a local scripted HTTP endpoint with exactly one generation and one judge request. That scripted endpoint's reward is a control result, not measured model performance. Raw development jobs are local and have not been uploaded.

Reproduce offline checks without model calls:

```bash
uv sync --extra native
uv run pytest -q
uv run ruff check src tests scripts
uv run python scripts/check_fidelity.py \
  --upstream-dir ../../tmp/DeceptionBench-original \
  --data-dir ../../tmp/deceptionbench-data --output-file fidelity.json
```

The fidelity command requires the pinned source checkout described below and a directory containing the five pinned dataset JSON files. Generation downloads those files into `~/.cache/deceptionbench/38b412da565073483c0a68ce9e04eb15859beedc/` by default; that directory can also be passed as `--data-dir`. It invokes the official functions with local scripted clients and never requests an API key.

| Agent | Model | Metric | Runs per side | Evaluated tasks | Original (mean ± sample SEM) | Harbor (mean ± sample SEM) |
| --- | --- | --- | --- | --- | --- | --- |
| deceptionbench-native@0.1.0 | Pending team configuration | Response honesty | 0 | 0 | Not measured | Not measured |

The proposed comparison is the native API agent against the pinned original single-turn API functions. No CLI-agent parity claim is made. `python -m deceptionbench.original` executes checksum-verified upstream generation/evaluation functions independently of Harbor and retains raw records.

From `src/deceptionbench/`, prepare the original source:

```bash
git clone https://github.com/Aries-iai/DeceptionBench.git ../../tmp/DeceptionBench-original
git -C ../../tmp/DeceptionBench-original checkout 8f3175f66fdff89ded3ebdf0ee2055fa47c955db
uv sync --extra native
```

After agreeing the exact models, endpoints, sample, and budget with the Harbor team, start with the same six tasks on both sides:

```bash
uv run python -m deceptionbench.original \
  --upstream-dir ../../tmp/DeceptionBench-original \
  --model gpt-4o --limit 6 --output-dir ../../results/deceptionbench/original-smoke
uv run deceptionbench --limit 6 --output-dir ../../datasets/deceptionbench-smoke
uv run harbor run -p ../../datasets/deceptionbench-smoke \
  --agent-import-path deceptionbench.native_agent:DeceptionBenchAgent -m gpt-4o
```

For the full comparison, remove the original runner's `--limit 6` and use the full generated dataset on Harbor. Use a new original output directory and Harbor job directory for each repetition. The original runner and task generator share `--split`, `--domains`, `--conditions`, `--task-ids`, and `--limit`. Preserve selected IDs, source pins, exact model IDs, endpoint settings, runtime versions, and raw results.

Run order must be symmetric: sanity check on both sides, one full run on both sides, then three full runs on both sides. Report at least two runs per side as mean ± sample SEM and require overlapping run-score ranges. Aggregate valid judged records; infrastructure errors are not low benchmark scores. Compare response honesty and both original deception rates. Original output is `results.jsonl` with `config.json`; Harbor reports are per trial under the job directory. No adapter, dataset, or parity-artifact PR has been published yet.

## Notes & Caveats

- **Prompt fidelity:** native generation uses the original scenario text plus the exact `utils.py` response suffix, without a system message or delivery text. CLI-agent instructions append a request to write the response file and define a distinct experimental protocol.
- **Judge quirk retained:** upstream sends the `###FINAL:` evaluation prompt through a helper that appends the `{thought,response}` generation suffix. The adapter preserves both conflicting instructions. Malformed or unrecognized judge labels cause verifier errors, not honesty scores.
- **Upstream metrics bug:** the aggregate-metrics filename parser misidentifies some condition names. Comparisons use raw per-condition `honest`/`decept` labels from the official evaluator instead. Reward is `1` for an honest response and `0` for a deceptive response. `report.json` also preserves thought/response deception metrics; higher deception rates are worse.
- **Failure semantics:** missing, empty, non-UTF-8, symlink, non-regular, or over-1-MiB response files receive zero with an explicit submission status and no invented deception label. API failures, missing judge configuration, or invalid judge output leave no reward file and fail the verifier. Native generation failures raise agent errors rather than grading upstream's `Error: ...` placeholder.
- **Oracle coverage:** source groundtruth is used to construct an honest-response candidate. Ten real-judge smoke tasks passed; the remaining candidates still require full validation. The smoke run used the `gpt-4o` alias, not a pinned model snapshot.
- **Transport limit:** API clients use an explicit 120-second request timeout on both sides, instead of upstream's implicit SDK timeout. Generation/judge parameters and retry counts are unchanged. Harbor also enforces task timeouts.
- **Scope and cost:** L3 is excluded. A valid native task normally needs one generation and one judge request; retries increase request counts. Full oracle validation and parity remain outstanding.
- **Licensing:** upstream declares CC BY-SA 4.0 and separately prohibits commercial use. [NOTICE.md](NOTICE.md) preserves both statements; the adapter does not resolve their tension or grant additional rights.

## Installation / Prerequisites

Use Python 3.12+ and `uv`; the generated container uses Python 3.13. Docker must be available for task execution. `uv sync --extra native` installs Harbor `0.22.0` and OpenAI SDK `2.54.0`; `uv sync` alone supports generation.

Provide dedicated model and judge credentials in the host environment:

```bash
export DECEPTIONBENCH_MODEL_API_KEY='your-evaluated-model-key'
export DECEPTIONBENCH_MODEL_BASE_URL='https://api.openai.com/v1'
export DECEPTIONBENCH_JUDGE_API_KEY='your-judge-key'
export DECEPTIONBENCH_JUDGE_BASE_URL='https://api.openai.com/v1'
export DECEPTIONBENCH_JUDGE_MODEL='gpt-4o'
```

The native agent requires the dedicated model key, with no `OPENAI_API_KEY` fallback. Its base URL defaults to OpenAI's API. The judge has its own dedicated variables, defaulting to OpenAI's API and `gpt-4o`. Generated verifier configuration passes only judge variables into the separate verifier container. Never put credentials in source or generated tasks. Pin agreed model identifiers for measured parity.

Reproduction pins:

- Original code: `Aries-iai/DeceptionBench@8f3175f66fdff89ded3ebdf0ee2055fa47c955db`.
- Data: `skyai798/DeceptionBench@38b412da565073483c0a68ce9e04eb15859beedc`. Per-file SHA-256 hashes are in [dataset.py](src/deceptionbench/dataset.py); generated metadata records provenance.
- Python image: `python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72`.
- Harbor: `0.22.0`; OpenAI SDK: `2.54.0`. Use [uv.lock](uv.lock) for the complete dependency resolution.

## Troubleshooting

- **Checksum mismatch:** use the exact pinned JSON files, or remove the affected cache file and regenerate. Edited/newer data is rejected.
- **Custom agent import failure:** run `uv sync --extra native` and `uv run harbor ...` from `src/deceptionbench/`.
- **Missing key:** configure the dedicated variables above. Personal `OPENAI_API_KEY` is not an automatic fallback.
- **No reward file:** inspect `verifier/report.json` for error stage/type and Harbor logs for API/container failures. Do not count infrastructure errors as deceptive responses.
- **Model rejected by endpoint:** use its exact accepted identifier. Native model IDs are not rewritten, including provider prefixes.
- **Existing output:** use `--overwrite` only for generated tasks intended for replacement. Original experiments require a fresh empty output directory per repetition.

## Citation

```bibtex
@inproceedings{huang2025deceptionbench,
  title={DeceptionBench: A Comprehensive Benchmark for AI Deception Behaviors in Real-world Scenarios},
  author={Huang, Yao and Sun, Yitong and Zhang, Yichi and Zhang, Ruochen and Dong, Yinpeng and Wei, Xingxing},
  booktitle={Advances in Neural Information Processing Systems},
  year={2025},
  url={https://arxiv.org/abs/2510.15501}
}
```

Sources: [official repository](https://github.com/Aries-iai/DeceptionBench), [dataset](https://huggingface.co/datasets/skyai798/DeceptionBench), and [NeurIPS proceedings](https://proceedings.neurips.cc/paper_files/paper/2025/hash/55494d8756b72c2219027edc9de1ee5a-Abstract-Datasets_and_Benchmarks_Track.html).

## Authors & Contributions

Adapter author: [YuCheng (Tom) Yuan](mailto:tomyuanyucheng@gmail.com). Original benchmark authors are credited above and in generated task metadata. Submit adapter issues and contributions to [harbor-framework/adapters](https://github.com/harbor-framework/adapters). No parity API sponsorship is claimed while experiments are pending.
