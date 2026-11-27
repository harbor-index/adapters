# LatchBio Benchmarks (benchmarks.bio) → Harbor Adapter

## Overview

[benchmarks.bio](https://benchmarks.bio) is LatchBio's suite of agentic evaluations on real, messy biological
data. Each evaluation gives an agent a data snapshot and a natural-language analysis task, and grades a structured
JSON answer with a deterministic grader. The suite's overall score spans 13 benchmarks; every benchmark lives in its
own GitHub repository and is **mostly withheld** to prevent training contamination, with only a small public subset
released per benchmark.

This adapter converts every public evaluation that can be run and scored from its public release: **44 tasks from
5 benchmarks**.

| Benchmark | Domain | Tasks here | Full benchmark | Upstream repository (pinned commit) |
|---|---|---:|---:|---|
| scBench | single-cell RNA-seq analysis | 6 | 195 | [latchbio/scbench](https://github.com/latchbio/scbench) @ `106726f` |
| SpatialBench | spatial transcriptomics analysis | 16 | 159 | [latchbio/spatialbench](https://github.com/latchbio/spatialbench) @ `baa9841` |
| VariantBench | variant discovery and statistical genetics | 6 | 118 | [latchbio/variantbench](https://github.com/latchbio/variantbench) @ `96a2656` |
| MetagenomicsBench | metagenomic analysis | 4 | 100 | [latchbio/metagenomicsbench](https://github.com/latchbio/metagenomicsbench) @ `661be73` |
| TxBench-Preclinical-Pharmacology | small-molecule preclinical pharmacology | 12 | 100 | [latchbio/txbench-pp](https://github.com/latchbio/txbench-pp) @ `920bff7` |
| **Total** | | **44** | | |

"Full benchmark" is the size each upstream README reports at the pinned commit (scBench, SpatialBench,
VariantBench) or the benchmarks.bio leaderboard count (MetagenomicsBench, TxBench-PP).

How the 44 is derived: the 13 repositories publish 87 public eval files in total. An eval can be run and scored
only if its public file ships both the input data pointers (`data_node`) and a grader with its ground truth. That
holds for 48 evals: 46 in the five repositories above and 2 in OncologyBench. The 2 OncologyBench evals need an
LLM-judged rubric and are not part of the scored benchmark, and 2 of the 46 (VariantBench's two neoantigen evals)
reference Latch Data nodes that no longer exist, leaving 44. The other 39 public evals are excluded because upstream
withholds the data, the grader or both; see [Notes & Caveats](#notes--caveats) for the full list with reasons.

Main adaptation choices:

- **No prompt changes.** `instruction.md` is the upstream task text followed by the upstream harness's standard data
  instructions, which is the prompt upstream gives its CLI agents. For Claude Code only, upstream appends one more
  sentence; see [Notes & Caveats](#notes--caveats).
- **No grading changes.** The verifier runs LatchBio's own graders, from the
  [latch-eval-tools](https://github.com/latchbio/latch-eval-tools) 0.4.54 release installed in every task image,
  on the same answer file the upstream harness reads.
- **Same environment.** Tasks run in the agent container LatchBio publishes for these benchmarks, pinned by digest,
  with the evaluation data at the same paths.

Licensing: scBench, SpatialBench and VariantBench are Apache-2.0. MetagenomicsBench states Apache-2.0 in its README
but ships no license file, and TxBench-PP ships neither a license file nor a license statement. latch-eval-tools
(harness and graders) is published on GitHub and PyPI as "© LatchBio LLC. All rights reserved.", so this adapter
contains none of its code: tasks install the PyPI release. Evaluation data is redistributed unmodified through a
public mirror. See [Notes & Caveats](#notes--caveats) for what is redistributed.

## What is LatchBio Benchmarks?

LatchBio's benchmarks test whether AI agents can extract correct biological results from real workflows. Each
problem snapshots an analysis state just before a target step (for example an AnnData object after QC, a VCF, or a
plate-reader export) and asks for a specific quantitative or categorical result. Tasks are built so that prior
knowledge alone is not enough: the agent has to inspect and analyse the data, and many tasks contain traps where a
default tutorial workflow gives a confidently wrong answer.

- Site and leaderboards: https://benchmarks.bio
- Harness and graders: https://github.com/latchbio/latch-eval-tools
- Papers: [scBench](https://arxiv.org/abs/2602.09063); manuscripts for the other benchmarks are linked from their
  repositories and from benchmarks.bio.

**Scoring.** Every evaluation is pass/fail: the grader compares the agent's JSON answer with the ground truth
(numeric tolerances, label-set Jaccard, marker-gene precision/recall, multiple choice, or an all-of combination).
The reported metric is accuracy: the percentage of evaluations passed, averaged over repeated runs. LatchBio
describes its own runs (SpatialBench `METHODS.md`) as three per evaluation, each in a container with 4 vCPU, 32 GB
RAM, 100 GB storage and a 6 hour task timeout.

## Adapter Features

- **Pinned upstream sources.** Eval definitions are fetched from the five upstream repositories at fixed commits
  (`src/latchbio/sources.py`) with a sparse, blob-filtered checkout, so the large published trajectories are not
  downloaded. Regeneration is deterministic.
- **Upstream graders, unmodified and not copied.** Every task image installs latch-eval-tools 0.4.54 from PyPI into
  the verifier's own virtualenv (without the agent-harness dependencies), and the verifier imports only its
  `graders` subpackage. `tests/graders.lock.json` records the SHA-256 of each grader module (the same files as
  commit `0d79831`); `tests/grade.py` checks them before grading and fails closed on any difference.
  `scripts/pin_graders.py --check` and the test suite verify the lock.
- **Same answer protocol.** The verifier (`tests/grade.py`) mirrors upstream's `EvalRunner`: it reads
  `/workspace/analysis_output.json` or `/workspace/eval_answer.json` (upstream's lookup order), treats a missing,
  unparseable or `null` answer as not passed, and treats a grader exception as a fail. Reward is 1 if the
  evaluation passed, else 0.
- **Pinned, verified input data.** Every input file is pinned by size and SHA-256 in
  `src/latchbio/data_manifest.json` (98 Latch Data nodes, 37.8 GiB). Task images download their files from a public
  mirror at build time and verify them, so no Latch account is needed to run the tasks. The data can also be pulled
  straight from Latch (`--data-source latch`).
- **Oracle answers proven at generation time.** LatchBio publishes ground truth inside the grader configs rather
  than reference code. The adapter derives each oracle answer from the grader config and grades it with the same
  graders before writing the task; generation fails if an oracle answer would not pass.
- **Upstream environment.** Tasks build on `public.ecr.aws/p5z7v3z8/benchmark_agent` pinned by digest (LatchBio's
  `agent_env`: Python 3.12 with scanpy, squidpy, scvi-tools, R and common bioinformatics libraries), work in
  `/workspace`, and request the resources LatchBio documents for its runs (4 vCPU, 32 GB RAM, 100 GB storage, 6 hour
  agent timeout).
- **No custom agent.** LatchBio's harness already drives Claude Code, Codex and other CLI agents that Harbor
  supports, so the adapter adds none. `parity/run_latchbio_parity.yaml` is the complete Claude Code configuration
  that matches upstream's harness (Claude Code 2.1.174, `--effort max --thinking-display summarized`, Bash
  timeouts, the Claude-only prompt note).

## Generated Task Structure

```
latchbio/
├── scbench__bd_rhapsody_tnbc_panel_aware_qc/
│   ├── task.toml                     # Name, authors, upstream provenance, resources, timeouts
│   ├── instruction.md                # Upstream task text + upstream data instructions
│   ├── environment/
│   │   ├── Dockerfile                # LatchBio agent image (pinned digest) + data + verifier venv (graders)
│   │   ├── data_manifest.json        # Files for this eval: path, size, SHA-256, mirror URL
│   │   └── fetch_data.py             # Downloads and verifies the files at build time
│   ├── solution/
│   │   ├── solve.sh                  # Writes the reference answer to /workspace/eval_answer.json
│   │   ├── eval_answer.json          # Reference answer derived from the grader's ground truth
│   │   └── upstream_notes.md         # Upstream's notes on how the ground truth was derived (when published)
│   └── tests/
│       ├── test.sh                   # Runs grade.py, writes /logs/verifier/reward.txt
│       ├── grade.py                  # Mirrors upstream's answer loading and grading
│       ├── eval_config.json          # Upstream grader config (ground truth, tolerances) + provenance
│       └── graders.lock.json         # SHA-256 of the upstream grader modules the verifier may use
├── spatialbench__.../
├── variantbench__.../
├── metagenomicsbench__.../
└── txbench-pp__.../
```

Task directories are named `<benchmark>__<eval id>` (upstream eval ids lowercased) and registered as
`latchbio/<benchmark>__<eval id>`.

The adapter code:

```
src/latchbio/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_latchbio.yaml                 # Reference config: full dataset, oracle by default
├── parity/                           # Parity experiment: original-side runner, Harbor configs, summary script
├── scripts/                          # Maintainer tools: pin graders, pin/mirror data, evaluator replay
├── tests/                            # Unit tests (pytest)
└── src/latchbio/
    ├── adapter.py                    # LatchBioAdapter: task generation
    ├── main.py                       # CLI entry point
    ├── sources.py                    # Pinned upstream repositories, image digest, exclusions
    ├── upstream.py                   # Fetch and load eval definitions
    ├── oracle.py                     # Oracle answer synthesis, checked against the graders
    ├── graders_release.py            # Locates and verifies the pinned latch-eval-tools release
    ├── data.py                       # Data manifest, mirror and Latch download
    ├── data_manifest.json            # Pinned input files for all 44 tasks
    └── task-template/
        ├── task.toml
        ├── instruction.md
        ├── environment/{Dockerfile,fetch_data.py}
        ├── solution/solve.sh
        └── tests/{test.sh,grade.py,graders.lock.json}
```

## Run Evaluation / Harness

### Running with Datasets Registry

After the dataset is published to the Harbor registry:

```bash
# Oracle (reference answers)
harbor run -d latchbio/latchbio

# A specific agent and model
harbor run -d latchbio/latchbio -a claude-code -m "anthropic/claude-haiku-5-5"
```

Until then, generate the tasks locally (see [Usage](#usage-create-task-directories)) and use the path-based
commands below.

### Using Job Configurations

```bash
# From the repository root. Oracle on the full dataset:
harbor run -c src/latchbio/run_latchbio.yaml

# Any agent on a locally generated dataset:
harbor run -p datasets/latchbio -a claude-code -m "anthropic/claude-haiku-5-5"

# Claude Code configured as in the parity experiment:
harbor run -c src/latchbio/parity/run_latchbio_parity.yaml

# Resume an interrupted job:
harbor job resume -p jobs/<job-name>
```

`run_latchbio.yaml` keeps the oracle as the default agent and lists Claude Code and Codex blocks configured like
LatchBio's own harness. Tasks request 4 vCPU / 32 GB RAM / 100 GB storage; on smaller hosts add
`--override-memory-mb` / `--override-storage-mb` (or the `environment.override_*` keys in the config).

### Running Individual Trial

```bash
harbor trial start -p datasets/latchbio/scbench__bd_rhapsody_tnbc_panel_aware_qc
harbor trial start -p datasets/latchbio/scbench__bd_rhapsody_tnbc_panel_aware_qc \
    -a claude-code -m "anthropic/claude-haiku-5-5"
```

## Usage: Create Task Directories

```bash
cd src/latchbio
uv sync

# All 44 tasks (input data is downloaded from the mirror when each image is built)
uv run latchbio --output-dir ../../datasets/latchbio

# Only the 38 tasks whose input data totals at most 2 GiB (for hosts without 32 GB per container)
uv run latchbio --split small-data --output-dir ../../datasets/latchbio-small-data

# Selected tasks: Harbor task ids, registry names or upstream eval ids all work
uv run latchbio --output-dir ../../datasets/latchbio \
    --task-ids scbench__de01_pseudobulk_de bd_rhapsody_tnbc_panel_aware_qc
```

Flags:

- `--output-dir`: where task directories are written (default `datasets/latchbio` at the repository root)
- `--limit N`: generate only the first N tasks
- `--overwrite`: replace task directories that already exist (existing ones are skipped otherwise)
- `--task-ids ...`: only these tasks
- `--benchmarks ...`: only these benchmarks (`scbench`, `spatialbench`, `variantbench`, `metagenomicsbench`,
  `txbench-pp`)
- `--split {full,small-data}`: `small-data` selects the 38 tasks whose input data totals at most 2 GiB
- `--data-source {mirror,latch,none}`: `mirror` (default) downloads pinned files at image build time; `latch`
  downloads them now with the Latch SDK and copies them into each task (`uv sync --extra latch`, Latch credentials
  required); `none` stages no data and is only for exercising the oracle and verifier
- `--data-dir`: cache for `--data-source latch` (default `$LATCHBIO_DATA_DIR`, else `~/.cache/harbor-latchbio/data`)
- `--cache-dir`: where the upstream repositories are fetched (default `~/.cache/harbor-latchbio/upstream`)
- `--upstream-root`: use existing clones of the upstream repositories instead of fetching (must be at the pinned
  commits)
- `--list`: print the tasks that would be generated

## Comparison with Original Benchmark (Parity)

Parity compares LatchBio's own harness (latch-eval-tools 0.4.54, `EvalRunner` + `run_claudecode_task`) with the
Harbor tasks, using the same agent, model, container image, prompt and limits on both sides.

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|
| claude-code@2.1.174 | claude-haiku-5-5 | accuracy (%) | 3 | 44 (100%) | 40.91 ± 2.27 | 38.64 ± 1.31 |

Scores are mean ± sample SEM over runs; a run's score is the percentage of the 44 evaluations passed. Run scores are
43.18, 36.36, 43.18 (original) and 36.36, 38.64, 40.91 (Harbor). The ranges overlap, so the matching criterion is
met. A second run of the whole comparison on a small machine agrees; see
[Reduced-resource replication](#reduced-resource-replication).

**Per-task agreement.** 33 of the 44 tasks have the same number of passes on both sides over the three runs (12
pass in every run on both sides, 20 in none). Nine tasks differ by one pass and two by two. Counting the replication
as well (246 trials per side, every trial scored as first run), the original harness passes 106 trials (43.1%) and
Harbor 104 (42.3%). The two tasks that differ most:

- `txbench-pp__ctrl01_no_cc1_gate_for_crizotinib_hits`: 0 of 6 on the original side, 4 of 6 in Harbor. The four
  passes are the four Harbor trials in which the agent decided to look up the source publication with web search;
  in every other trial on either side it answered from the data alone and gave the same wrong answer. Claude
  Code's web tools are available on both sides (the tool lists in the transcripts are identical) and are rarely
  used: 4 of 246 Harbor trials, 1 of 246 original trials. Prompt and data for this task are identical on both
  sides.
- `spatialbench__motif04_neuronal_female_tf_family`: 3 of 6 on the original side, 0 of 6 in Harbor. The answer is a
  count that depends on the analysis route the agent picks. Both sides produced the wrong counts 13 and 123; the
  counts within tolerance (8 and 11) came up three times, all on the original side.

With 44 tasks compared, differences of this size on one or two tasks are within what chance produces (two-sided
Fisher exact p = 0.06 and 0.18 for these tasks alone, before correcting for 44 comparisons). They were not followed
up with isolated repeats.

**Setup on both sides.**

| | Original (latch-eval-tools) | Harbor |
|---|---|---|
| Eval definitions | upstream repositories at the pinned commits | generated from the same commits |
| Agent | Claude Code 2.1.174, preinstalled in the agent image | same binary (`version: "2.1.174"`) |
| CLI flags | `--effort max --thinking-display summarized`, permissions bypassed, prompt on stdin | same (`reasoning_effort`, `thinking_display`, `bypassPermissions`) |
| Model | `claude-haiku-5-5` | `anthropic/claude-haiku-5-5` |
| Prompt | task + data instructions + Claude Code background-process note | `instruction.md` + `parity/claude_code_background_note.md` (byte-identical result) |
| Image | `benchmark_agent@sha256:5527…baf7c` | same image as the Dockerfile base |
| Data | downloaded from Latch, mounted at `/workspace/data` | same files (SHA-256 verified) from the mirror, at `/workspace/data` |
| Limits | 4 CPUs, 32 GiB memory (swap allowance of the same size), 21600 s, `BASH_*_TIMEOUT_MS=21600000` | same (task defaults, `override_timeout_sec`, agent env) |
| Invocations | one per evaluation | one per evaluation |
| Grading | latch-eval-tools 0.4.54 graders | the same grader modules (hash-checked) |

The original side is the published latch-eval-tools package, driven by `parity/run_original.py`. To make the two
sides equal, that script changes the harness's defaults where they differ from Harbor's Docker environment or from
the task settings, without touching the prompt, the agent command or the graders:

- memory limit set to 32 GiB (the harness would use the host's memory minus 2 GiB) and a swap allowance equal to
  memory (the harness allows none; Harbor's Docker environment sets no swap limit, which Docker treats this way);
- a 4-CPU limit (the harness sets none);
- timeout 21600 s (the harness default is 6000 s);
- restart-after-OOM and resume-after-provider-error switched off, because Harbor invokes the agent once;
- the Claude subscription token forwarded into the container, with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`;
- the two evals with a `graders` list graded as described under
  [Notes & Caveats](#notes--caveats).

Both sides ran on one Google Compute Engine `n2-highmem-8` VM (8 vCPU, 64 GB, x86-64, Ubuntu 24.04, Docker 29.10)
with Harbor 0.27.0, on 2026-11-23 and 2026-11-24 (UTC). The two sides ran at the same time, one container each; the
original side went through the tasks in reverse order so that the memory-heavy tasks did not coincide. Claude Code
authenticated with a Claude subscription token (`CLAUDE_CODE_OAUTH_TOKEN`) on both sides.

**How results are counted.** An evaluation without a gradable answer counts as not passed on both sides, as in
upstream's batch runner. A trial that timed out is scored on what the agent had written. A trial that failed before
or outside the agent's own work (a harness or environment error, or an API error reported by Claude Code) is not
scored and is repeated, on whichever side it happens. `parity/summarize.py` applies this one rule to both sides and
also lists any trial that ended without a final result from Claude Code.

No trial on either side timed out, ended without a final result or reported an API error. One infrastructure
failure occurred: during run 3 the Latch API became unavailable (2026-11-23, from about 20:36 UTC) and 39
original-side evaluations failed while the harness was staging data, before the agent started. They were not scored
and were repeated the same day. The data was already in the harness's cache; the harness only asks Latch for each
file's name, so `parity/run_original.py` now falls back to the name pinned in `data_manifest.json` when Latch
cannot be reached (results that used the fallback carry `latch_name_from_manifest`). Nothing was repeated on the
Harbor side. Container memory was sampled about every 12 seconds; the two sides together peaked at 35 GiB of the
host's 62 GiB.

**Graders used in the runs.** During the parity runs the Harbor tasks carried the grader modules inside `tests/`.
The tasks now install the same modules from PyPI instead (same SHA-256, checked before grading). Regrading every
stored answer of both experiments with the current verifier reproduces all 474 recorded verdicts, and the oracle
was run again on all 44 tasks built from the current Dockerfile.

**Effort on the two sides** (three runs, as reported by Claude Code):

| | Original | Harbor |
|---|---:|---:|
| Agent time | 26.0 h | 22.5 h |
| Median time per trial | 398 s | 370 s |
| Agent turns | 2,945 | 2,883 |
| Timeouts | 0 | 0 |
| API list-price cost | $281 | $270 |

Cost is Claude Code's `total_cost_usd`; the runs themselves were billed to a subscription.

### Reduced-resource replication

The same comparison was also run on an Apple Silicon workstation (Docker Desktop, linux/amd64 emulation, 7.75 GiB
Docker VM) on 2026-11-22 and 2026-11-23 (UTC). It is supporting evidence, not the parity result, and it is not in
`parity_experiment.json`. Everything is as above except:

- **Tasks:** `--split small-data`, the 38 tasks whose input data totals at most 2 GiB. The six tasks left out have
  3 to 9 GiB of input, which cannot be analysed in 6 GiB. The rule depends only on the pinned data manifest.
- **Limits:** 6 GiB memory and 1800 s per task (`BASH_*_TIMEOUT_MS=1800000`), on both sides
  (`parity/run_latchbio_parity_reduced.yaml`).

| Scoring | Original (mean ± SEM) | Original runs | Harbor (mean ± SEM) | Harbor runs |
|---|---|---|---|---|
| Every trial as first run | 45.61 ± 2.32 | 50.00, 44.74, 42.11 | 46.49 ± 2.32 | 47.37, 50.00, 42.11 |
| With 15 trials repeated alone | 46.49 ± 1.75 | 50.00, 44.74, 44.74 | 45.61 ± 3.16 | 47.37, 50.00, 39.47 |

Means are computed from pass counts, so they can differ by 0.01 from the mean of the rounded run scores. The ranges
overlap under both scorings. The second row exists because two 6 GiB containers shared a 7.75 GiB Docker
VM: 14 trials (7 per side) ran while all containers together had reached 6.5 GiB, so the other side's container may
have constrained them, and one original-side trial ended without a result when Claude Code was killed at the
container's own memory limit. All 15 were repeated alone. Thirteen repeats gave the same outcome. The two that
changed are `spatialbench__curio_ovary_oocyte_count_per_timepoint` in Harbor run 3 (pass became fail) and
`txbench-pp__sp_01_plate_well_position_confounder` in original run 3 (the memory kill, which became a pass; counting
it as an agent failure gives the first row). Both the first runs and the repeats are published with the results.

Scores are higher than in the 44-task experiment because the six tasks left out are hard ones (two passes in 36
trials there); on the same 38 tasks the 44-task experiment gives 46.5% (original) and 43.9% (Harbor). For the
trials of the second row: 8 timeouts (original) and 9 (Harbor); 2,360 and 2,229 agent turns; API list-price cost
$219 and $212, excluding timed-out trials, which report none.

**Supporting evidence (not parity).**

- *Oracle.* The reference answer of every task scores 1.0 in Harbor: 44/44 in one job on the generated dataset with
  the real mirrored data.
- *No-op control.* An agent that writes nothing scores 0 (tried on six tasks).
- *Evaluator replay.* scBench and SpatialBench publish the answers real agents submitted together with the verdicts
  LatchBio's harness recorded. Regrading all 1,041 published answers (22 evals) with the Harbor verifier reproduces
  the recorded verdict in every case (`scripts/replay_upstream_results.py`). This covers `numeric_tolerance`
  grading only; upstream publishes no recorded verdicts for the other grader types.

**Reproduce.**

```bash
# Original side (needs Docker, Latch credentials, and ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN).
# On arm64 hosts, pull the image once with:
#   docker pull --platform linux/amd64 public.ecr.aws/p5z7v3z8/benchmark_agent@sha256:552728033c6c32d99094e848d8fb831cddc52c17ab6b10badaebe0aa301baf7c
cd src/latchbio
uv run --extra parity python parity/run_original.py \
    --model claude-haiku-5-5 --run-name run1 \
    --output-dir ../../parity-experiments/latchbio/original_parity

# Harbor side (from the repository root; with a subscription token also set CLAUDE_FORCE_OAUTH=1)
(cd src/latchbio && uv run latchbio --output-dir ../../datasets/latchbio)
harbor run -c src/latchbio/parity/run_latchbio_parity.yaml --job-name latchbio-parity-harbor-run1

# Repeat both with run2, run3, then:
python src/latchbio/parity/summarize.py --adapted-size 44 \
    --original parity-experiments/latchbio/original_parity/run{1,2,3} \
    --harbor jobs/latchbio-parity-harbor-run{1,2,3}
```

The script prints per-run accuracy, mean ± sample SEM, the range-overlap check and a per-task comparison. The
defaults of `run_original.py` are the limits above (`--split full --eval-timeout 21600 --memory-gb 32 --cpus 4`).
For the reduced-resource replication, add `--split small-data --eval-timeout 1800 --memory-gb 6` on the original
side, generate the tasks with `--split small-data --output-dir ../../datasets/latchbio-small-data`, and use
`parity/run_latchbio_parity_reduced.yaml`.

**Links.**

- Original benchmark: https://benchmarks.bio, repositories listed in the [Overview](#overview); harness
  https://github.com/latchbio/latch-eval-tools (no fork was needed: the original side is driven by
  `parity/run_original.py` against the published package)
- Adapter PR: PARITY_ADAPTER_PR
- Dataset PR: PARITY_DATASET_PR
- Parity results (Hugging Face): PARITY_HF_PR

## Notes & Caveats

**Scope and exclusions.** Only public evaluations that ship input data and a grader are adapted.

| Excluded | Public evals | Reason |
|---|---:|---|
| SpatialBench-Long | 4 | Public files have prompts and data pointers but no grader or ground truth |
| scBench-Long | 4 | Same: no grader or ground truth published |
| EpiBench | 7 | Same: no grader or ground truth published |
| TxBench-Antibody-Discovery | 6 | Prompt only: input data, grader and ground truth are withheld |
| TxBench-Oligo-Discovery | 4 | Prompt only: input data, grader and ground truth are withheld |
| BioSecBench-Surveillance | 7 | Prompt only: input data, grader and ground truth are withheld |
| BioSecBench-Function | 7 | Prompt only: input data, grader and ground truth are withheld |
| OncologyBench | 2 | Not part of the scored benchmark per upstream; each needs an LLM-judged rubric over free text with no reference answer, so it cannot be graded deterministically |
| VariantBench `eval_stage4_annotated_vcf`, `osteosarcoma_wes_stage3_called_vcf_neoantigen_gene_recall` | 2 | Every `data_node` returns "no such Latch file or directory" (checked 2026-11-22); upstream also states these two are not part of the main 118-eval benchmark |

All non-public evaluations of every benchmark (the large majority) are unavailable by design. Scores on these 44
public tasks are therefore **not comparable to benchmarks.bio leaderboard numbers**, which are computed on the full
withheld sets.

**Oracle solutions.** Upstream publishes no reference analysis code. The oracle answer of each task reproduces the
ground truth embedded in its grader config (`solution/eval_answer.json`); `solution/upstream_notes.md` carries
upstream's own notes on how that ground truth was derived where upstream publishes them. The oracle therefore
validates the task wiring and the verifier, not that the analysis is reproducible from the data.

**Evals with several graders (an adapter-defined rule).** Two VariantBench evals (`cis_mr_bcaa`,
`fine_mapping_effector_gene_prioritization`) define a top-level `graders` list instead of a single `grader`.
Upstream's public `EvalRunner` reads only `grader`, so with the public harness these two evals are never graded and
always count as not passed. The graders package does ship a helper for such lists,
`grade_multiple_graders_single_answer`, but nothing in the public harness calls it and upstream does not document
how its hosted benchmark combines the results. The adapter runs that helper and passes the evaluation only when
every grader passes. The original-side parity runner applies the same rule, so both sides agree; both tasks scored
0 in every parity trial on both sides, so only the oracle and the unit tests have exercised a pass under this rule.

**Data mirror.** Input data is copied unmodified from the Latch Data nodes named in the public eval files to
https://huggingface.co/datasets/yxjia/latchbio-benchmarks-data, laid out as `<node id>/<path>`. The mirror holds
input files only (no prompts, graders or answers) and carries a do-not-train notice. Every file is verified against
`data_manifest.json` at image build time. `--data-source latch` bypasses the mirror. The mirror is in the adapter
author's Hugging Face account and can be moved to an organisation account by changing `mirror.base_url` in
`data_manifest.json`.

**What is redistributed, and under which terms.** Generated tasks contain the upstream prompts and grader configs
(Apache-2.0 for scBench, SpatialBench and VariantBench; no license file for MetagenomicsBench and TxBench-PP), and
the mirror holds the input data those repositories point to. From latch-eval-tools ("All rights reserved"), the
generated instructions contain the data-instructions paragraph the harness appends to every prompt, and
`parity/claude_code_background_note.md` is the one sentence it appends for Claude Code; both are needed verbatim for
prompt fidelity. No latch-eval-tools code is in this repository or in the generated tasks.

**Environment differences from the upstream harness.**

- Upstream bind-mounts the data read-only at `/workspace/data`; here it is part of the image and writable.
- Upstream's `benchmark_agent:latest` tag is pinned to the digest it resolved to on 2026-11-21.
- The base image is linux/amd64 only. On arm64 hosts it runs under emulation, which is slower.
- A small extra virtualenv (`/opt/latchbio-verifier`: latch-eval-tools, pydantic, anthropic) is added for the
  verifier. The image build accepts the latch-eval-tools wheel only with its recorded SHA-256, and the verifier
  checks the grader modules against `graders.lock.json` before importing them, so no other release can be used.
- **Memory.** The public harness gives the container the host's memory minus 2 GiB, with no swap: 30 GiB on the
  32 GB machines LatchBio describes (its published results record `memory_limit_bytes` = 30 GiB). Tasks here
  request 32 GB, and Harbor's Docker environment sets no swap limit, which Docker treats as swap equal to memory.
- **CPU.** The public harness sets no CPU limit on the container; tasks here request 4 CPUs, the machine size
  LatchBio describes.
- **Timeout.** The public harness defaults to 6000 s per evaluation; tasks here use the 21600 s LatchBio documents
  for its benchmark runs.
- **Claude Code prompt note.** For Claude Code only, the upstream harness appends one sentence about background
  processes to the prompt. It is not part of `instruction.md`, which every agent receives; the Claude Code configs
  under `parity/` add it with `extra_instruction_paths`. They also set `thinking_display` and the Bash timeouts as
  upstream does. `harbor run -a claude-code` without such a config runs Claude Code with its defaults instead.
- The upstream harness restarts an agent after an out-of-memory kill and resumes it after a provider error (and,
  in LatchBio's hosted benchmark mode only, re-prompts Claude Code when it ends its turn without an answer file).
  Harbor runs the agent once.

**Limits of the verification.**

- The container has network access, as upstream's does, and the ground truth of these public evaluations is on
  GitHub in the upstream repositories. Nothing prevents an agent from looking it up; upstream accepts the same
  exposure for its public subsets.
- The verifier runs in the agent's container, as Harbor verifiers do by default. The hash check and the isolated
  interpreter (`python -I`, separate bytecode cache) make sure the pinned graders are used, but an agent with root
  in that container could still alter the verifier's interpreter or its other packages.
- Task images install latch-eval-tools 0.4.54 from PyPI. If that release were removed from PyPI, the images would
  no longer build.
- The oracle writes the ground truth; it does not show that a task is solvable from the data. Across all parity
  trials, 18 of the 44 tasks were never passed by Claude Code with claude-haiku-5-5 on either side.

**Resources.** Six tasks have large inputs: single AnnData files of 3 to 9 GiB
(`spatialbench__spatial10_genome_wide_de_pct`, `spatialbench__spatial07_sex_housekeeping_de`,
`spatialbench__dr01_batch_confounded_clustering`, `spatialbench__norm01_batch_correction`) and about 7 GiB of
genomics files (`variantbench__cis_mr_bcaa`, `variantbench__small_variant_calling_mnv_codon_consequence`). Run them
with the requested 32 GB RAM. `--split small-data` generates the other 38 tasks for hosts that cannot provide that.

**Prompt wording.** The upstream data instructions mention a completion signal
(`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`) used by upstream's mini-swe-agent harness. It is kept verbatim for
prompt parity and is harmless in Harbor: only `/workspace/eval_answer.json` is read.

**Benchmark canary.** Upstream eval files carry LatchBio's contamination canary string. The adapter repository
contains no eval files (they are fetched at generation time); generated tasks keep the canary in
`tests/eval_config.json`. `solution/eval_answer.json` holds only the answer values and has no canary field.

## Installation / Prerequisites

- Docker (able to run linux/amd64 images) or a cloud environment supported by Harbor
- Harbor CLI: `uv tool install harbor` (developed and tested with Harbor 0.27.0)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- `git` (the adapter fetches the upstream repositories)

```bash
cd src/latchbio
uv sync                     # adapter dependencies
uv run pytest               # optional: unit tests
```

Only for regenerating data from Latch (`--data-source latch`, `scripts/build_data_manifest.py`) or for the
original side of the parity experiment: a Latch account. `uv sync --extra latch` (or `--extra parity` for
`parity/run_original.py`), then `uv run latch login`, or create a token at
https://console.latch.bio/settings/developer and save it to `~/.latch/token`.

Agents need their usual credentials, for example `ANTHROPIC_API_KEY` for Claude Code.

## Troubleshooting

- **`no matching manifest for linux/arm64`** or very slow runs on Apple Silicon: the base image is amd64 only. The
  Dockerfile pins `--platform=linux/amd64`; enable Rosetta emulation in Docker Desktop for acceptable speed.
- **Image build is slow or times out.** The base image is about 4 GB compressed and six tasks download 3 to 9 GiB
  of data. `build_timeout_sec` is 3600; raise it with `--environment-build-timeout-multiplier` on slow links.
- **`checksum mismatch` during the build.** A mirrored file differs from the pinned manifest. Retry the build; if it
  persists, regenerate with `--data-source latch` and report it.
- **The agent is killed or tools report `Killed`.** The container ran out of memory. Give the task the requested
  32 GB, or expect the large-data tasks listed above to fail.
- **`--data-source latch` fails with an authentication error.** Run `uv run latch login`, or save an API token to
  `~/.latch/token`.
- **`... is at <sha>, but the adapter pins ...`** with `--upstream-root`: check out the pinned commit in that clone
  or drop the flag.
- **Reward 0 with "no answer file found".** The agent did not write `/workspace/eval_answer.json`. See
  `verifier/grading.json` in the trial directory for the grader's reasoning.
- **Reward 0 with "is not the pinned release" in `verifier/test-stdout.txt`.** The grader modules in the image
  differ from `tests/graders.lock.json`: the image and the task files come from different adapter versions, or
  something modified `/opt/latchbio-verifier` during the run. Regenerate the task and rebuild the image.
- **Image build fails with a hash mismatch for latch-eval-tools.** The package index served a different file than
  the one pinned in `src/latchbio/sources.py`. Do not bypass it; check the index and report it.

## Citation

```bibtex
@article{scbench2026,
  title   = {scBench: Evaluating AI Agents on Single-Cell RNA-seq Analysis},
  author  = {Workman, Kenny and Yang, Zhen and Muralidharan, Harihara and Abdulali, Aidan and Le, Hannah},
  year    = {2026},
  journal = {arXiv preprint arXiv:2602.09063},
  url     = {https://arxiv.org/abs/2602.09063}
}

@article{spatialbench2025,
  title  = {SpatialBench: Can Agents Analyze Real-World Spatial Biology Data?},
  author = {Workman, Kenny and Yang, Zhen and Muralidharan, Harihara and Le, Hannah},
  year   = {2025},
  url    = {https://github.com/latchbio/spatialbench}
}

@article{variantbench2026,
  title  = {VariantBench: An Agentic Benchmark for Genetic Variant Discovery and Interpretation},
  author = {Bhowmick, Aashka and Rahmatpour, Nasim and Sharma, Shivang and Xu, Qian and Gupta, Ananya and Lagwankar, Asmita and Banerjee, Arjun and Zou, Christopher},
  year   = {2026},
  url    = {https://github.com/latchbio/variantbench}
}

@misc{latchbio_benchmarks_bio,
  title        = {benchmarks.bio: Biological AI Agent Benchmarks},
  author       = {{LatchBio}},
  year         = {2026},
  howpublished = {\url{https://benchmarks.bio}}
}
```

MetagenomicsBench (https://github.com/latchbio/metagenomicsbench) and TxBench-Preclinical-Pharmacology
(https://github.com/latchbio/txbench-pp) do not publish a citation yet; cite their repositories and benchmarks.bio.

## Authors & Contributions

This adapter is developed and maintained by [Yingxin Jia](mailto:37559580+yingxin-jia@users.noreply.github.com).

**Issues and Contributions:**
- Submit issues and pull requests to the [adapters repository](https://github.com/harbor-index/adapters)
- Benchmark content questions belong upstream, in the LatchBio repositories listed above
