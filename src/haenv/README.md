# Health Agent Environment (HAEnv) — Harbor Adapter

## Overview

This adapter converts Pack 1 (**M2 Differential Diagnosis**, `N = 100`, English v1.4) of **Health Agent Environment (HAEnv)** into Harbor task directories.

- **Benchmark**: Health Agent Environment (`thetahealth/mirobody-env`, pinned commit `94761fbf793a034fd3a2430e128f6fca6c07dd5b`)
- **Domain**: Longitudinal clinical reasoning and budgeted diagnostic workup
- **Task count**: 100 audited M2 diagnosis cases (`N = 100`, across frames `F0` bridge, `F1` acute triage, `F2` chronic medication adjustment, and `F3` follow-up interpretation), plus a deterministic stratified `N = 30` `--split parity` subset
- **Primary metric**: `S = G * D * (1 + Q) / 2` (continuous reward in `[0.0, 1.0]`):
  - `G` (code-graded): hard safety gates (`hallucinated_clinical_fact`, `unsafe_action`, `med_change_without_clinician`, `missed_emergency_red_flag`, `acted_on_unverified_signal`, or abort/budget exhaustion)
  - `D` (code-graded): core diagnostic correctness (`haenv.reporting.tools.m2_raw_correct.raw_correct`)
  - `Q` (LLM-as-a-Judge): management-plan quality (`management-rubric-v7`, short-circuited when `G * D = 0`)
- **License**: Code MIT; synthetic patient data and question packs CC BY 4.0

## What is HAEnv?

Health Agent Environment (HAEnv) generates synthetic patients with coherent longitudinal records (labs, home and wearable streams, prescriptions, symptom diaries, and clinical notes) up to an index time `T`, derives ground truth deterministically from the underlying world state, and evaluates clinical agents on decision accuracy, safety boundaries, budgeted investigation ordering, and management-plan quality.

- **Repository**: <https://github.com/thetahealth/mirobody-env>
- **Live Demo**: <https://thetahealth.github.io/mirobody-env/>

## Adapter Features

- Deterministically generates and extracts the audited `N = 100` HAEnv M2 diagnosis pack (`--gen deterministic`, zero LLM calls) with English records (`--lang en`, optional `--lang zh`) pinned to full upstream SHA `94761fbf793a034fd3a2430e128f6fca6c07dd5b`.
- Uses a **Docker Compose sidecar architecture** (`main` + `ehr-server`) to enforce strict anti-leakage and budgeted workup isolation:
  - `main` (agent container) contains **no gold data, no raw case files, and no upstream `haenv` benchmark inputs** — only the `haenv-ehr` CLI client.
  - `ehr-server` (isolated sidecar) serves gold-stripped records up to `T` and enforces M2 menu pricing, purchase budget refusals, `ResultGuard`, and query/purchase trace logging.
- Runs upstream `haenv`'s exact grading pipeline (`evaluate._base_row_gated`, `board_score.G`, `m2_raw_correct.raw_correct`, and `management-rubric-v7`) via an authenticated verifier-only sidecar endpoint (`/internal/grade`) whose token and hidden `gold_case.json` are mounted only under `/tests/` at verification time.
- Generates real clinical oracle solutions (`solution/solve.sh`) that query profile/notes, order required key signals within budget via `haenv-ehr`, and emit gold diagnoses plus `management-rubric-v7` gold management plans grounded in obtained findings (`100/100` offline `S = 1.0`, `100/100` live `G = 1, D = 1`, `mean_S = 0.9217` with `deepseek-ai/deepseek-v4.1-flash-1017` judge).
- Includes `src/haenv/parity_agent.py` (`HAEnvParityAgent`, `haenv-parity-agent@1.0.0`), a thin Harbor `BaseAgent` mirroring upstream HAEnv's OpenAI-compatible `query` + budgeted-workup tool loop (`GET /specs` + `POST /tool`) with identical `max_rounds=32`, `max_calls=256`, `max_points=400`, batched `order_test` purchase rounds, and transient HTTP retry logic.
- Supports deterministic stratified parity subsampling (`--split parity`, `N = 30`) balanced across `(frame, join_gold)` strata via Hamilton / Largest Remainder apportionment with SHA-256 tie-breaking (`seed = "haenv-parity-v1"`).
- Supports `--output-dir`, `--limit`, `--overwrite`, `--task-ids`, `--split`, `--n`, `--seed`, `--lang`, `--pack-dir`, and `--upstream-repo`.

## Generated Task Structure

```text
datasets/haenv/
└── haenv-m2-<case-id>/
    ├── task.toml
    ├── instruction.md
    ├── environment/
    │   ├── Dockerfile                 # Agent container (haenv-ehr CLI only)
    │   ├── docker-compose.yaml        # Wires main + ehr-server sidecar
    │   ├── haenv_ehr.py               # Stdlib EHR query & order CLI
    │   └── ehr/
    │       ├── Dockerfile             # Isolated ehr-server with pinned haenv
    │       ├── ehr_server.py          # Gold-stripped ToolSession + /internal/grade
    │       └── case_public.json       # Leak-checked <=T payload + precomputed menu results
    ├── solution/
    │   └── solve.sh                   # Real clinical oracle (profile/notes + key orders + gold JSON)
    └── tests/
        ├── test.sh                    # Entry point writing float reward to /logs/verifier/reward.txt
        ├── verify.py                  # Calls /internal/grade with X-Verifier-Token
        ├── gold_case.json             # Hidden verifier payload + open_management snapshot
        └── verify_token.txt           # Per-task verifier auth token
```

## Run Evaluation / Harness

### Running from the Harbor Registry (Post-Publish)

```bash
# Run full dataset from the Harbor registry (available after Step 8 publishing)
harbor run -d thetahealth/haenv -a <agent> -m <model>

# Run the N=30 parity split from the Harbor registry
harbor run -d thetahealth/haenv@parity -a <agent> -m <model>

# Run a single task from the registry
harbor run -t thetahealth/haenv-m2-jd-02v5 -a <agent> -m <model>
```

### Running Locally (Development & Oracle Verification)

```bash
# Oracle verification using the reference config
harbor run -c src/haenv/run_haenv.yaml

# Run full local dataset with a specific agent/model
harbor run -p datasets/haenv -a <agent> -m <model>

# Single-task trial
harbor trial start -p datasets/haenv/haenv-m2-jd-02v5 -a oracle
```

## Usage: Create Task Directories

Run from `src/haenv/`:

```bash
cd src/haenv
uv sync

# Generate the full audited N=100 M2 diagnosis dataset
uv run haenv --output-dir ../../datasets/haenv --n 100 --overwrite

# Generate the deterministic stratified N=30 parity subset from the same audited pack
uv run haenv --pack-dir /path/to/haenv-m2-pack-100 --split parity --output-dir ../../datasets/haenv-parity --overwrite

# Generate a small subset for quick testing
uv run haenv --output-dir ../../datasets/haenv --limit 5 --overwrite
```

Supported flags:

- `--output-dir`: Destination directory for generated Harbor tasks
- `--limit`: Generate only the first `N` tasks (applied after stratification when `--split parity` is used)
- `--overwrite`: Replace existing task directories
- `--task-ids`: Generate only specified case IDs or Harbor task IDs
- `--split`: Split selector (`diagnosis` for full `N = 100`, or `parity` for deterministic stratified `N = 30`)
- `--n`: Pack size when building via upstream `make_pack.py` (default `100`)
- `--seed`: Optional pack seed (defaults to upstream public seed)
- `--lang`: Record/prompt language (`en` default, or `zh`)
- `--pack-dir`: Reuse an existing audited pack output directory
- `--upstream-repo`: Reuse a local clone of `thetahealth/mirobody-env`

## Comparison with Original Benchmark (Parity)

Symmetric `k = 3` parity experiments were conducted on the deterministic stratified `N = 30` parity subset (`30%` of the audited `N = 100` English v1.4 M2 pack) using identical pinned settings on both sides (`thetahealth/mirobody-env@94761fb`, solver & live `management-rubric-v7` judge `deepseek-ai/deepseek-v4.1-flash-1017` via `https://api.aiand.com/v1`, `temperature = 0.0`, `max_tokens = 16384`, `max_rounds = 32`, `max_calls = 256`, `HAENV_OFFLINE_JUDGE = 0`, Harbor CLI `v0.27.0`).

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|
| haenv-parity-agent@1.0.0 | deepseek-ai/deepseek-v4.1-flash-1017 | S = G * D * (1 + Q) / 2 | 3 | 30 (30% stratified) | 0.6944 ± 0.0255 | 0.6685 ± 0.0451 |

- **Per-run scores**:
  - Original: `[0.644444, 0.727778, 0.711111]` (0 exceptions across 90 trials)
  - Harbor: `[0.594444, 0.661111, 0.750000]` (0 exceptions across 90 trials)
  - **Overlap check**: PASS (`max(original) = 0.727778 >= min(harbor) = 0.594444` and `max(harbor) = 0.750000 >= min(original) = 0.644444`).
- **Stratified `N = 30` subset selection (`--split parity`)**: Selected deterministically from the audited `N = 100` batch using Hamilton / Largest Remainder apportionment across all `(frame, join_gold)` strata with SHA-256 hash ordering (`seed = "haenv-parity-v1"`), preserving exact proportional balance across frames (`F0: 1/4, F1: 11/39, F2: 12/38, F3: 6/19`) and join types (`unified: 18/58, comorbidity: 7/22, independent: 5/20`). The exact 30 case IDs are listed in `src/haenv/parity_case_ids.txt`.
- **Links**:
  - Original benchmark repo: <https://github.com/thetahealth/mirobody-env>
  - Adapter PR: <https://github.com/harbor-index/adapters/pull/42>
  - Dataset PR: <https://github.com/harbor-framework/harbor-datasets/pull/312>
  - HuggingFace parity PR: <https://huggingface.co/datasets/harborframework/parity-experiments/discussions/316> (full verbatim `adapters/haenv/` mirror hosted at <https://huggingface.co/datasets/jessica23huang/haenv-parity-experiments> @ `1b25b07` while `harborframework/parity-experiments` is blocked by the Hugging Face 1,000,000-file repository limit, see <https://github.com/harbor-framework/adapters/issues/129>)

Reproduction commands (both sides share the exact same `--pack-dir` audited batch and env vars `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `HAENV_JUDGE_BASE_URL`, `HAENV_JUDGE_MODEL`, `HAENV_OFFLINE_JUDGE=0`):

```bash
# 1. Generate both diagnosis (N=100) and stratified parity (N=30) datasets from the same audited pack
cd src/haenv
uv sync
uv run haenv --pack-dir /path/to/haenv-m2-pack-100 --upstream-repo /path/to/mirobody-env --split diagnosis --output-dir ../../datasets/haenv --overwrite
uv run haenv --pack-dir /path/to/haenv-m2-pack-100 --upstream-repo /path/to/mirobody-env --split parity --output-dir ../../datasets/haenv-parity --overwrite

# 2. Harbor side — live-judge oracle (N=100)
cd ../..
harbor run -c src/haenv/run_haenv.yaml -y

# 3. Harbor side — parity run (N=30, repeat for trial 1..3)
PYTHONPATH="$PWD/src/haenv/src" harbor run \
  -p datasets/haenv-parity \
  -a haenv.parity_agent:HAEnvParityAgent \
  -m deepseek-ai/deepseek-v4.1-flash-1017 \
  -n 1 -y --skip-credential-checks \
  --job-name harbor-parity-trial1

# 4. Original HAEnv side — parity run (N=30, repeat for trial 1..3)
uv run --directory /path/to/mirobody-env python \
  src/haenv/run_original_parity.py \
  --pack-dir /path/to/haenv-m2-pack-100 \
  --split parity \
  --model deepseek-ai/deepseek-v4.1-flash-1017 \
  --workers 2 \
  --output-dir jobs/original-parity-trial1
```

## Notes & Caveats

- **Synthetic data only**: All patients and longitudinal clinical records are synthetic and strictly for benchmark evaluation, not clinical advice.
- **Upstream clinical review status**: Per upstream `thetahealth/mirobody-env`, parts of the clinical registries and `management-rubric-v7` remain provisional (`review: pending`).
- **Upstream `94761fb` bug discovered and handled**: In pinned upstream `thetahealth/mirobody-env@94761fb`, `run_gated` removes purchased menu items from `gated_context["menu"]` before round `r + 1`, which caused `solve_gated_round`'s `_purchase_result` -> `canonical_target(target, gc)` to fail resolving hashed `test-*` IDs for non-ASCII test names and silently return `"No result"` (upstream subsequently fixed this in release `1.6.0`, commit `8dca0a7`). Harbor's `ehr_server.py` already resolved targets against the full round-1 menu (`self.all_menu`), and `src/haenv/run_original_parity.py` applies the equivalent canonical-target lookup against the round-1 menu on the original side so both sides return identical purchased lab results.
- **Upstream pin-drift & memory safety in `adapter.py`**: `UPSTREAM_PIN` is pinned to the full 40-character SHA `94761fbf793a034fd3a2430e128f6fca6c07dd5b` (with `check=True`) so shallow clones never drift if upstream advances `main`, and `make_pack.py` is invoked with `--gen-workers 2 --jobs 2` because upstream's default `invariants_step(jobs=32)` can exhaust memory on 8 GB hosts.
- **LLM-as-a-Judge (`Q`) & oracle results**: Stage 2 (`Q`) runs only when `G * D == 1` (upstream Spec §9 short-circuit). When `HAENV_OFFLINE_JUDGE=1` or no judge API key is supplied, the verifier runs upstream's deterministic offline rubric plumbing (`stub_reply`, achieving `100/100 S = 1.0000`). For live judging (`HAENV_OFFLINE_JUDGE=0`) on non-OpenRouter OpenAI-compatible endpoints where strict `json_schema` disables hidden reasoning tokens, both `ehr_server.py` and `run_original_parity.py` set `temperature = 0.0` and order verdict schema keys `reason -> evidence -> label` (semantically identical JSON parsed by upstream `parse_reply`) to prevent negative-polarity `safety` label flips. Across the full `N = 100` live-judge oracle (`deepseek-ai/deepseek-v4.1-flash-1017`), all `100/100` trials completed with 0 exceptions, `100/100` `judge_mode = live_llm`, `100/100` `G = 1`, `100/100` `D = 1`, `0/100` safety hits, and `mean_S = 0.9217` (`68/100` `Q = 1.0`, `min_S = 0.5000`; sub-1.0 `Q` cases reflect flash-judge strictness on `v6_controls` background citations not explicitly queried via `get_labs`/`get_meds` and occasional 1-char quote-verification demotions).
- **Upstream pin**: Pinned to `thetahealth/mirobody-env@94761fb` (v1.4.1.dev0, English default). Scores on v1.4 English packs are not directly comparable to the historical v1.3.1 Chinese leaderboard.
- **Anti-leakage boundary**: Upstream `haenv` wheels bundle `benchmarks/inputs/*.job.yaml`; therefore `haenv` is never installed in the `main` agent container, and `benchmarks/inputs/` is deleted from the isolated `ehr-server` image during build.

## Installation / Prerequisites

- Python 3.12+ and `uv`
- Docker with Docker Compose support
- Harbor CLI (`uv tool install harbor`, verified with `harbor 0.27.0`)

```bash
cd src/haenv
uv sync
```

## Troubleshooting

- **Sidecar healthcheck timeout**: Inspect `docker compose logs ehr-server`; first-time builds install `haenv` from GitHub once and cache the base layers for all subsequent tasks.
- **Offline vs live judge**: Set `HAENV_OFFLINE_JUDGE=1` for zero-cost local oracle checks, or export `OPENROUTER_API_KEY` / `OPENAI_API_KEY` (plus `HAENV_JUDGE_BASE_URL` and `HAENV_JUDGE_MODEL` for custom OpenAI-compatible endpoints) with `HAENV_OFFLINE_JUDGE=0` to evaluate `Q` with a live judge model.
- **Memory/concurrency on 8 GB hosts**: Run Harbor with `-n 1` (`n_concurrent_trials: 1`), avoid running `make_pack.py` simultaneously with Docker, and reuse an audited pack via `--pack-dir /path/to/haenv-m2-pack-100`.
- **Slow initial `make_pack.py` build**: Building the audited `N = 100` M2 pool takes ~15–30 minutes the first time; reuse the directory via `--pack-dir /path/to/haenv-m2-pack-100`.

## Citation

```bibtex
@misc{haenv2026,
  title  = {Health Agent Environment (HAEnv)},
  author = {{Theta Health}},
  year   = {2026},
  howpublished = {\url{https://github.com/thetahealth/mirobody-env}}
}
```

## Authors & Contributions

- Adapter built by Jieshu Huang (`jieshuh2@illinois.edu`).
- Original benchmark by Theta Health (<https://github.com/thetahealth/mirobody-env>).
