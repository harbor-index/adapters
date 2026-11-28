# Active-SWE → Harbor

## Overview

This adapter generates all 400 tasks in the Active-SWE main split for the Recorded track. The split contains 300 simple and 100 hard tasks across ten programming languages. Task IDs include the upstream set because three instance IDs occur in both sets. The parquet revision is `cf042f048bfe717277f598bf2cd7e8038a48ad39`. [Dataset](https://huggingface.co/datasets/XLearning-SCU/Active-SWE/tree/cf042f048bfe717277f598bf2cd7e8038a48ad39)

## What is Active-SWE?

An agent gets a repository and a list of files pending review. It has to discover the recorded bug and produce a patch without an issue description. The Recorded evaluator awards 1 when every designated FAIL_TO_PASS and PASS_TO_PASS test is recognized as passing after applying the patch. Potential and Judge are outside this adapter's scope. [Paper](https://arxiv.org/abs/2608.04682) · [Upstream Recorded evaluator](https://github.com/XLearning-SCU/Active-SWE/blob/25ecfa5ef43b4c14721935adfbdb220bb10fb9ea/active_swe/run_recorded.py)

## Adapter Features

Each task uses the upstream Recorded prompt, amd64 instance image, gold patch, evaluation script and test lists. During the build, the adapter replaces Git history with a clean base commit and installs a separate Python interpreter for the verifier. The agent has 90 minutes. A fresh verifier with a one-hour timeout receives the submitted patch, restores the base, applies the patch and scores the test log. The runtime requests four CPUs and 8 GiB of memory.

## Generated Task Structure

| Path | Purpose |
|---|---|
| `instruction.md` | Recorded prompt with the task's review files |
| `task.toml` | Metadata, patch artifact, limits and verifier isolation |
| `environment/Dockerfile` | Per-task build from the upstream image |
| `solution/{solve.sh,gold.patch}` | Oracle submission |
| `tests/` | Evaluation script, pinned grading code and expected test IDs |

## Run Evaluation / Harness

Install Harbor 0.27.0 and generate the local tasks first. From the adapters repository root:

```bash
harbor trial start -p datasets/active-swe/simple-pylint-dev__pylint-5417 -a oracle
harbor trial start -p datasets/active-swe/simple-pylint-dev__pylint-5417 -a nop
harbor run -c src/active-swe/run_active-swe.yaml
```

The dataset PR and registry entry follow parity. For Firebase on Docker hosts with IPv6 loopback disabled, see the task-specific reproduction command below.

## Usage: Create Task Directories

```bash
cd src/active-swe
uv sync --locked
uv run active-swe --output-dir ../../datasets/active-swe
uv run active-swe --split parity --output-dir ../../datasets/active-swe-parity
```

The generator also accepts `--data-file` for a local copy of the pinned parquet, `--task-ids`, `--limit` and `--overwrite`.

## Comparison with Original Benchmark (Parity)

**Proposed Scenario 1, pending maintainer confirmation.** Run the same pinned Claude Code version and dated Claude Haiku 4.5 model snapshot on the same 12 tasks on both sides. The manifest uses seed 42, covers all ten languages, and includes nine simple and three hard tasks. Reproduce it with `uv run python generate_parity_subset.py --size 12 --seed 42`. [Task manifest](src/active_swe/parity_task_ids.txt)

Match prompt, project state, dependency fixes, agent settings, 300-turn cap, 90-minute budget and model access. Use the upstream Recorded generation and evaluation on one side and Harbor on the other. First check five tasks on both sides, then complete three runs per side (72 scored sessions). Count pilot trials toward the first run only if the final configuration matches. Report per-run strict resolved percentages, mean ± sample SEM and run ranges. Endpoint, access, immutable versions and compute budget need confirmation before running.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Performance | Harbor Performance |
|---|---|---|---|---|---|---|
| Claude Code (proposed) | Claude Haiku 4.5 (proposed) | Strict resolved rate | 3 per side (planned) | 12 | Pending | Pending |

Prepare the shared task list from the adapter directory:

```bash
uv run python generate_parity_subset.py --size 12 --seed 42
uv run active-swe --split parity --output-dir ../../datasets/active-swe-parity
```

## Notes & Caveats

**Oracle evidence, Harbor 0.27.0.** The complete first pass yielded 371/400. Each of the 29 misses also scored zero when the upstream evaluator independently ran the gold patch on the original image. After targeted reruns, 397/400 passed cumulatively with the original test identifiers. The pylint NOP control returned zero and its gold patch returned one. [All 400 results](oracle/results.tsv)

| Change tested on the failed tasks | Number | Patched environment, Harbor / upstream |
|---|---:|---|
| Preload locked Rust dependencies before offline testing | 14 | 14/14 on each |
| Restore the testbed Python environment from the instance image | 11 | 11/11 on each |
| Enable IPv6 loopback inside the isolated Firebase verifier namespace | 1 | 1/1 on each |
| Proposed Django test-identifier corrections, scored separately | 3 | 3/3 on each with the proposed identifiers |

The generator includes the 25 Dockerfile repairs. The Firebase rerun used a task-specific Docker override for `simple-firebase__firebase-tools__4409`. On Linux with Docker at `/usr/bin/docker`, run the following from the adapters repository root. The wrapper enables IPv6 on the loopback interface in the Harbor egress sidecar. When reproducing upstream, use the same PATH for its isolated `docker run --network none` evaluator.

```bash
PATH="$PWD/src/active-swe/oracle/firebase-cli:$PATH" harbor trial start \
  -p datasets/active-swe/simple-firebase__firebase-tools__4409 -a oracle
```

**Django decision requested.** For `django-14372`, `django-15315` and `django-18001` (all simple), the upstream expected lists contain 9, 10 and 12 test descriptions respectively. The log pairs each description with a test method, while the upstream parser stores that method name. With the original labels, each scorer returns zero. In separate trials we replaced those identifiers in the expected lists on both sides and retained the tests, patch and pass criteria. All three then scored one on both sides. The generator keeps the original labels pending review. [Exact proposed mappings](oracle/proposed-django-labels.json)

To reproduce an upstream comparison, check out the pinned evaluator linked above. Write the pinned parquet row to JSONL, use the row's gold patch as the submitted patch, and run `python -m active_swe.run_recorded --eval --input row.jsonl --eval-input row.jsonl --output result.jsonl --concurrency 1`. For corrected-image comparisons, build the generated Dockerfile and set the JSONL row's `image` to that local tag for the run. Read `strict_resolved` from the result. For the Django diagnostic, replace the indicated entries in both the upstream JSONL test lists and the Harbor task's `tests/config.json` before repeating the comparison.

## Installation / Prerequisites

Use Python 3.12+, uv, Linux/amd64 Docker and Harbor 0.27.0. Image and interpreter downloads need network access at build time. The verifier runs offline. The original instance images and build cache can be large.

## Troubleshooting

A zero can indicate that a named test failed, was never executed, or was absent from the parsed log. Check `verifier/after.log`, `verifier/report.json` and the corresponding upstream run before diagnosing the gold patch.

## Citation

Li et al. (2026), *Active-SWE: Benchmarking Coding Agents for Proactive Bug Fixing without Issue Reports*. [arXiv](https://arxiv.org/abs/2608.04682)

```bibtex
@misc{li2026activeswe,
  title = {Active-SWE: Benchmarking Coding Agents for Proactive Bug Fixing without Issue Reports},
  author = {Haobin Li and Ping Deng and Weizhong Qian and Liang Jiang and Zhenyu Huang and Mouxing Yang and Xi Peng},
  year = {2026},
  eprint = {2608.04682},
  archivePrefix = {arXiv}
}
```

## Authors & Contributions

Active-SWE is maintained by its upstream authors. Harbor adapter by [HarperLi680](https://github.com/HarperLi680). The vendored scoring modules include the upstream license.
