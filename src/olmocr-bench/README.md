# olmOCR-Bench → Harbor Adapter

## Overview

olmOCR-Bench measures how well a system converts PDF pages into clean, linearized Markdown / plain text. Instead of comparing against a reference transcription, every page comes with machine-checkable pass/fail "unit tests" about the output (a sentence is present, a header is absent, two passages appear in reading order, a table cell has the right neighbours, a formula renders identically).

- **Task type**: convert one single-page PDF to Markdown (OCR / document parsing)
- **Domains**: 7 document categories: arXiv math papers, old scanned math textbooks, tables, old scans (letters, typewritten documents), pages with headers/footers, multi-column layouts, long pages of tiny text
- **Dataset size**: 1,403 tasks, one per PDF, covering the whole benchmark. No PDFs or tests are excluded
- **Tests**: 7,010 unit tests in the dataset files (the number quoted by the paper and the dataset card), plus the per-PDF baseline test that the original harness adds, for 8,413 graded tests in total (see the table below)
- **Provenance**: [paper](https://arxiv.org/abs/2502.18443) | [dataset](https://huggingface.co/datasets/allenai/olmOCR-bench) (pinned to revision `54a96a6fb6a2bd3b297e59869491db4d3625b711`) | [original harness](https://github.com/allenai/olmocr/tree/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench) (pinned to commit `f7cfe4c22098b154c76b6ec950d1c0a464eecf8d`)
- **License**: dataset ODC-BY 1.0; harness code Apache-2.0
- **Main modifications for Harbor**:
  - The original harness scores a folder of Markdown files produced by an OCR system. Here an agent gets the PDF at `/workspace/document.pdf` and writes `/workspace/output.md`; the instruction wraps the original API-model prompt and only adds the file locations and the fact that there is no internet access.
  - Each task's reward is the fraction of that PDF's tests that pass. The original headline score aggregates differently (mean of per-category pass rates); `olmocr-bench-score` recomputes it from a Harbor job.
  - Grading runs the original test classes, vendored unmodified, in a fresh container that only receives the agent's output file.
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
- The instruction contains the original "no document anchoring" API prompt verbatim, plus where to read the PDF and where to write the result
- Separate verifier: scoring runs in a fresh container, and `/workspace/output.md` is the only file carried over from the agent's container
- No network during the agent phase other than the agent's model endpoint
- One Docker image definition for all tasks (digest-pinned Python 3.11 base, poppler-utils, pinned scoring dependencies, headless Chromium installed at build time); tasks differ only in the final `COPY` of the PDF, so image builds are cache hits and scoring needs no network
- Oracle solutions synthesized from the unit tests
- `--split parity` generates a deterministic 140-task subset with 20 PDFs per category

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

Commands below were verified with Harbor 0.25.0.

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

Each task sets `network_mode = "no-network"` for the agent phase. Harbor adds the model endpoint of the agent it installed and switches off the agent's own web search and fetch tools, so an agent can call its model and nothing else. The agent is installed before this restriction applies. If Harbor reports that it cannot determine an agent's model endpoint, pass it explicitly:

```bash
uvx --from harbor==0.25.0 harbor run -p datasets/olmocr-bench -a <agent_name> -m "<model_name>" \
  --allow-agent-host <model-api-hostname>
```

The environment must be able to change the network policy between phases. With Docker, Harbor does this through an egress-control sidecar container, which needs nftables support in the host kernel.

### Computing the Original Scores

Harbor reports the mean reward over tasks. To get the numbers the original harness prints (per-category pass rates and their unweighted mean), run:

```bash
cd src/olmocr-bench
uv run olmocr-bench-score ../../jobs/<job_name>
```

See [How to interpret the scores](#how-to-interpret-the-scores) for the relationship between the two. Each trial directory also keeps the agent's output at `artifacts/workspace/output.md` and the per-test results at `verifier/olmocr_bench_results.json`.

## Usage: Create Task Directories

```bash
cd src/olmocr-bench
uv run olmocr-bench --output-dir ../../datasets/olmocr-bench
```

Available flags:
- `--output-dir`: directory to write generated tasks (defaults to `datasets/olmocr-bench` at the repository root)
- `--limit`: generate only the first N tasks, after `--split` and `--task-ids` are applied
- `--overwrite`: regenerate task directories that already exist (existing ones are skipped otherwise)
- `--task-ids`: only generate these tasks; accepts Harbor task IDs (`arxiv-math-2503-04048-pg46`) or original PDF paths (`arxiv_math/2503.04048_pg46.pdf`)
- `--split`: `full` (default, 1,403 tasks) or `parity` (140 tasks)
- `--source-dir`: use a local copy of the dataset (the directory containing `bench_data/`) instead of downloading it

The first run downloads about 0.36 GB into the Hugging Face cache. Generation takes about 10 seconds and is deterministic: repeated runs produce byte-identical task directories. The generated dataset is about 760 MB (the PDFs plus a copy of the grading code in each task).

## Comparison with Original Benchmark (Parity)

**Parity is pending.** No parity experiment has been run yet, so the table and `parity_experiment.json` contain no scores.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|----------------|--------------|--------------------------------|----------------------------|
| pending | pending | Overall (mean of per-source pass rates, %) | pending | 140 of 1,403 planned (10%) | pending | pending |

### Oracle verification

The oracle was run on the full benchmark with Harbor 0.25.0 on Docker (`run_olmocr-bench.yaml`, with `-n 6` for six concurrent trials):

| Tasks | Trials with reward 1.0 | Exceptions | Mean reward | Runtime |
|-------|------------------------|------------|-------------|---------|
| 1,403 | 1,403 | 0 | 1.000 | 1 h 12 min |

- `olmocr-bench-score` on that job reports "Average Score: 100.0% ± 0.0% (average of per-JSONL scores) over 8413 tests", with all eight sources at 100%.
- Independently of Harbor, the same oracle outputs pass every test, 8,413 of 8,413, under the original harness (`python -m olmocr.bench.benchmark` at the pinned commit: "Average Score: 100.0% ± 0.0%").

No task is excluded and no oracle fails.

### Evaluator replay against the original harness

To check that the adapter grades exactly like the original harness, the same Markdown files were scored on both sides without any model in the loop. These are evaluator replays and negative controls, not parity results.

- **Local replay, all 1,403 PDFs**: `pdftotext` output for every PDF was scored by the original `python -m olmocr.bench.benchmark` at the pinned commit and by each generated task's `tests/evaluate.py`, both inside the task image. The results are identical: 8,413 tests, the same 5,711 failing test IDs, and the same pass counts for all eight sources (original harness: "Average Score: 40.9% ± 0.9%"). `olmocr-bench-score` prints the same summary lines as the original harness for these results.
- **Through Harbor, 36-task sample**: five jobs were run on the same 36 tasks and compared test by test with the original harness scoring the same files. All five agree exactly (278 graded tests each): the oracle (100.0%), no output file (the `nop` agent, 13.0%), an empty file (13.0%), an unrelated paragraph of filler text (29.2%) and `pdftotext` output (39.8%).
- **Through Harbor, 140-task parity subset**: five more jobs were run on the 140 parity tasks (942 graded tests) and compared in the same way. All five agree exactly with the original harness. The first job runs Harbor's `nop` agent, which writes nothing, on the unchanged tasks. For the other four, each task's `solve.sh` was replaced by a script that writes the output named in the table.

| Output in `/workspace/output.md` | Overall, original harness | Overall, `olmocr-bench-score` | Mean reward | Tasks at reward 1.0 |
|---|---:|---:|---:|---:|
| No file (`nop` agent) | 13.8% | 13.8% | 0.113 | 0 of 140 |
| Empty file | 13.8% | 13.8% | 0.113 | 0 of 140 |
| Unrelated filler text | 26.3% | 26.3% | 0.310 | 20 of 140 |
| The single letter `e` | 36.5% | 36.5% | 0.416 | 24 of 140 |
| `pdftotext` output | 40.7% | 40.7% | 0.427 | 17 of 140 |

None of the controls comes close to the oracle's 100%. The filler text and the single letter still get full reward on some tasks; the reason is given under "Known weaknesses inherited from the original tests" in [Notes & Caveats](#notes--caveats).

### Parity plan

The original harness has no agent support: it scores Markdown files produced by OCR tools or by one API call per page. Parity therefore needs a fork of the original repository that runs a Harbor-compatible agent (scenario 2 of the adapter guide). The agent, model and number of runs must be agreed with the Harbor team before anything is run; the proposal is:

- **Agent and model**: `claude-code` at a pinned version with `claude-haiku-4-5-20251001`. The agent must be able to look at the page, and Claude Code can read PDFs and images directly. A second standard agent (`codex` with `gpt-5-mini-2025-08-07`) is an optional generalization check.
- **Subset**: `--split parity`, 140 tasks. For each of the 7 categories, tasks are ranked by `sha256("42:<task_id>")` and the first 20 are taken. Equal allocation is used because the headline metric weights categories equally. The subset has 942 graded tests.
- **Runs**: 3 per side, in the order required by the guide: 5 to 10 tasks on both sides, then one full subset run on both sides, then three.
- **Metric**: Overall = unweighted mean of the eight per-source pass rates, in percent, as printed by `benchmark.py` on the original side and by `olmocr-bench-score` on the Harbor side.
- **Original side, to be built in a fork of `allenai/olmocr` at commit `f7cfe4c`**:
  1. A script that materializes a `bench_data` directory with only the 140 parity PDFs and their test records (the original harness requires an output file for every PDF it finds).
  2. A runner that, for each PDF, starts the same container image as the Harbor task (this adapter's `Dockerfile`, PDF at `/workspace/document.pdf`), installs the same agent version, then cuts network access down to the model endpoint and runs the agent headless with the text of this adapter's `instruction.md`, the same model and the same 1,800 s limit. Afterwards it copies `/workspace/output.md` byte for byte to `<bench_data>/<candidate>/<category>/<name>_pg1_repeat1.md`. If the agent wrote no file, the runner writes an empty file, as `convert.py` does for a failed conversion.
  3. Scoring with the unmodified `python -m olmocr.bench.benchmark --dir <bench_data> --candidate <candidate>`, executed inside the same image so that both sides use the same Chromium and fonts for math tests.

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

Original side (scoring part, already usable; the agent runner is still to be written):

```bash
git clone https://github.com/allenai/olmocr.git && cd olmocr
git checkout f7cfe4c22098b154c76b6ec950d1c0a464eecf8d
pip install -e ".[bench]" && playwright install chromium
hf download allenai/olmOCR-bench --repo-type dataset \
  --revision 54a96a6fb6a2bd3b297e59869491db4d3625b711 --local-dir ./olmOCR-bench
# put one Markdown file per PDF at
#   ./olmOCR-bench/bench_data/<candidate>/<category>/<pdf name without .pdf>_pg1_repeat1.md
python -m olmocr.bench.benchmark --dir ./olmOCR-bench/bench_data --candidate <candidate>
```

### How to interpret the scores

- **Task reward**: passed tests / graded tests of that PDF, including its baseline test.
- **Harbor's dataset metric**: the mean of the task rewards, i.e. an average over PDFs.
- **Original "Overall"**: tests are grouped by the file they come from (`arxiv_math.jsonl`, `headers_footers.jsonl`, `long_tiny_text.jsonl`, `multi_column.jsonl`, `old_scans.jsonl`, `old_scans_math.jsonl`, `table_tests.jsonl`), and the added baseline tests form an eighth group `baseline`. Each group's pass rate is passed tests / tests in the group, and Overall is the unweighted mean of the eight rates. The 9 explicit baseline records count towards their own file, not towards `baseline`. The `±` value is half the width of a 95% bootstrap interval that resamples tests within each group.

The two numbers are different aggregates of the same pass/fail outcomes and generally differ (for the `pdftotext` replay: Overall 40.9%, mean reward 0.385). Use `olmocr-bench-score` for anything that is compared with published olmOCR-Bench results. It reads the per-test records in each trial's `verifier/olmocr_bench_results.json`. Trials without such a file (the scoring harness did not complete) are listed separately and left out of the totals.

### Links

- Original benchmark: https://github.com/allenai/olmocr (`olmocr/bench/`), dataset https://huggingface.co/datasets/allenai/olmOCR-bench
- Parity fork: not created yet
- Adapter PR, dataset PR, parity results PR: not opened yet

## Notes & Caveats

**The oracle is derived from the tests and is not a transcription.** olmOCR-Bench provides no reference outputs. `oracle.py` builds, for each PDF, a document that contains exactly what its tests assert: asserted texts on their own lines in an order consistent with all reading-order tests, one small HTML table per table test, each formula as a `\[ ... \]` block, and nothing for absent tests. Pages whose only tests are absent tests (all 266 `headers_footers` pages) get the single line `Body text.` so that the baseline test passes. The oracle proves that every task's tests can be satisfied together and that the pipeline can award full reward. It says nothing about what a good transcription looks like.

**Prompt.** The instruction quotes `build_openai_silver_data_prompt_no_document_anchoring()` from `olmocr/bench/prompts.py` without changes. The original ChatGPT, Gemini and OpenAI-compatible-server runners select it with `prompt_template=full_no_document_anchoring`. The paper reports API models in "No Anchor" and "Anchored" configurations but does not print the prompt, so reading "No Anchor" as this template is an inference from the code. Three points are added to map the prompt onto files: the PDF is at `/workspace/document.pdf` and can be read with the installed poppler tools (there is no internet access), the result goes to `/workspace/output.md` with nothing but the document text, and "output null" means leaving the file empty. The anchored variant is not used because it embeds text pre-extracted from the PDF; an agent can extract that itself. The same instruction must be used on the original side for parity.

**Grading logic is unchanged.** No test, threshold or normalization was modified, and no test record was edited or dropped. The original loader does not filter on the `checked` field (2,810 records have no `checked` value; none are marked rejected), and neither does the adapter.

**Scoring happens in a separate container.** `task.toml` sets `environment_mode = "separate"` for the verifier and declares `/workspace/output.md` as the only artifact. After the agent finishes, Harbor copies that one file into a fresh container built from the same image and runs the tests there. Nothing else from the agent's container survives, so an agent with root access cannot alter Chromium, the Python packages or any other scoring tool. A control run confirmed this: a script that wrote an empty output and then, as root, replaced the Python interpreter, patched the fuzzy-matching library and planted reward files received exactly the reward of an honest empty output. A symbolic link in place of the output file gains nothing either: Harbor 0.25.0 does not carry links over, so the page is scored as if no file had been written, and `evaluate.py` additionally fails every test if the output path resolves into the tests directory. This was checked with links pointing at the tests and at the oracle file. The tests, the grading code and the oracle are never present in the agent's container.

**No internet for the agent.** The pages are public documents. The LaTeX sources of the arXiv papers and human transcriptions of the Library of Congress scans can be found online, and some test strings were derived from them. With open internet access an agent could look the text up instead of reading the page. The agent phase therefore runs with `network_mode = "no-network"`; Harbor still lets the agent reach its own model endpoint and disables the agent's web search and fetch tools. This also rules out calling third-party OCR services or downloading OCR models, so results reflect what the agent can do with its model and the tools in the image. The original API runners make one model call per page and give the model no tools, so they could not look anything up either. The restriction was checked with a probe script run in place of the oracle, which could not resolve or reach any outside host. How Harbor 0.25.0 opens the model endpoint and disables web tools for an installed agent is taken from its source code and has not been exercised here, because no model-backed agent was run. Two things are outside the task's control: what a model remembers about a public document, and web tools that a model provider runs on its own servers if an agent does not let Harbor disable them.

**Missing and unreadable outputs.** If `/workspace/output.md` does not exist, the page is scored as an empty document. This follows the original conversion driver, which writes an empty file when a system returns nothing for a page; absent tests then pass and everything else fails. A file that cannot be read as UTF-8 text fails every test, as in the original `benchmark.py`; so does a directory at that path.

**If the scoring harness itself fails**, for example because Chromium cannot start, `test.sh` writes a reward of 0, exits with an error and keeps no per-test results. `olmocr-bench-score` lists such trials as unscored instead of counting their tests as failed.

**Known weaknesses inherited from the original tests.** These were measured with the original test classes and are reported here so that rewards are read correctly. They are not changed, because that would break comparability with the original benchmark.

- `headers_footers` pages have only absent tests. Any non-blank output that leaves out the listed headers and footers gets full reward: an unrelated paragraph of filler text scores 1.0 on all 266 of these tasks.
- Present tests use `rapidfuzz.fuzz.partial_ratio`, which aligns the shorter string inside the longer one. An output that is shorter than the expected text and occurs inside it passes: the one-letter output `e` passes 437 of 442 `long_tiny_text` tests and gets full reward on 114 tasks, and an output containing only whitespace passes 441 of them (but fails the baseline test).
- An empty or missing output still passes every absent test (original Overall 14.0%, mean reward 0.139). The baseline test is what penalizes it, on every task.

**What an agent can see about the origin of a page.** The instruction, the file name `document.pdf`, the image and its environment variables do not name the benchmark or carry identifiers from the original dataset, and the PDFs' metadata only records the tool that wrote the file. The instruction does quote the original prompt word for word, and that wording is public, so a model could recognise where it comes from; without internet access it cannot use that to look anything up. Task IDs keep the original file names for traceability (for `arxiv_math` they contain the arXiv identifier), but they are not visible to the agent: in a probe of the agent phase with Harbor 0.25.0 on Docker, the host name was a random container ID, no host path appeared in the mount table, a reverse DNS lookup of the container's own address returned nothing, and no outside host could be resolved or reached. Other environment providers were not checked. The page content itself is unchanged, so a page may show a journal header, a DOI or an arXiv stamp.

**Math tests depend on the rendering environment.** Formulas are compared through bounding boxes measured in headless Chromium. The bundled KaTeX style sheet refers to font files that the original repository does not ship, so glyphs come from system fonts. The image pins the base image by digest, Playwright 1.65.1 (Chromium 158) and all Python packages; system libraries and fonts come from Debian bookworm. Results for borderline formulas may differ on other setups, so parity runs should score both sides in this image.

**Environment additions.** The image contains `poppler-utils` so that an agent can rasterize or inspect the PDF without downloading anything, and `STOPSIGNAL SIGKILL` so that containers stop without the 10 s grace period. Neither affects grading. The agent runs as root in its own container. `harbor lint` (Harbor 0.25.0) reports nothing for the generated tasks with its default rules; with every rule enabled it reports only `root-agent`.

**Resources.** Tasks request 2 CPUs and 4 GB of memory. Loading math tests starts up to 8 headless Chromium instances; scoring the page with the most formulas (30) peaked at 1.3 GB. In the full oracle run the scoring phase, which starts the second container and runs the tests, took 7 seconds for the median task and 18 seconds at most.

**Explicit baseline tests.** Nine PDFs carry their own baseline record, which switches off the CJK/emoji character check (`check_disallowed_characters: false`). They get no additional baseline test, as in the original harness.

## Installation / Prerequisites

- Docker installed and running. The kernel that the Docker engine runs on must support nftables, which Harbor needs to restrict the network during the agent phase
- [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer for the adapter
- Harbor CLI; the commands in this README use `uvx --from harbor==0.25.0 harbor`
- Adapter dependencies:
  ```bash
  cd src/olmocr-bench
  uv sync
  ```
- Network access to Hugging Face for the first task generation (not needed with `--source-dir`)
- Disk: 0.36 GB for the dataset download, about 760 MB for the generated tasks, about 1 GB for the Docker image and about 100 MB for the job directory of a full oracle run. The first image build downloads Chromium and system libraries; later builds reuse the cached layers, and Harbor removes each task's image after its trial
- Time: a trial with the oracle takes about 18 seconds (two container start-ups and scoring). The full oracle run over 1,403 tasks took 1 h 12 min with six concurrent trials on a machine with 16 CPUs and 30 GB of memory, after the shared image layers had been built once
- API keys for the agent and model you want to run. The oracle needs none

## Troubleshooting

- **Download fails or is rate limited**: set `HF_TOKEN`, or download the dataset once and pass `--source-dir`.
- **Regenerated tasks look unchanged**: existing task directories are skipped unless `--overwrite` is given.
- **First trial is slow**: the first build installs Chromium. Keep `force_build: true`; later builds only add the PDF layer.
- **Job aborts with "cannot change network policy after start"**: Harbor's check for kernel support of its egress-control sidecar timed out, usually because Docker was busy when the job started. Start the job again when Docker is idle.
- **Agent cannot reach its model**: Harbor could not work out the agent's model endpoint. Pass `--allow-agent-host <hostname>`.
- **A trial has reward 0 and `verifier/olmocr_bench_results.json` is missing**: the scoring harness failed. Check `verifier/test-stdout.txt` in the trial directory. The most likely cause is Chromium failing to start, for example under memory pressure; lower the number of concurrent trials or raise `memory_mb`. It did not happen in any of the runs reported in this README.
- **Score differs from published numbers**: Harbor's mean reward is not the original Overall score; run `olmocr-bench-score` on the job directory.
- **Containers left behind after an interrupted run**: remove the containers and images whose names start with the affected task IDs.

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
