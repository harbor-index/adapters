# PG-LLM → Harbor Adapter

## Overview

The `pg-llm` adapter converts the public frozen evaluation split of [PG-LLM (ProteinGym-LLM)](https://www.biorxiv.org/content/10.64898/2026.07.27.741045v3) into Harbor task format. PG-LLM evaluates whether general-purpose language models and agents can rank protein mutant sequences by experimental fitness using only the wild-type amino acid sequence, candidate mutant sequences, and an assay description.

- **Source benchmark**: [`rohitarorayyc/proteingym-llm`](https://github.com/rohitarorayyc/proteingym-llm) (commit `3f5f1d31e79365922bf13ddca1942bed0b609db3`, frozen bundle `eval-data-v1.2`)
- **Default split used in this adapter**: `n50_b1` (`size=50`, `seed=1`, **217 tasks** across all 217 ProteinGym v1.3 substitution assays; supports `--seeds 1 2 3` for all 651 episodes and `--split parity` for a 50-task stratified subset)
- **Excluded upstream split**: The 59 temporally held-out assays reported in the preprint are access-controlled and not included in the public `eval-data-v1.2` release
- **Domain**: Computational Biology / Protein Engineering (`science`)
- **Evaluation metric**: Per-assay Spearman rank correlation (`ρ ∈ [-1, 1]`) between predicted ranking and ground-truth experimental DMS scores
- **License & terms**: Adapter code adapted from upstream is MIT-licensed and ships with full license text (`THIRD_PARTY_LICENSES.md` in the wheel, dataset root, and verifier `tests/` directory); upstream ProteinGym v1.3 assays retain their original terms and are strictly evaluation-only (no training, fine-tuning, RAG, or prompt tuning against released splits/labels)
- **Key adaptations**: Preserves the upstream `ranking-v1` system and user prompt with a single unambiguous file-submission directive (`/app/answer.json`) plus tool-free constraints, configures `--network-mode {public,no-network}` (default `public` for local Docker portability including macOS Docker Desktop; `--network-mode no-network` for strict egress isolation on Daytona, Modal, or Linux Docker with `CONFIG_NFT_FIB_INET`), and isolates held-out DMS scores, `grade.py`, and license notices inside a separate verifier container (`verifier.environment_mode = "separate"`)

## What is PG-LLM?

PG-LLM (*"PG-LLM: Benchmarking General-Purpose Language Models for Protein Variant Ranking"*, Arora et al., 2026) reformulates [ProteinGym](https://proteingym.org/) Deep Mutational Scanning (DMS) substitution assays into self-contained natural-language ranking problems for general-purpose LLMs. Each task presents the target protein name, organism, assay description, wild-type sequence, and a shuffled set of `N=50` full-length mutant sequences (`M01`..`M50`) without mutation shorthand, structures, alignments, or labels, and asks the model to return a JSON ranking `{"ranking": [...]}` ordered from highest to lowest predicted fitness. Upstream scoring computes exact Spearman rank correlation (`ρ`) with average-rank tie handling and fails closed on missing or duplicate variant IDs.

- **Paper**: [bioRxiv 10.64898/2026.07.27.741045v3](https://www.biorxiv.org/content/10.64898/2026.07.27.741045v3)
- **Repository**: [`rohitarorayyc/proteingym-llm`](https://github.com/rohitarorayyc/proteingym-llm)

## Adapter Features

- Automatically downloads (enforcing a monotonic wall-clock deadline across DNS resolution, per-address TCP connect, TLS handshake, HTTP headers, redirects, and chunked streaming, plus metadata/compressed size caps and HTTP exception handling), caches (`XDG_CACHE_HOME` or `~/.cache/harbor-pg-llm`), and SHA-256 verifies the official `eval-data-v1.2` release (`proteingym-llm-eval-v1.2.tar.gz` and `eval_bundle_manifest.json`), or consumes a verified local directory via `--data-dir` (rejecting symlinks along explicit `--data-dir`, cache paths, and user-created parent directories)
- Preserves the exact upstream `ranking-v1` system and user prompt (`SYSTEM_PROMPT` + `build_user_prompt`) with the last-line stdout directive cleanly replaced by `/app/answer.json` output
- Generates deterministic, registry-safe task IDs (`markslab/pg-llm__<dms-slug>-n<size>-b<seed>`) with full assay metadata in `task.toml` and `tests/labels.json`, recording SHA-256 ownership (including `root_license_sha256` for root `THIRD_PARTY_LICENSES.md`) in `.pg-llm-manifest.json`, staging write-ahead journaled swaps/pruning outside `output_dir` (`.pg-llm-tx` guarded by an immutable 0-byte `O_NOFOLLOW` sibling `.pg-llm.lock`, with `os.replace`-atomic `.pg-llm-tx.state` markers, dedicated `tmp/` scratch ownership checks, crash-recoverable initialization/rollback/resume, stale manifest-only entry detection and `--overwrite` pruning, and destination-state and manifest verification before cleanup), adopting byte-identical unrecorded PG-LLM task directories into `.pg-llm-manifest.json` while preserving modified, incomplete, ambiguous, or unrelated unrecorded directories untouched
- Isolates verification via `verifier.environment_mode = "separate"` and `artifacts = ["/app/answer.json"]`, keeping `tests/labels.json`, `grade.py`, and `tests/THIRD_PARTY_LICENSES.md` out of the agent sandbox, and supports `--network-mode {public,no-network}` across `[environment]`, `[agent]`, and `[verifier]`
- Runs the agent as a non-root `agent` user inside a minimal pinned `python:3.11-slim` image with zero bioinformatic packages or pretrained model weights installed
- Re-implements upstream `parse_ranking` (linear-time scan), `_rankdata`, and `score_ranking` (`O(N log N)` rank sorting) in pure Python stdlib (`tests/grade.py`) with bounded `16 MiB` verifier reads, atomic reward-last publishing, explicit submission/verifier fault separation, and strict Harbor v3 job / v2 trial lock, `JobStats`, full config, digest, provenance, and coverage-gated `pg-llm-score` aggregation (`src/pg_llm/score.py`)
- Supports `--split parity` (50 stratified assays: 10 per functional category across `Activity`, `Binding`, `Expression`, `OrganismalFitness`, and `Stability`, RNG seed `42`, yielding 50 tasks per selected seed)

## Generated Task Structure

```
datasets/pg-llm/
├── THIRD_PARTY_LICENSES.md           # Full upstream MIT license, provenance, and evaluation-only terms
└── <dms-slug>-n50-b1/
    ├── task.toml                     # Task identity, authors, metadata, separate verifier & network config
    ├── instruction.md                # PG-LLM ranking-v1 prompt adapted cleanly to write /app/answer.json
    ├── environment/
    │   └── Dockerfile                # Minimal pinned python:3.11-slim image with non-root user `agent`
    ├── solution/
    │   └── solve.sh                  # Oracle script writing ground-truth DMS-sorted ranking to /app/answer.json
    └── tests/
        ├── test.sh                   # Verifier entrypoint invoking grade.py (exits non-zero on verifier fault)
        ├── grade.py                  # Stdlib Spearman grader (linear-time parser, O(N log N) ranker) publishing details then reward atomically
        ├── THIRD_PARTY_LICENSES.md   # Upstream MIT notice and ProteinGym terms carried per task (verifier-only)
        └── labels.json               # Held-out ground-truth DMS scores and metadata for this episode
```

Adapter package layout under `src/pg-llm/`:

```
src/pg-llm/
├── pyproject.toml
├── uv.lock
├── README.md
├── THIRD_PARTY_LICENSES.md
├── adapter_metadata.json
├── parity_experiment.json
├── run_pg-llm.yaml
└── src/
    └── pg_llm/
        ├── __init__.py
        ├── adapter.py
        ├── main.py
        ├── score.py
        └── task-template/
            ├── task.toml
            ├── instruction.md
            ├── environment/
            │   └── Dockerfile
            ├── solution/
            │   └── solve.sh
            └── tests/
                ├── test.sh
                ├── grade.py
                └── THIRD_PARTY_LICENSES.md
```

## Run Evaluation / Harness

### Running with Harbor Registry (post-publish)

```bash
# Oracle verification
harbor run -d markslab/pg-llm

# Evaluate an agent and model
harbor run -d markslab/pg-llm -a <agent_name> -m "<model_name>"

# If running a cleanroom dataset generated with `--network-mode no-network`, use either a pre-built agent image
# or pass setup registry hosts alongside the rollout inference host:
harbor run -d markslab/pg-llm -a <agent_name> -m "<model_name>" --allow-environment-host <setup_registry_host> --allow-agent-host <provider_host>
```

### Using Local Dataset or Config File

Run these commands from the adapters repository root (where `datasets/pg-llm`, `src/pg-llm`, and `jobs/` resolve directly):

```bash
# Oracle verification using the reference config
harbor run -c src/pg-llm/run_pg-llm.yaml

# Run locally generated tasks with a specific agent and model (Docker)
harbor run -p datasets/pg-llm -a <agent_name> -m "<model_name>"

# Cleanroom run on Daytona (after generating with `--network-mode no-network`):
# either preinstall the CLI agent into the environment image or allow setup hosts separately from rollout hosts
harbor run -p datasets/pg-llm-cleanroom -e daytona -a <agent_name> -m "<model_name>" --allow-environment-host <setup_registry_host> --allow-agent-host <provider_host>

# Single-task smoke test
harbor trial start -p datasets/pg-llm/a0a140d2t1-zikv-sourisseau-2019-n50-b1 -a oracle

# Compute both Harbor flat mean and upstream per-seed UniProt-nested macro average
# (pass --dataset-dir datasets/pg-llm when scoring relocated job artifacts)
uv run --project src/pg-llm pg-llm-score jobs/<job_name>
```

## Usage: Create Task Directories

```bash
cd src/pg-llm
uv sync

# Default: generate all 217 primary tasks (N=50, seed=1, network_mode=public) to ../../datasets/pg-llm
uv run pg-llm --output-dir ../../datasets/pg-llm

# Cleanroom generation for Daytona / Modal / Linux Docker with CONFIG_NFT_FIB_INET
uv run pg-llm --network-mode no-network --output-dir ../../datasets/pg-llm-cleanroom

# Generate the 50-task stratified parity subset (10 assays per selection type -> 50 tasks per seed, seed=42)
uv run pg-llm --split parity --output-dir ../../datasets/pg-llm-parity

# Generate all 651 tasks (N=50, seeds 1, 2, 3)
uv run pg-llm --seeds 1 2 3 --output-dir ../../datasets/pg-llm-all

# Reuse a pre-downloaded eval-data-v1.2 directory and limit task count
uv run pg-llm --data-dir /path/to/eval-data-v1.2 --limit 5 --overwrite --output-dir ../../datasets/pg-llm-smoke
```

Available CLI flags:
- `--output-dir` — Output directory for generated tasks (defaults to `<repo>/datasets/pg-llm` inside a source checkout, or `./datasets/pg-llm` in the current working directory otherwise)
- `--data-dir` — Optional path to an already-extracted `eval-data-v1.2` directory (SHA-256 verified); otherwise uses `$XDG_CACHE_HOME/harbor-pg-llm` (`~/.cache/harbor-pg-llm`)
- `--size` — Nominal mutant candidate set size per task (`10`, `50`, or `100`; default: `50`; `NPC1_HUMAN_Erwood_2022_RPE1` contains `63` candidates at nominal `N=100`)
- `--seeds` — Evaluation batch seeds (`1`, `2`, `3`; default: `1`)
- `--split` — `all` (217 assays) or `parity` (50 stratified assays across the 5 `SELECTION_TYPES`, yielding 50 tasks per selected seed)
- `--network-mode` — `public` (default, compatible with all local Docker hosts including macOS Docker Desktop) or `no-network` (enforces zero-egress isolation on Daytona, Modal, or Linux Docker with `CONFIG_NFT_FIB_INET`; pass `--allow-agent-host <provider_host>` when running in-container agents)
- `--limit` — Maximum number of tasks to generate
- `--overwrite` — Journaled replacement of PG-LLM tasks (manifest-owned or byte-identical unrecorded tasks adopted into `.pg-llm-manifest.json`, with verified rollback on interruption) and pruning of stale verified manifest-owned PG-LLM task directories outside the current selection (preserves modified, incomplete, ambiguous, or unrelated unrecorded directories untouched; differing existing tasks fail without `--overwrite`)
- `--task-ids` — Filter to specific sanitized task IDs or upstream `DMS_id` names (unmatched IDs raise an error)

## Comparison with Original Benchmark (Parity)

> **WIP Status**: Across all 217 `n50_b1` tasks, 217/217 Oracle rankings achieve their exact strict-permutation Spearman theoretical maximum (`188/217` equal `1.0` with distinct `DMS_score` labels; `29/217` contain ground-truth `DMS_score` ties capping strict-permutation `ρ < 1.0`, minimum `0.7246` on `ccdb-ecoli-tripathi-2016-n50-b1` with `44` repeated observations across `48` tied variants; overall 217-task Harbor flat Oracle mean = `0.997970558499`, upstream per-seed UniProt-nested macro Oracle mean = `0.998565982126`). Parity experiments are pending maintainer confirmation of the parity plan (agent, model, subset, default `network_mode`, and continuous signed Spearman reward exception).

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Performance | Harbor Performance |
|-------|-------|--------|----------------|--------------|----------------------|--------------------|
| TBD | TBD | Spearman rho | TBD | 217 (or 50 parity subset) | TBD | TBD |

Reproduction steps (run from the adapters repository root; template to be finalized with parity fork and pinned agent/model):

```bash
# 1. Generate Harbor tasks (or --split parity; use --network-mode no-network for cleanroom runs)
uv run --project src/pg-llm pg-llm --output-dir datasets/pg-llm

# 2. Run Harbor evaluation (for Daytona `no-network` cleanroom runs, pass `-e daytona` with `--allow-environment-host <setup_registry_host> --allow-agent-host <provider_host>`, or use a pre-built agent image)
harbor run -p datasets/pg-llm -a <agent> -m "<model>"

# 3. Compute upstream UniProt-nested macro score alongside Harbor flat mean
uv run --project src/pg-llm pg-llm-score jobs/<job_name>
```

See `parity_experiment.json` for structured parity records.

## Notes & Caveats

- **Evaluation-only terms & licenses**: Upstream code is MIT-licensed and the full MIT copyright/permission text + provenance travel in `THIRD_PARTY_LICENSES.md` across the wheel (`.dist-info/licenses/`), generated dataset root, and verifier-only `tests/` directory of each task. Upstream ProteinGym v1.3 assays retain their original terms, and `BENCHMARK_USE_POLICY.md` prohibits using released splits or held-out labels for model training, fine-tuning, RAG, or prompt optimization.
- **Public dataset scope (`217` vs `276`)**: Only the 217 ProteinGym v1.3 substitution assays in `eval-data-v1.2` are publicly released; the 59 temporally held-out assays in the preprint remain access-controlled.
- **Nominal `N=100` exception**: For `NPC1_HUMAN_Erwood_2022_RPE1`, the upstream `N=100` split freezes `63` available candidates across seeds; task prompts and `n_variants` reflect `63` while `task_id` and `nominal_size` preserve `100`.
- **Single-channel prompt adaptation**: `instruction.md` replaces the upstream last-line stdout instruction (`"then on the last line output ONLY the JSON object:"`) with `"then write ONLY the JSON object to /app/answer.json:"` plus a concise `## Constraints` block so there is one unambiguous submission path. Paired parity experiments must record and match this submission instruction consistently.
- **Harbor contract exception (WIP PR — pending maintainer confirmation) — signed reward (`[-1, 1]`) & tied-DMS Oracle ceiling**: Harbor `docs/adapters.mdx` assumes `[0, 1]` rewards and `1.0` across all Oracle tasks, whereas upstream PG-LLM uses signed Spearman rank correlation `ρ ∈ [-1, 1]` (returning `0.0` if ground-truth labels were constant, though `eval-data-v1.2` has zero constant-label splits) and requires strict no-tie permutations. Across the `217` `n50_b1` tasks, `188/217` achieve `1.0` and `29/217` have tied ground-truth `DMS_score` labels whose theoretical strict-permutation ceiling is `< 1.0` (lowest is `0.7245646952474566` on `ccdb-ecoli-tripathi-2016-n50-b1` with `44` repeated observations across `48` tied variants). `/logs/verifier/reward.txt` preserves raw upstream Spearman `ρ` (`0.0` fail-closed for classified missing/invalid submissions), and Oracle acceptance verifies `217/217` completed trials with `0` exceptions, `status == "scored"`, `at_theoretical_max == true`, and `max |reward - max_possible_spearman_rho| < 1e-9` (Harbor flat Oracle mean = `0.997970558499`, upstream UniProt-nested macro mean = `0.998565982126`). Raw `ρ` is also preserved in `/logs/verifier/details.json` (`spearman_rho`), pending explicit Harbor maintainer confirmation of this signed-metric exception for PG-LLM.
- **Aggregation & strict coverage checks**: `pg-llm-score` (`uv run --project src/pg-llm pg-llm-score jobs/<job_name>`) reconciles Harbor v3 job `lock.json`, v2 per-trial `lock.json` (enforcing full Harbor `0.27.0` `JobLock`, `TrialLock`, `EnvironmentConfig`, `VerifierLock`, `RetryConfig`, and `StepResult` constraints identically in pure Python stdlib without external package dependencies, plus Pydantic model validation when `harbor` is installed), `result.json` `JobStats` arithmetic/status-migrated fields (rejecting explicit `null` stat counters), full Harbor `TrialLock._equality_key` and execution `config.json` fields (`install_only`, timeout multipliers, environment, verifier, artifacts, error-policy overrides, extra instructions, skills, and source-trial metadata), and normalized local `task.path` + resolved custom-agent (`module:Class` and `import_path`) and model configs (`env`, `harbor-skip-parsing`), verifies task content digests via Harbor `Packager` parity (rejecting any symlink inside the entire task tree as well as user symlinks along `task_dir` or `--dataset-dir` ancestors, failing closed on `.gitignore`/`.harborignore` if `harbor` is unavailable, and unconditionally requiring `task.toml`, `instruction.md`, `environment/Dockerfile`, `tests/labels.json`, `tests/grade.py`, and `tests/test.sh` among the hashed inputs on both hashing paths; pass `--dataset-dir` if job artifacts were relocated), enforces full-job lock reconciliation with 1-to-1 planned-slot `Counter` matching (`task.type`, `task.path`, `task.digest`, full `TrialLock` equality, and zero missing planned trials or duplicate slot reuse across the job even when selecting a single cohort with `--agent`/`--model`/`--size`) and pre-filter validation across all trials (`StepResult` schemas, primary `verifier_result.rewards["reward"]`, local `task.path`, explicit `verifier_environment_mode == "separate"` in both locks and trial results, zero excluded-trial exceptions, and strict provenance + verifier + answer-replay checks across excluded completed trials), replays saved `artifacts/app/answer.json` (plus `artifacts/manifest.json`) against digest-verified `tests/labels.json` (`|ρ| ≤ max_ρ`), computes two-level UniProt-nested macro per seed before averaging across seeds (mirroring upstream `proteingym-llm/src/analyze.py`), and rejects incomplete jobs/trials (including 0-trial jobs), synthetic `repetition_id`/`attempt_index`/`attempt` keys, missing or non-zero `stats.n_retries` (`status.n_retries` migrated per Harbor `0.27.0`; `n_retries` exposed in diagnostics), `reward_valid=false` or missing primary rewards, exceptions, verifier faults, uneven seed coverage, `--agent-version` filtered diagnostics, or `n_attempts != 1` jobs unless `--allow-incomplete` is passed for diagnostic partial reporting (requiring a validated `job/lock.json` plan and matching reconciled `trial/lock.json` digest to certify any reward and returning `diagnostic_nested_macro_zero_filled = null` without verified provenance for every planned trial; `diagnostic_flat_mean_zero_filled` divides across `n_planned_trials` even when 0 trial directories exist). Verifier crashes in `test.sh` (`set -euo pipefail`) exit non-zero without writing `reward.txt` (`grade.py` classifies `FileNotFoundError` during answer reads as `missing_answer` while propagating unexpected host I/O `OSError`s cleanly, writes `details.json` first, and atomically publishes `reward.txt` last).
- **Parser & artifact size cap**: Matching upstream `parse_ranking`, `grade.py` scans `"ranking": [...]` in linear time (falling back to full text if no array block is closed), accepts explanatory reasoning alongside the ranking, normalizes `M1` to `M01`, ignores extraneous unknown IDs when every expected ID appears once, fails closed on duplicates or incomplete permutations, enforces canonical zero-padded `M01..M999` ID format (`M` followed by integer `1..999` with minimum width 2, so e.g. `M001` is rejected; generated datasets use `M01..M100`) in `tests/labels.json`, and aligns `variants[].dms_score` by ID against `ids` (`spearman` / `_rankdata` sorts ranks in `O(N log N)`). `grade.py` bounds `/app/answer.json` reads at `16 MiB` inside the verifier after Harbor transfers the `/app/answer.json` artifact from the agent container.
- **Prompt length**: Wild-type and mutant sequences are passed in full (`37` to `3,423` amino acids per sequence; generated `instruction.md` ranges from `3,287` to **`176,000` UTF-8 bytes** at default `N=50`, longest on `a0a140d2t1-zikv-sourisseau-2019-n50-b1`, and up to `347,454` UTF-8 bytes when generating `--size 100`), so models should support long inputs.
- **Non-agentic & network isolation**: Upstream PG-LLM forbids external tools, structures, MSAs, and pretrained protein models. Harbor `0.27.0` Docker egress control requires Linux kernel `CONFIG_NFT_FIB_INET` (not enabled in macOS Docker Desktop's LinuxKit kernel), so task generation defaults to `network_mode = "public"` for local Docker portability while supporting `--network-mode no-network` on Daytona, Modal, or Linux Docker hosts with `CONFIG_NFT_FIB_INET`. For installed CLI agents under `no-network`, use either a pre-built image or `--allow-environment-host <setup_registry_host>` for setup dependencies alongside `--allow-agent-host <provider_host>` for rollout inference (`[environment]` setup hosts are dropped before agent execution). In all modes, `instruction.md` forbids external lookups/PLMs, `verifier.environment_mode = "separate"` isolates held-out labels, and the container installs only standard Python 3.11.

## Installation / Prerequisites

- Python 3.11+ and `uv`
- Docker running locally
- Harbor CLI (`uv tool install harbor==0.27.0`)
- Adapter setup:
  ```bash
  cd src/pg-llm
  uv sync
  ```

## Troubleshooting

- **GitHub API rate limit or offline fallback**: Export `GITHUB_TOKEN` or `GH_TOKEN`, or pass `--data-dir` pointing to a pre-downloaded `eval-data-v1.2` directory (set `XDG_CACHE_HOME` to override `~/.cache/harbor-pg-llm`).
- **Checksum or size mismatch**: Remove stale files under `$XDG_CACHE_HOME/harbor-pg-llm/` (`~/.cache/harbor-pg-llm/`) and re-run `uv run pg-llm`.
- **Docker `no-network` unsupported error on macOS**: Harbor's Docker egress-control sidecar requires `CONFIG_NFT_FIB_INET`; use the default `--network-mode public` on macOS Docker Desktop, or run `--network-mode no-network` on Daytona (`-e daytona`), Modal, or Linux Docker with `CONFIG_NFT_FIB_INET`.
- **Docker permission or image pull issues**: Ensure Docker Desktop/daemon is running and pull `python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535`.

## Citation

```bibtex
@article{arora2026pgllm,
  title   = {PG-LLM: Benchmarking General-Purpose Language Models for Protein Variant Ranking},
  author  = {Arora, Rohit Krishan and Chen, Leo Tianlai and Du, Melissa and Marks, Debora S. and Church, George M.},
  year    = {2026},
  journal = {bioRxiv},
  doi     = {10.64898/2026.07.27.741045},
  url     = {https://www.biorxiv.org/content/10.64898/2026.07.27.741045v3}
}
```

## Authors & Contributions

- Adapter author: `Ced3-han` (`2311961062@qq.com`)
- Original benchmark: Rohit Krishan Arora, Leo Tianlai Chen, Melissa Du, Debora S. Marks, George M. Church (`rohitarorayyc/proteingym-llm`)
- Acknowledgement: API inference compute for parity experiments is supported by [2077AI](https://www.2077ai.com/) (https://www.2077ai.com/).
