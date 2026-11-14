# olmOCR-Bench → Harbor Adapter

## Overview

olmOCR-Bench measures how well a system converts PDF pages into clean, linearized Markdown / plain text. Instead of comparing against a reference transcription, every page comes with machine-checkable pass/fail "unit tests" about the output (a sentence is present, a header is absent, two passages appear in reading order, a table cell has the right neighbours, a formula renders identically).

- **Task type**: convert one single-page PDF to Markdown (OCR / document parsing)
- **Domains**: 7 document categories: arXiv math papers, old scanned math textbooks, tables, old scans (letters, typewritten documents), pages with headers/footers, multi-column layouts, long pages of tiny text
- **Dataset size**: 1,403 tasks, one per PDF, covering the whole benchmark. No PDFs or tests are excluded
- **Tests**: 7,010 unit tests in the dataset files (the number quoted by the paper and the dataset card), plus the per-PDF baseline test that the original harness adds, for 8,413 graded tests in total (see the table below)
- **Provenance**: [paper](https://arxiv.org/abs/2502.18443) | [dataset](https://huggingface.co/datasets/allenai/olmOCR-bench) (pinned to revision `54a96a6fb6a2bd3b297e59869491db4d3625b711`) | [original harness](https://github.com/allenai/olmocr/tree/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench) (pinned to commit `f7cfe4c22098b154c76b6ec950d1c0a464eecf8d`)
- **License**: dataset ODC-BY 1.0 (its card adds that it is intended for research and educational use in accordance with AI2's [Responsible Use Guidelines](https://allenai.org/responsible-use)); harness code Apache-2.0
- **Main modifications for Harbor**:
  - The original harness scores a folder of Markdown files produced by an OCR system. Here an agent gets the PDF at `/workspace/document.pdf` and writes `/workspace/output.md`; the instruction quotes the original API-model prompt and adds only what is needed to apply it to files: where the PDF is and which installed tools can read it, where to write the result (the text only, without explanations, code fences or a JSON wrapper), that "output null" means an empty file, and that there is no internet access.
  - Each task's reward is the fraction of that PDF's tests that pass. The original headline score aggregates differently (mean of per-category pass rates); `olmocr-bench-score` recomputes it from a Harbor job.
  - Grading runs the original test classes, vendored unmodified, in a fresh container; the agent's output file is the only thing the task hands over to it.
  - The agent works without network access apart from its own model endpoint, because the pages and their source texts are public.
  - The benchmark has no reference transcriptions, so the oracle solution is synthesized from each PDF's tests (see Notes & Caveats).

| Category (PDF folder) | Test file | Tasks (PDFs) | Unit tests | Test types | Explicit baseline tests | Added baseline tests | Graded tests |
|---|---|---:|---:|---|---:|---:|---:|
| `arxiv_math` | `arxiv_math.jsonl` | 522 | 2,927 | math | 0 | 522 | 3,449 |
| `headers_footers` | `headers_footers.jsonl` | 266 | 753 | absent | 7 | 259 | 1,019 |
| `long_tiny_text` | `long_tiny_text.jsonl` | 62 | 442 | present | 0 | 62 | 504 |
| `multi_column` | `multi_column.jsonl` | 231 | 884 | order | 0 | 231 | 1,115 |
| `old_scans` | `old_scans.jsonl` | 98 | 526 | 279 present, 70 absent, 177 order | 0 | 98 | 624 |
| `old_scans_math` | `old_scans_math.jsonl` | 36 | 458 | math | 0 | 36 | 494 |
| `tables` | `table_tests.jsonl` | 188 | 1,020 | table | 2 | 186 | 1,208 |
| **Total** | | **1,403** | **7,010** | | **9** | **1,394** | **8,413** |

The dataset files hold 7,019 test records: the 7,010 unit tests plus 9 explicit `baseline` records. The original harness adds a default baseline test to each of the other 1,394 PDFs, which gives 8,413 graded tests.

## What is olmOCR-Bench?

olmOCR-Bench was introduced with olmOCR ([Poznanski et al., 2025](https://arxiv.org/abs/2502.18443)) by the Allen Institute for AI to compare OCR pipelines and vision-language models on document types they find hard. Any tool that outputs Markdown or plain text can be evaluated. The benchmark avoids edit-distance metrics: each test is a simple fact about the output that either holds or does not.

Test types (implemented in [`olmocr/bench/tests.py`](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/tests.py)):

- **present / absent**: a short text must (not) appear, with fuzzy matching and optional restriction to the first or last N characters
- **order**: one passage must appear before another
- **table**: a cell must exist whose neighbours and headings have given values (Markdown or HTML tables)
- **math**: a LaTeX formula must appear; formulas are rendered with KaTeX in headless Chromium and compared by symbol layout
- **baseline**: the output is not blank, does not end in a long repetition, and contains no CJK or emoji characters

The original score is the percentage of tests passed per document source, with the baseline tests as an eighth source; the headline "Overall" number is the unweighted mean of those eight percentages, reported with a 95% bootstrap confidence interval.

## Adapter Features

- Downloads the dataset at a pinned revision from Hugging Face, or reads a local copy (`--source-dir`)
- One Harbor task per PDF with deterministic, registry-safe task IDs derived from the category and file name (`arxiv_math/2503.04048_pg46.pdf` → `arxiv-math-2503-04048-pg46`)
- Grading uses the original test classes: `tests.py`, `table_parsing.py`, `katex/render.py`, the bundled KaTeX 0.16.21 and `repeatdetect.py` are byte-for-byte copies of the pinned commit, shipped with their license in each task's `tests/upstream/`
- `tests/evaluate.py` reproduces what the original `benchmark.py` does for one PDF: loading through `load_tests`, the automatically added baseline test, pass/fail per test
- Reward is the fraction of the PDF's tests that pass; per-test results are saved as `olmocr_bench_results.json` in the verifier logs
- `olmocr-bench-score` rebuilds the original "Overall" score, per-category pass rates and bootstrap interval from a Harbor job directory
- The instruction contains the original "no document anchoring" API prompt verbatim, plus where to read the PDF and where and in which form to write the result
- Separate verifier: scoring runs in a fresh container without network access, and `/workspace/output.md` is the only artifact the task declares (Harbor also copies its own artifact directory `/logs/artifacts`, which the scoring code never reads)
- An output that is missing, is not a regular file or is not UTF-8 text is scored as an empty document, which is what the original pipeline scores when a system returns nothing for a page; the per-test results record which case it was
- No network during the agent phase other than the agent's model endpoint
- One Docker image definition for all tasks (digest-pinned Python 3.11 base, poppler-utils, pinned scoring dependencies, headless Chromium installed at build time); tasks differ only in the final `COPY` of the PDF, so image builds are cache hits and scoring needs no network
- Oracle solutions synthesized from the unit tests
- `--split parity` generates a deterministic 140-task subset with 20 PDFs per category
- Each task directory is built in a hidden staging directory inside the output directory and moved into place complete, so an interrupted run leaves no partial task. Only one run at a time writes to an output directory (file lock); file modes do not depend on the umask

## Generated Task Structure

```
olmocr-bench/
├── {task_id}/                          # e.g. arxiv-math-2503-04048-pg46
│   ├── task.toml                       # Task configuration and metadata
│   ├── instruction.md                  # Conversion instructions for the agent
│   ├── environment/
│   │   ├── Dockerfile                  # Same for every task except the PDF copied in
│   │   └── document.pdf                # The page to convert
│   ├── solution/
│   │   ├── solve.sh                    # Copies oracle.md to /workspace/output.md
│   │   └── oracle.md                   # Output synthesized from the page's tests
│   └── tests/
│       ├── test.sh                     # Runs evaluate.py, writes the reward
│       ├── evaluate.py                 # Single-PDF driver around the original tests
│       ├── task.json                   # Path of the PDF in the original dataset
│       ├── bench_data/
│       │   └── {source}.jsonl          # This PDF's tests, original records
│       └── upstream/                   # Original harness code (Apache-2.0), unmodified
│           ├── LICENSE
│           ├── LICENSE-KaTeX           # Only for pages with math tests
│           ├── README.md               # Provenance and checksums
│           └── olmocr/...
```

The adapter code directory:

```
src/olmocr-bench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── uv.lock
├── run_olmocr-bench.yaml
└── src/olmocr_bench/
    ├── __init__.py
    ├── adapter.py                      # Dataset loading and task generation
    ├── main.py                         # CLI entry point
    ├── oracle.py                       # Builds oracle outputs from unit tests
    ├── score.py                        # Recomputes the original scores from a job
    └── task-template/
        ├── task.toml
        ├── instruction.md
        ├── environment/
        │   └── Dockerfile
        ├── solution/
        │   └── solve.sh
        └── tests/
            ├── test.sh
            ├── evaluate.py
            └── upstream/
```

## Run Evaluation / Harness

The syntax of the commands below was checked against Harbor 0.25.0. The local-dataset commands were run with the oracle and `nop` agents. The registry commands cannot be run until the dataset is registered, and no model-backed agent has been run yet.

### Running with Datasets Registry

Once the dataset is published to the registry:

```bash
# Oracle agent (reference output)
uvx --from harbor==0.25.0 harbor run -d allenai/olmocr-bench

# A specific agent and model
uvx --from harbor==0.25.0 harbor run -d allenai/olmocr-bench -a <agent_name> -m "<model_name>"

# Parity subset (140 tasks)
uvx --from harbor==0.25.0 harbor run -d allenai/olmocr-bench@parity -a <agent_name> -m "<model_name>"
```

The dataset is not registered yet; until then, generate the tasks locally (see [Usage](#usage-create-task-directories)) and use one of the methods below.

### Using Job Configurations

```bash
# From the repository root: oracle run over the whole dataset
uvx --from harbor==0.25.0 harbor run -c src/olmocr-bench/run_olmocr-bench.yaml

# Another agent and model with the same configuration
uvx --from harbor==0.25.0 harbor run -c src/olmocr-bench/run_olmocr-bench.yaml -a <agent_name> -m "<model_name>"

# Without a configuration file
uvx --from harbor==0.25.0 harbor run -p datasets/olmocr-bench -a <agent_name> -m "<model_name>"

# Resume an interrupted job
uvx --from harbor==0.25.0 harbor job resume -p jobs/<job_name>
```

Results are written to `jobs/` (set by `jobs_dir` in the YAML file).

### Running Individual Trial

```bash
uvx --from harbor==0.25.0 harbor trial start -p datasets/olmocr-bench/<task_id>
uvx --from harbor==0.25.0 harbor trial start -p datasets/olmocr-bench/<task_id> -a <agent_name> -m "<model_name>"
```

### Network Access for Agents

Each task sets `network_mode = "no-network"` for the agent phase and for scoring. For the agent phase Harbor adds the model endpoint of the agent it installed and switches off the agent's own web search and fetch tools, so that the agent can call its model and nothing else. The agent is installed before this restriction applies. If Harbor reports that it cannot determine an agent's model endpoint, pass it explicitly:

```bash
uvx --from harbor==0.25.0 harbor run -p datasets/olmocr-bench -a <agent_name> -m "<model_name>" \
  --allow-agent-host <model-api-hostname>
```

The environment must be able to change the network policy between phases. With Docker, Harbor does this through an egress-control sidecar container, which needs nftables support in the host kernel. Two things can stop an agent from reaching its model although the endpoint is allowed; both are described under [Troubleshooting](#troubleshooting): a Docker daemon configured with its own DNS server, and provider-selection variables in the shell that starts Harbor.

### Computing the Original Scores

Harbor reports the mean reward over tasks. To get the numbers the original harness prints (per-category pass rates and their unweighted mean), run:

```bash
cd src/olmocr-bench
uv run olmocr-bench-score ../../jobs/<job_name>
```

Pass one job directory. If a job holds several trials of the same task (`-k`), the script combines them by majority vote per test, as the original harness does for repeats, so use one job per run when you want one score per run. If any trial has no verifier result, the script prints `Average Score: FAILED (...)`, lists those trials and exits with status 1, because leaving them out would raise the score; run them again with `harbor job resume -p jobs/<job_name> -f <ErrorType>`, or pass `--allow-unscored` to see the partial numbers. See [How to interpret the scores](#how-to-interpret-the-scores) for the relationship between the two scores. Each trial directory also keeps the agent's output at `artifacts/workspace/output.md` and the per-test results at `verifier/olmocr_bench_results.json`.

## Usage: Create Task Directories

```bash
cd src/olmocr-bench
uv run olmocr-bench --output-dir ../../datasets/olmocr-bench
```

Available flags:
- `--output-dir`: directory to write generated tasks (defaults to `datasets/olmocr-bench` at the repository root)
- `--limit`: generate only the first N tasks, after `--split` and `--task-ids` are applied (a negative value is an error)
- `--overwrite`: regenerate task directories that already exist (existing ones are skipped otherwise; an existing directory that lacks required files stops the run instead of being skipped)
- `--task-ids`: only generate these tasks; accepts Harbor task IDs (`arxiv-math-2503-04048-pg46`) or original PDF paths (`arxiv_math/2503.04048_pg46.pdf`). An ID that matches no task is an error
- `--split`: `full` (default, 1,403 tasks) or `parity` (140 tasks)
- `--source-dir`: use a local copy of the dataset (the directory containing `bench_data/`) instead of downloading it

The first run downloads about 0.36 GB into the Hugging Face cache. Generation takes about 15 seconds and is deterministic: repeated runs produce byte-identical task directories with the same file modes. A run that is interrupted leaves only complete task directories; run the same command again to add the rest. The generated dataset is about 760 MB (the PDFs plus a copy of the grading code in each task).

## Comparison with Original Benchmark (Parity)

**Parity is pending.** No parity experiment has been run yet, so the table and `parity_experiment.json` contain no scores.

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|
| pending | pending | Overall (mean of per-source pass rates, %) | pending | 140 of 1,403 planned (10%) | pending | pending |

### Oracle verification

The oracle was run on the full benchmark with Harbor 0.25.0 on Docker (`run_olmocr-bench.yaml`, with `-n 6` for six concurrent trials). The table shows the run made with the final task files:

| Tasks | Trials with reward 1.0 | Exceptions | Mean reward | Runtime |
|-------|------------------------|------------|-------------|---------|
| 1,403 | 1,403 | 0 | 1.000 | 1 h 36 min |

- An earlier full run, made before scoring was taken off the network and before the last revision of `evaluate.py`, gave the same result (1,403 of 1,403, no exceptions) in 1 h 12 min; the later run shared the machine with other jobs.
- `olmocr-bench-score` on that job reports "Average Score: 100.0% ± 0.0% (average of per-JSONL scores) over 8413 tests", with all eight sources at 100%.
- Independently of Harbor, the same oracle outputs pass every test, 8,413 of 8,413, under the original harness (`python -m olmocr.bench.benchmark` at the pinned commit: "Average Score: 100.0% ± 0.0%").

No task is excluded and no oracle fails.

### Evaluator replay against the original harness

To check that the adapter grades exactly like the original harness, the same Markdown files were scored on both sides without any model in the loop. These are evaluator replays and negative controls, not parity results.

- **Local replay, all 1,403 PDFs**: `pdftotext` output for every PDF was scored by the original `python -m olmocr.bench.benchmark` at the pinned commit and by each generated task's `tests/evaluate.py`. `evaluate.py` ran in the task image. The original harness ran in the same image with three extra packages that it imports at start-up and that the task image does not need (`pypdf`, `numpy`, `Pillow`; they serve the page-count check, the bootstrap interval and the optional HTML report, not the grading). The results are identical: 8,413 tests, the same 5,711 failing test IDs, and the same pass counts for all eight sources (original harness: "Average Score: 40.9% ± 0.9%"). `olmocr-bench-score` prints the same pass counts and the same Overall as the original harness for these results; its headline adds "over N tests", and the `±` digit can differ by 0.1 because the original bootstrap is unseeded.
- **Through Harbor, 36-task sample**: five jobs were run on the same 36 tasks and compared test by test with the original harness scoring the same files. All five agree exactly (278 graded tests each): the oracle (100.0%), no output file (the `nop` agent, 13.0%), an empty file (13.0%), an unrelated paragraph of filler text (29.2%) and `pdftotext` output (39.8%).
- **Through Harbor, 140-task parity subset**: five more jobs were run on the 140 parity tasks (942 graded tests) and compared in the same way. All five agree exactly with the original harness. The first job runs Harbor's `nop` agent, which writes nothing, on the unchanged tasks; the original harness cannot score a missing file, so its column for that row comes from empty files. For the other four, each task's `solve.sh` was replaced by a script that writes the output named in the table.

| Output in `/workspace/output.md` | Overall, original harness | Overall, `olmocr-bench-score` | Mean reward | Tasks at reward 1.0 |
|---|---:|---:|---:|---:|
| No file (`nop` agent) | 13.8% | 13.8% | 0.113 | 0 of 140 |
| Empty file | 13.8% | 13.8% | 0.113 | 0 of 140 |
| Unrelated filler text | 26.3% | 26.3% | 0.310 | 20 of 140 |
| The single letter `e` | 36.5% | 36.5% | 0.416 | 24 of 140 |
| `pdftotext` output | 40.7% | 40.7% | 0.427 | 17 of 140 |

These replays were made before the last revision of `evaluate.py`. That revision changed only how an unreadable output is scored and added bookkeeping fields, which none of the replayed outputs touches; the full oracle run above was repeated with the final version, and so was a seven-task check through Harbor in which a missing file, an empty file, a file that is not UTF-8 and a directory at the output path all received the same reward on every task.

None of the controls comes close to the oracle's 100%. The filler text and the single letter still get full reward on some tasks; the reason is given under "Known weaknesses inherited from the original tests" in [Notes & Caveats](#notes--caveats).

### Parity plan

The original harness has no agent support: it scores Markdown files produced by OCR tools or by one API call per page. Parity therefore needs a fork of the original repository that runs a Harbor-compatible agent (scenario 2 of the adapter guide). The agent, model and number of runs must be agreed with the Harbor team before anything is run; the proposal is:

- **Agent and model**: `claude-code` at a pinned version with `claude-haiku-4-5-20251001`. The agent must be able to look at the page, and Claude Code can read PDFs and images directly. A second standard agent (`codex` with `gpt-5-mini-2025-08-07`) is an optional generalization check.
- **Subset**: `--split parity`, 140 tasks. For each of the 7 categories, tasks are ranked by `sha256("42:<task_id>")` and the first 20 are taken. Equal allocation is used because the headline metric weights categories equally. The subset has 942 graded tests. None of the 9 PDFs with an explicit baseline record is in it; that path is covered by the full oracle run and the full replay.
- **Runs**: 3 per side, in the order required by the guide: 5 to 10 tasks on both sides, then one full subset run on both sides, then three. Each run is its own Harbor job, because `olmocr-bench-score` combines several trials of a task inside one job.
- **Metric**: Overall = unweighted mean of the eight per-source pass rates, in percent, as printed by `benchmark.py` on the original side and by `olmocr-bench-score` on the Harbor side.
- **Original side**: a runner has been written on top of `allenai/olmocr` at commit `f7cfe4c` (a new directory `harbor_parity/`; nothing under `olmocr/` is changed). It is not published yet; the fork URL will be added here and to `parity_experiment.json` once it is. It works in three steps:
  1. `prepare.py` takes the pages, the instruction text, the image build context and the limits from the generated Harbor tasks of the parity split, checks every PDF and every test record against the pinned dataset snapshot, and materializes a `bench_data` directory with only those 140 PDFs (the original harness requires an output file for every PDF it finds).
  2. `run_agent.py` runs one page at a time in the task's own image with the task's limits (2 CPUs, 4 GB, 1,800 s). It installs the agent while the network is open, then restricts the network to the model endpoint with a copy of Harbor's egress sidecar, and starts the agent with the command line, tool restrictions and environment variable names that Harbor 0.25.0 uses for `claude-code` (recorded from a Harbor trial and compared). Afterwards it copies `/workspace/output.md` to `<bench_data>/<candidate>/<category>/<name>_pg1_repeat1.md`. An output that is missing, is not a regular file or is not UTF-8 becomes an empty file, the same rule as in this adapter.
  3. `score.py` runs the unmodified `python -m olmocr.bench.benchmark --dir <bench_data> --candidate <candidate>`. The original harness cannot start in the task image, because `benchmark.py` imports `pypdf`, `numpy` and `pillow`; it runs in the task image with exactly these three pinned packages added (`pypdf==7.1.0`, `numpy==2.4.6`, `pillow==13.0.0`). Chromium, the fonts and all packages used by the test classes are the same as in the verifier.
- **Checked without a model**: stand-in "agents" that write `pdftotext` output, an empty file, no file and the oracle document were run through the whole original-side runner on the 140 parity pages and compared with Harbor jobs for the same outputs. Failing tests and Overall are identical on both sides (40.7%, 13.8%, 13.8% and 100.0%). No model run has been made on either side, so agent behaviour, token use and cost are still unmeasured.
- **Open points for the Harbor team**: the pinned agent version and dated model ID, the number of runs, whether a page whose agent command failed is run again on both sides or scored as it is, and how the agent reaches its model on hosts that need the DNS remedy described under [Troubleshooting](#troubleshooting).

### Reproduction

Harbor side:

```bash
# From the repository root
cd src/olmocr-bench
uv run olmocr-bench --split parity --output-dir ../../datasets/olmocr-bench-parity
cd ../..
uvx --from harbor==0.25.0 harbor run -p datasets/olmocr-bench-parity -a <agent> -m "<model>" --job-name <job_name>
cd src/olmocr-bench
uv run olmocr-bench-score ../../jobs/<job_name>
```

Original side (from the parity fork of `allenai/olmocr`, which is not published yet; Python 3.11 or newer and Docker):

```bash
hf download allenai/olmOCR-bench --repo-type dataset \
  --revision 54a96a6fb6a2bd3b297e59869491db4d3625b711 --local-dir ./olmOCR-bench
python3 harbor_parity/prepare.py --harbor-dataset <adapters repo>/datasets/olmocr-bench-parity \
  --dataset-snapshot ./olmOCR-bench --work-dir <work>
python3 harbor_parity/run_agent.py --work-dir <work> --candidate <run name> \
  --agent claude-code --model <model> --agent-version <version> --concurrency 4
python3 harbor_parity/score.py --work-dir <work> --candidate <run name>
```

`score.py` runs the unmodified `python -m olmocr.bench.benchmark` on the candidate folder in the scoring image described in the parity plan.

### How to interpret the scores

- **Task reward**: passed tests / graded tests of that PDF, including its baseline test.
- **Harbor's dataset metric**: the mean of the task rewards, i.e. an average over PDFs.
- **Original "Overall"**: tests are grouped by the file they come from (`arxiv_math.jsonl`, `headers_footers.jsonl`, `long_tiny_text.jsonl`, `multi_column.jsonl`, `old_scans.jsonl`, `old_scans_math.jsonl`, `table_tests.jsonl`), and the added baseline tests form an eighth group `baseline`. Each group's pass rate is passed tests / tests in the group, and Overall is the unweighted mean of the eight rates. The 9 explicit baseline records count towards their own file, not towards `baseline`. The `±` value is half the width of a 95% bootstrap interval that resamples tests within each group.

The two numbers are different aggregates of the same pass/fail outcomes and generally differ (for the `pdftotext` replay: Overall 40.9%, mean reward 0.385). Use `olmocr-bench-score` for anything that is compared with published olmOCR-Bench results. It reads the per-test records in each trial's `verifier/olmocr_bench_results.json`. If a trial has no such file (the environment did not start, the scoring harness did not complete, or Harbor could not hand over the output file), the script reports FAILED instead of a score, as the original harness does for a candidate with errors; otherwise an agent could raise its score by making outputs unusable on pages it does badly on.

Scores of agents are not directly comparable with the published olmOCR-Bench rows for API models: those are single model calls on a rendered page image, whereas an agent can read the PDF's text layer and use tools over many turns (`pdftotext` alone reaches 40.9% Overall). The interval uses 1,000 bootstrap samples, the default of the pinned code; the paper's table caption mentions 10,000.

### Links

- Original benchmark: https://github.com/allenai/olmocr (`olmocr/bench/`), dataset https://huggingface.co/datasets/allenai/olmOCR-bench
- Parity fork: not created yet
- Adapter PR: https://github.com/harbor-index/adapters/pull/3
- Dataset PR, parity results PR: not opened yet

## Notes & Caveats

**The oracle is derived from the tests and is not a transcription.** olmOCR-Bench provides no reference outputs. `oracle.py` builds, for each PDF, a document that contains exactly what its tests assert: asserted texts on their own lines in an order consistent with all reading-order tests, one small HTML table per table test, each formula as a `\[ ... \]` block, and nothing for absent tests. Pages whose only tests are absent tests (all 266 `headers_footers` pages) get the single line `Body text.` so that the baseline test passes. The oracle proves that every task's tests can be satisfied together and that the pipeline can award full reward. It says nothing about what a good transcription looks like.

**Prompt.** The instruction quotes `build_openai_silver_data_prompt_no_document_anchoring()` from `olmocr/bench/prompts.py` without changes. The original ChatGPT, Gemini and OpenAI-compatible-server runners select it with `prompt_template=full_no_document_anchoring`. The paper reports API models in "No Anchor" and "Anchored" configurations but does not print the prompt, so reading "No Anchor" as this template is an inference from the code. Three points are added to map the prompt onto files: the PDF is at `/workspace/document.pdf` and can be read with the installed poppler tools (there is no internet access), the result goes to `/workspace/output.md` with nothing but the document text, and "output null" means leaving the file empty. The anchored variant is not used because it embeds text pre-extracted from the PDF; an agent can extract that itself. The same instruction must be used on the original side for parity.

**Grading logic is unchanged.** No test, threshold or normalization was modified, and no test record was edited or dropped. The original loader does not filter on the `checked` field (2,810 records have no `checked` value; none are marked rejected), and neither does the adapter.

**Scoring happens in a separate container.** `task.toml` sets `environment_mode = "separate"` for the verifier and declares `/workspace/output.md` as the only artifact. After the agent finishes, Harbor copies that file into a fresh container built from the same image and runs the tests there, without network access. Harbor 0.25.0 also copies its own artifact directory `/logs/artifacts`, which is empty unless the agent writes to it; the scoring code never reads it. No other change made in the agent's container reaches the scoring container, so an agent with root access cannot alter Chromium, the Python packages or any other scoring tool. A control run confirmed this: a script that wrote an empty output and then, as root, replaced the Python interpreter, patched the fuzzy-matching library and planted reward files received exactly the reward of an honest empty output. A symbolic link in place of the output file gains nothing either: Harbor 0.25.0 does not carry links over, so the page is scored as if no file had been written, and `evaluate.py` reads the output only if it is a regular file. This was checked with links pointing at the tests and at the oracle file. The tests, the test classes and the oracle are never present in the agent's container (its image does contain the Python packages and the browser that scoring uses, because both phases share one image).

**No internet for the agent.** The pages are public documents. The LaTeX sources of the arXiv papers and human transcriptions of the Library of Congress scans can be found online, and some test strings were derived from them. With open internet access an agent could look the text up instead of reading the page. The agent phase therefore runs with `network_mode = "no-network"`; Harbor still lets the agent reach its own model endpoint and disables the agent's web search and fetch tools. This also rules out calling third-party OCR services or downloading OCR models, so results reflect what the agent can do with its model and the tools in the image. The original API runners make one model call per page and give the model no tools, so they could not look anything up either. The restriction was checked with a probe script run in place of the oracle: no outside name could be resolved and no TCP or UDP payload could be exchanged with an outside host (ICMP echo is not blocked by Harbor's sidecar). A probe in the scoring container, which runs with the same setting, could not resolve a name either and received no data for an HTTP request to an outside address. How Harbor 0.25.0 opens the model endpoint and disables web tools for an installed agent is taken from its source code and has not been exercised here, because no model-backed agent was run. Two things are outside the task's control: what a model remembers about a public document, and web tools that a model provider runs on its own servers if an agent does not let Harbor disable them.

**Missing and unusable outputs.** If `/workspace/output.md` does not exist, is not a regular file (a directory, a link, a special file) or cannot be read as UTF-8 text, the page is scored as an empty document, and `olmocr_bench_results.json` records which case it was (`output_status`). This follows the original conversion driver, which writes an empty file when a system returns nothing for a page; absent tests then pass and everything else fails. The original `benchmark.py` has no score for a file it cannot read: it reports `FAILED (errors)` for the whole candidate. Scoring such a page as empty keeps the two sides comparable, because an empty file is something the unmodified original harness can score; the parity runner has to apply the same rule. A test that raises an error counts as failed and is flagged in the results (`error`), and `olmocr-bench-score` warns about it; the original harness would again report `FAILED (errors)`. One case never reaches scoring: if the agent makes the file unreadable for Harbor itself (mode 000), Harbor 0.25.0 fails the trial with a `PermissionError` while copying it, and the trial has no verifier result (see the next paragraph).

**If the scoring harness itself fails**, for example because Chromium cannot start or the scoring process is killed for using too much memory, `test.sh` writes a reward of 0, exits with an error and keeps no per-test results. Harbor 0.25.0 ignores the exit status and records an ordinary trial with reward 0. `olmocr-bench-score` treats every trial without per-test results (this case, a verifier timeout, a trial that Harbor could not complete) as unscored and then reports FAILED instead of a score, so that such trials are run again rather than silently dropped.

**Known weaknesses inherited from the original tests.** These were measured with the original test classes and are reported here so that rewards are read correctly. They are not changed, because that would break comparability with the original benchmark.

- `headers_footers` pages have only absent tests. Any non-blank output that leaves out the listed headers and footers gets full reward: an unrelated paragraph of filler text scores 1.0 on all 266 of these tasks.
- Present tests use `rapidfuzz.fuzz.partial_ratio`, which aligns the shorter string inside the longer one. An output that is shorter than the expected text and occurs inside it passes: the one-letter output `e` passes 437 of 442 `long_tiny_text` tests and gets full reward on 114 tasks, and an output containing only whitespace passes 441 of them (but fails the baseline test).
- An empty or missing output still passes every absent test (original Overall 14.0%, mean reward 0.139). The baseline test is what penalizes it, on every task.

**What an agent can see about the origin of a page.** The instruction, the file name `document.pdf`, the image and its environment variables do not name the benchmark or carry identifiers from the original dataset, and the PDFs' metadata only records the tool that wrote the file. The instruction does quote the original prompt word for word, and that wording is public, so a model could recognise where it comes from; without internet access it cannot use that to look anything up. Task IDs keep the original file names for traceability (for `arxiv_math` they contain the arXiv identifier), but they are not visible to the agent with Harbor's default settings: in a probe of the agent phase with Harbor 0.25.0 on Docker, the host name was a random container ID and no host path appeared in the mount table. This is not guaranteed for every configuration (with Harbor's optional `auto_mounting` environment setting the mount table shows the host's trial path, which starts with the task ID), and other environment providers were not checked. The page content itself is unchanged, so a page may show a journal header, a DOI or an arXiv stamp, and at least 178 of the 1,403 PDFs carry link annotations.

**Math tests depend on the rendering environment.** Formulas are compared through bounding boxes measured in headless Chromium. The bundled KaTeX style sheet refers to font files that the original repository does not ship, so glyphs come from system fonts. The image pins the base image by digest, Playwright 1.65.1 (Chromium 158) and all Python packages; system libraries and fonts come from Debian bookworm. Results for borderline formulas may differ on other setups, so parity runs should score both sides in this image.

**Environment additions.** The image contains `poppler-utils` so that an agent can rasterize or inspect the PDF without downloading anything, and `STOPSIGNAL SIGKILL` so that containers stop without the 10 s grace period. Neither affects grading. The agent runs as root in its own container. `harbor lint` (Harbor 0.25.0) reports nothing for the generated tasks with its default rules; with every rule enabled it reports only `root-agent`.

**Resources.** Tasks request 2 CPUs and 4 GB of memory. Loading math tests starts up to 8 headless Chromium instances; scoring the page with the most formulas (30) peaked at 1.3 GB. In the full oracle run the scoring phase, which starts the second container, sets up its network policy and runs the tests, took 12 seconds for the median task and 54 seconds at most, on a machine that was running other jobs. Scoring time grows with the number of formulas in the output that cannot be rendered, because the original code tries each of them again for every formula test of the page: in a test, an output with 300 unparseable formulas on the page with the most formula tests (30) did not finish within 1,200 s. The verifier's time limit is therefore 3,600 s; an output that still exceeds it leaves the trial without a verifier result.

**Explicit baseline tests.** Nine PDFs carry their own baseline record, which switches off the CJK/emoji character check (`check_disallowed_characters: false`). They get no additional baseline test, as in the original harness.

## Installation / Prerequisites

- Linux or macOS. The adapter uses Unix file locking (`flock`) and has not been run on Windows
- Docker installed and running. The kernel that the Docker engine runs on must support nftables, which Harbor needs to restrict the network during the agent phase and during scoring
- [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer for the adapter
- Harbor CLI; the commands in this README use `uvx --from harbor==0.25.0 harbor`
- Adapter dependencies:
  ```bash
  cd src/olmocr-bench
  uv sync
  ```
- Network access to Hugging Face for the first task generation (not needed with `--source-dir`)
- Disk: 0.36 GB for the dataset download, about 760 MB for the generated tasks, about 1 GB for the Docker image and about 100 MB for the job directory of a full oracle run. The first image build downloads Chromium and system libraries; later builds reuse the cached layers, and Harbor removes each task's image after its trial
- Time: a trial with the oracle takes about 23 seconds (two container start-ups, each with its network policy, and scoring). The full oracle run over 1,403 tasks took 1 h 36 min with six concurrent trials on a machine with 16 CPUs and 30 GB of memory that was running other jobs at the same time, after the shared image layers had been built once
- API keys for the agent and model you want to run. The oracle needs none

## Troubleshooting

- **Download fails or is rate limited**: set `HF_TOKEN`, or download the dataset once and pass `--source-dir`.
- **Regenerated tasks look unchanged**: existing task directories are skipped unless `--overwrite` is given.
- **`... existing task director(ies) ... are incomplete`**: a task directory lacks required files, for example after an interrupted run of an earlier version of the adapter or after manual changes. Run again with `--overwrite`.
- **`Another olmocr-bench run is writing to ...`**: only one run at a time may write to an output directory. A hidden `.staging-*` directory left there by a killed run is removed by the next run.
- **First trial is slow**: the first build installs Chromium. Keep `force_build: true`; later builds only add the PDF layer.
- **Job aborts with "cannot change network policy after start"**: Harbor's check for kernel support of its egress-control sidecar timed out, usually because Docker was busy when the job started. Start the job again when Docker is idle.
- **Agent cannot reach its model**: three causes are known. (1) Harbor could not work out the agent's model endpoint: pass `--allow-agent-host <hostname>`. (2) The shell that starts Harbor exports provider-selection variables for the agent (for example `CLAUDE_CODE_USE_BEDROCK`), so the agent uses another backend than the one Harbor allows; in a test Harbor then logged that it could not determine the endpoint and allowed nothing. Start Harbor from a clean environment or pass `--allow-agent-host`. (3) The agent retries its API calls without getting an HTTP status and then times out, although Harbor logged that it allowed the model host. This was seen on a host whose Docker daemon is configured with its own DNS server: names cannot be resolved while the egress sidecar is active. Run with `--extra-docker-compose <file>`, where the file sets `network_mode: bridge` for the `harbor-docker-egress-control-sidecar` service.
- **A trial has reward 0 and `verifier/olmocr_bench_results.json` is missing**: the scoring harness failed. Check `verifier/test-stdout.txt` in the trial directory. The most likely cause is Chromium failing to start or the scoring process being killed under memory pressure; lower the number of concurrent trials or raise `memory_mb`. It did not happen in any of the runs reported in this README. `olmocr-bench-score` reports FAILED for the job until the trial has been run again.
- **`VerifierTimeoutError`**: scoring did not finish within its limit of 3,600 s. This can happen when an output contains hundreds of formulas that cannot be rendered on a page with many formula tests. The trial has no verifier result; raise the limit with `--verifier-timeout-multiplier` and run it again.
- **Score differs from published numbers**: Harbor's mean reward is not the original Overall score; run `olmocr-bench-score` on the job directory.
- **Containers left behind after an interrupted run**: remove the containers and images that Harbor named after the trials (the first 32 characters of the task name in lower case, followed by a random suffix).

## Citation

```bibtex
@misc{olmocrbench,
      title={{olmOCR: Unlocking Trillions of Tokens in PDFs with Vision Language Models}},
      author={Jake Poznanski and Jon Borchardt and Jason Dunkelberger and Regan Huff and Daniel Lin and Aman Rangapur and Christopher Wilhelm and Kyle Lo and Luca Soldaini},
      year={2025},
      eprint={2502.18443},
      archivePrefix={arXiv},
      primaryClass={cs.CL},
      url={https://arxiv.org/abs/2502.18443},
}
```

## Authors & Contributions

This adapter is developed and maintained by [Himanshu Gupta](mailto:him141195@gmail.com).

olmOCR-Bench was created by Jake Poznanski, Jon Borchardt, Jason Dunkelberger, Regan Huff, Daniel Lin, Aman Rangapur, Christopher Wilhelm, Kyle Lo and Luca Soldaini at the Allen Institute for AI.

**Issues and Contributions:**
- Submit issues and pull requests to the [Harbor adapters repository](https://github.com/harbor-framework/adapters)
- Follow the project's coding style and commit guidelines
