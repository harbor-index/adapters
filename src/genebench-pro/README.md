# GeneBench-Pro

## Overview

This adapter converts the **ten public GeneBench-Pro case studies** from
[openai/genebench-pro-public-package](https://huggingface.co/datasets/openai/genebench-pro-public-package)
into Harbor tasks. The upstream release is pinned to
`eb75a3c0996b3cedcc9af685bad02fd166848fa2`.

Adapter implementation and oracle verification are complete. The original-side
Codex integration is linked below. Parity experiments and registry publication
are pending.

## What is GeneBench-Pro?

[GeneBench-Pro](https://www.biorxiv.org/content/10.64898/2026.06.29.735386v4)
evaluates multistage scientific analysis in genomics, quantitative biology and
translational biomedicine. Each case supplies a scientific question, local input
files, and a problem-specific JSON answer format. All files are available at the
start of a task.

This adapter includes the ten released cases and all 50 listed input files. The
50 Artificial Analysis cases and internal holdout are outside the public release
and are excluded. Results on this adapter describe the public cases only.

## Adapter Features

- Generates one task per upstream evaluation ID, in manifest release order.
- Downloads through `huggingface_hub.snapshot_download` at the pinned revision.
- Verifies the manifest, grader, task configurations and input data against
  the published SHA-256 checksums. Each config defines its permitted input files.
- Preserves the scientific prompt, input filenames, original evaluation config,
  and reference grader. No numerical grading rules are reimplemented.
- Uses the upstream `passed` boolean for binary Harbor reward. The upstream
  diagnostic `score` is retained only in the verification log.
- Keeps full configs, answers, tolerances, grader and reports out of the agent
  image. Harbor uploads verification files after the agent completes.
- Provides the released reference answers as oracle submissions.
- Accepts another commit of the same public package through `--revision`,
  discovering tasks from its manifest and recording that commit in every task.
- Checks source paths, required file checksums and unique registry names.
  Prepares replacement tasks before removing existing output.
- Runs the agent as an unprivileged user and verification as root, with a
  protected interpreter, isolated Python imports and fresh reward files.

## Generated Task Structure

```text
datasets/genebench-pro/<upstream_eval_id>/
├── LICENSE
├── instruction.md
├── task.toml
├── environment/
│   ├── Dockerfile
│   ├── LICENSE.dataset
│   ├── requirements.in
│   ├── requirements.txt
│   └── data_files/
├── solution/
│   ├── solve.sh
│   └── eval_answer.json
└── tests/
    ├── LICENSE
    ├── test.sh
    ├── verify.py
    ├── eval_config.json
    └── reference_grader.py
```

Task names follow `openai/genebench-pro-<sanitized-eval-id>` and remain stable
across generation. Names are lowercase with hyphen separators; colliding names
are rejected. Directory names preserve upstream IDs containing only letters,
digits, underscores and hyphens; other IDs use the sanitized name. The agent writes its
submission to `/workspace/eval_answer.json`.

## Run Evaluation / Harness

Registry execution will be documented after publication. Until then, generate
the local tasks and run the following **from the adapters repository root**:

```bash
# Full oracle verification, using the supplied reference configuration.
harbor run -c src/genebench-pro/run_genebench-pro.yaml

# Oracle verification of one generated task.
harbor trial start -p datasets/genebench-pro/wf_selection -a oracle
```

The reference configuration runs one trial at a time and deletes task
containers afterward. The environment supplies Python 3.12, R, a locked
scientific Python stack, bedtools and tabix. Base Python and uv images are pinned
by digest. The task requests 1 CPU, 2 GiB RAM, a 120-second verifier limit and a
3,600-second agent limit. The latter is an adapter execution limit, not a limit
specified by the original benchmark. Runtime internet access is disabled.
Harbor's native `[agent].user = "analyst"` and `[verifier].user = "root"`
settings separate analysis permissions from grading permissions. The workspace
is writable by the agent; system tools and the initially empty `/tests` directory
are owned by root.

Oracle verification with Harbor 0.5.0 on 2026-11-12: **10/10 tasks passed,
zero exceptions, mean reward 1.000**. The WIP PR includes the required terminal
screenshot. Harbor writes local run results under `jobs/genebench-pro/`.

Twelve conversion tests cover the prompts, 50 inputs, an additional task/revision,
and invalid-source/overwrite failures. Ten Docker checks cover submission errors,
numeric tolerances and schema rules. Container checks also verified grading
permissions, import isolation, fresh rewards and access to all 50 inputs.

## Usage: Create Task Directories

```bash
cd src/genebench-pro
uv sync --frozen
uv run genebench-pro --output-dir ../../datasets/genebench-pro

# Generate one case using its original evaluation ID.
uv run genebench-pro --task-ids wf_selection --output-dir ../../datasets/genebench-pro

# Regenerate the first two cases from the existing download cache.
uv run genebench-pro --offline --limit 2 --overwrite --output-dir ../../datasets/genebench-pro

# Run conversion checks after the pinned snapshot has been downloaded.
uv run python -m unittest discover -s tests -v

# Opt into a newer public-package commit without editing adapter code.
# Supply its full, lowercase 40-character Hugging Face commit SHA.
uv run genebench-pro --revision <commit-sha> --output-dir ../../datasets/genebench-pro-new
```

Required CLI flags are `--output-dir`, `--limit`, `--overwrite`, and `--task-ids`.
An existing task is skipped unless `--overwrite` is supplied. Unknown IDs and
non-positive limits fail. The optional `--cache-dir` selects the Hugging Face
cache; its default is the repository's ignored `tmp/genebench-pro/huggingface`.
`--offline` requires that cache to contain the selected snapshot's required files.
The default revision remains the verified ten-case release. `--revision` accepts
only an immutable commit SHA from the same OpenAI dataset. New tasks must retain
the public manifest/config/data-files contract and use the supplied reference
grader. A new input format or tool requirement may require an environment update;
new scientific task IDs, answer fields and grader rules require no task-specific
adapter branches. Validate a new revision before publishing its generated tasks.

## Comparison with Original Benchmark (Parity)

The [original-benchmark fork](https://github.com/nbzy1995/genebench-pro/tree/codex-integration)
provides a Codex CLI integration over the released configs and permitted inputs,
using the unchanged reference grader. Its README contains setup and reproduction
commands, following Agent Guide Scenario 2.

Parity has not been run. `parity_experiment.json` is an empty array, and metadata
records zero parity tasks and zero published registry tasks. These are pending
work, not measured scores. Agent/model selection and the experiment protocol
belong to the next step of the contribution guide.

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|---|---|---|---|---|---|---|

## Notes & Caveats

- **Prompt discrepancy:** the actual pinned `task` strings say the data came
  from a real experiment and imply reasoning quality is graded. The README
  describes different provenance wording, while the paper describes simulated
  data and the reference grader checks final answer fields. The adapter retains
  the actual released prompts and grader behavior. Reasoning content is not
  scored.
- **Output-path instruction:** the only appended instruction identifies the
  workspace, permitted input paths, available tools and required answer file.
  The original scientific prompt remains intact.
- **Published documentation checksums:** `checksums.sha256` contains stale
  hashes for the README and MIT license at this revision. All ten configs, all
  50 inputs, the manifest and grader match their published hashes. The license
  is copied unchanged from the pinned commit.
- **Oracle scope:** `solve.sh` copies the released gold answer into the output
  file. This validates conversion and grading; it does not compute a scientific
  solution from the inputs.
- **Environment:** the public release supplies no original Dockerfile or
  dependency lockfile. This adapter supplies a CPU scientific environment for
  the released tabular and NumPy inputs. It includes core Python/R tools and
  some genomics tools, rather than the complete unpublished research image.
- **Schema behavior:** only the structural-inversion case enables the strict
  schema in the released grader. That task-specific behavior is preserved.
- **Failure handling:** missing files, unreadable JSON and non-object JSON
  submissions receive reward zero and a diagnostic. Unexpected grader errors
  remain verifier failures. A failing submission returns nonzero from
  `tests/test.sh`; success returns zero.

## Installation / Prerequisites

- Python 3.11+ and uv for task generation.
- Harbor CLI; the implementation was verified with Harbor 0.5.0.
- Docker with enough free space for the scientific image and build cache.
- Network access for the initial public-package download and image build.
  Downloading the dataset requires no Hugging Face token.

The adapter is an independent uv project with its own `pyproject.toml`,
`uv.lock` and `.venv`. Runtime Python dependencies are pinned with hashes in
`task-template/environment/requirements.txt`.

## Troubleshooting

- A checksum mismatch for a task input, config or grading file stops generation;
  inspect the pinned cache before retrying.
- If `--offline` reports an incomplete snapshot, run generation once with
  network access using the same cache directory.
- Docker Desktop has its own virtual-disk capacity. Check that limit as well as
  host free space before building the scientific image.
- Inspect `jobs/genebench-pro/<job>/<trial>/verifier/result.json` for the original
  grader decision and field diagnostics. The source grader's CLI exit code
  alone does not indicate whether a submission passed.

## Citation

```bibtex
@article{li2026genebenchpro,
  title = {GeneBench-Pro: Evaluating Multistage Statistical Reasoning in Genomics, Quantitative Biology, and Translational Biomedicine},
  author = {Li, Jeremy and Ho, Andrew},
  year = {2026},
  journal = {bioRxiv},
  doi = {10.64898/2026.06.29.735386},
  url = {https://www.biorxiv.org/content/10.64898/2026.06.29.735386v4}
}
```

## Authors & Contributions

- Jeremy Li and Andrew Ho (OpenAI): original benchmark, public case studies,
  reference answers and grading contract.
- Yang Zhou: Harbor adapter and verification.

The public package is MIT licensed. Its license is retained in every generated
task and in the runtime image.
