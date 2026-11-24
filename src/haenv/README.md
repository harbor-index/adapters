# Health Agent Environment (HAEnv) — Harbor Adapter

## Overview

This adapter converts Pack 1 (**M2 Differential Diagnosis**, `N = 100`, English v1.4) of **Health Agent Environment (HAEnv)** into Harbor task directories.

- **Benchmark**: Health Agent Environment (`thetahealth/mirobody-env`, pinned commit `94761fb`)
- **Domain**: Longitudinal clinical reasoning and budgeted diagnostic workup
- **Task count**: 100 audited M2 diagnosis cases (`N = 100`, across frames `F0` bridge, `F1` acute triage, `F2` chronic medication adjustment, and `F3` follow-up interpretation)
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

- Deterministically generates and extracts the audited `N = 100` HAEnv M2 diagnosis pack (`--gen deterministic`, zero LLM calls) with English records (`--lang en`, optional `--lang zh`).
- Uses a **Docker Compose sidecar architecture** (`main` + `ehr-server`) to enforce strict anti-leakage and budgeted workup isolation:
  - `main` (agent container) contains **no gold data, no raw case files, and no upstream `haenv` benchmark inputs** — only the `haenv-ehr` CLI client.
  - `ehr-server` (isolated sidecar) serves gold-stripped records up to `T` and enforces M2 menu pricing, purchase budget refusals, `ResultGuard`, and query/purchase trace logging.
- Runs upstream `haenv`'s exact grading pipeline (`evaluate._base_row_gated`, `board_score.G`, `m2_raw_correct.raw_correct`, and `management-rubric-v7`) via an authenticated verifier-only sidecar endpoint (`/internal/grade`) whose token and hidden `gold_case.json` are mounted only under `/tests/` at verification time.
- Generates real clinical oracle solutions (`solution/solve.sh`) that order required key signals within budget via `haenv-ehr` and emit gold diagnoses plus `management-rubric-v7` gold management plans (`S = 1.0`).
- Includes `src/haenv/parity_agent.py` (`HAEnvParityAgent`), a thin Harbor `BaseAgent` mirroring upstream HAEnv's OpenAI-compatible `query` + budgeted-workup tool loop (`GET /specs` + `POST /tool`) for Step 5 parity experiments.
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
    │   └── solve.sh                   # Real clinical oracle (key orders + gold JSON)
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

# Generate a small subset for quick testing
uv run haenv --output-dir ../../datasets/haenv --limit 5 --overwrite

# Reuse an already-built audited make_pack directory
uv run haenv --output-dir ../../datasets/haenv --pack-dir /path/to/haenv-m2-100 --overwrite
```

Supported flags:

- `--output-dir`: Destination directory for generated Harbor tasks
- `--limit`: Generate only the first `N` tasks
- `--overwrite`: Replace existing task directories
- `--task-ids`: Generate only specified case IDs or Harbor task IDs
- `--split`: Split selector (`diagnosis` or `parity`)
- `--n`: Pack size when building via upstream `make_pack.py` (default `100`)
- `--seed`: Optional pack seed (defaults to upstream public seed)
- `--lang`: Record/prompt language (`en` default, or `zh`)
- `--pack-dir`: Reuse an existing audited pack output directory
- `--upstream-repo`: Reuse a local clone of `thetahealth/mirobody-env`

## Comparison with Original Benchmark (Parity)

Parity experiments are pending Step 4 Harbor-team coordination and API key provisioning (both solver and `management-rubric-v7` `Q` judge).

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|
| Pending | Pending | S = G*D*(1+Q)/2 | 0 | 100 (100%) | Pending | Pending |

Reproduction commands (to be updated with exact pinned versions after Step 4 sign-off):

```bash
# Harbor side (oracle verification)
harbor run -c src/haenv/run_haenv.yaml

# Upstream HAEnv side (deterministic pack build)
uv run --with scikit-learn --with joblib python tools/generation/make_pack.py \
  --pack m2 --n 100 --out /tmp/haenv-m2-100
```

## Notes & Caveats

- **Synthetic data only**: All patients and longitudinal clinical records are synthetic and strictly for benchmark evaluation, not clinical advice.
- **Upstream clinical review status**: Per upstream `thetahealth/mirobody-env`, parts of the clinical registries and `management-rubric-v7` remain provisional (`review: pending`).
- **LLM-as-a-Judge (`Q`)**: Stage 2 (`Q`) runs only when `G * D == 1` (upstream Spec §9 short-circuit). When `HAENV_OFFLINE_JUDGE=1` or no judge API key is supplied, the verifier runs upstream's deterministic offline rubric plumbing (`stub_reply`) for zero-cost oracle testing; live parity and benchmark runs require `OPENROUTER_API_KEY` (or `OPENAI_API_KEY` / `GOOGLE_API_KEY`) in `[verifier.env]`.
- **Upstream pin**: Pinned to `thetahealth/mirobody-env@94761fb` (v1.4.1.dev0, English default). Scores on v1.4 English packs are not directly comparable to the historical v1.3.1 Chinese leaderboard.
- **Anti-leakage boundary**: Upstream `haenv` wheels bundle `benchmarks/inputs/*.job.yaml`; therefore `haenv` is never installed in the `main` agent container, and `benchmarks/inputs/` is deleted from the isolated `ehr-server` image during build.

## Installation / Prerequisites

- Python 3.12+ and `uv`
- Docker with Docker Compose support
- Harbor CLI (`uv tool install harbor`)

```bash
cd src/haenv
uv sync
```

## Troubleshooting

- **Sidecar healthcheck timeout**: Inspect `docker compose logs ehr-server`; first-time builds install `haenv` from GitHub once and cache the base layers for all subsequent tasks.
- **Offline vs live judge**: Set `HAENV_OFFLINE_JUDGE=1` for zero-cost local oracle checks, or export `OPENROUTER_API_KEY` (and optional `HAENV_JUDGE_MODEL`) to evaluate `Q` with a live judge model.
- **Slow initial `make_pack.py` build**: Building the audited `N = 100` M2 pool takes ~5–10 minutes the first time; reuse the directory via `--pack-dir /path/to/haenv-m2-100`.

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
