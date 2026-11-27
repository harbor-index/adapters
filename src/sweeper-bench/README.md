# SWEeper-Bench → Harbor Adapter

> **Status.** Oracle: 198/198 adapted tasks reach reward 1.0 (two upstream
> tasks excluded, see below). NOP matches the upstream `baseline` phase on the
> pilot (0/3 on both sides). Parity (10-task cohort, three runs per side,
> Gemini CLI + `gemini-3.8-flash`): original 20.00 ± 5.77, Harbor 23.33 ± 6.67
> solved rate, run ranges overlap.

## Overview

[SWEeper-Bench](https://github.com/zlab-princeton/sweeper-bench) asks a coding
agent to find and fix a user-facing bug in a real open-source web application
without being told what the bug is. Each of the 200 tasks comes from a merged
pull request in a different app (PHP, Go, TypeScript/JavaScript, Python, Rust,
and others; seven domains). The agent gets the buggy checkout, a running copy of
the app and a one-sentence scoped request such as *"Find and fix issues in
AbanteCart's storefront account profile editor."* An agentic browser verifier
then applies the agent's patch to a fresh copy of the app and runs two hidden
behaviour tests.

- **Size:** 200 upstream tasks (`data/test.jsonl`, one split); the adapter
  produces **198** by default. `sweeper-045` and `sweeper-150` are excluded
  because the upstream reference fix fails their target workflow on the original
  runner as well (checked with the upstream `reference` phase); they can still
  be generated with `--task-ids`. Task names are
  `zlab-princeton/sweeper-bench__<case_id>`.
- **Pilot:** `sweeper-001`, `sweeper-073`, `sweeper-102` (`--split pilot`).
- **Provenance:** [paper](https://arxiv.org/abs/2610.14096) ·
  [runner](https://github.com/zlab-princeton/sweeper-bench) (MIT) ·
  [dataset](https://huggingface.co/datasets/zlab-princeton/SWEeper-Bench)
  (CC BY 4.0). Every benchmark app keeps its own license; the adapter does not
  redistribute app code, it clones the public repository at build time and uses
  the upstream-published images.
- **Main adaptation choices:** the agent runs inside the upstream *prediction*
  product image instead of a separate agent container; the verifier runs in a
  Harbor *separate verifier environment* built from the upstream *evaluation*
  product image plus the upstream `evaluation-codex` verifier image. Details in
  [Notes & Caveats](#notes--caveats).

## What is SWEeper-Bench?

SWEeper-Bench (Yao, Chen, Kang, Narasimhan, Liu; Princeton, 2026) measures
whether coding agents can *discover* bugs in interactive software. The bugs only
show up after a sequence of normal user actions, so the agent has to run the app
and use it. The best of 15 frontier agents solves 59.0% of the tasks; given the
bug description, agents solve about 96%.

Scoring: for each task the verifier runs one `target` workflow (the bug is fixed
and the feature works) and one `preservation` workflow (nearby behaviour still
works). Each returns `pass`, `fail` or `uncertain`. **A task is solved only if
both pass.** The upstream default verifier is the Codex CLI driving Playwright
with `gpt-5.6-luna` at `xhigh` effort.

## Adapter Features

- **Upstream prompt, unchanged.** `instruction.md` is the exact text the
  upstream runner sends at its default settings (`prediction.level = "scoped"`,
  `prediction.prompt_templates = "browser"`, unlimited time budget), produced by
  a port of upstream `render_prompt()`. For the three pilot tasks it was checked
  byte-for-byte against the upstream function.
- **Upstream images.** Agent: `ghcr.io/evigbyen/sweeper-NNN-prediction:alpha`.
  Verifier: `ghcr.io/evigbyen/sweeper-NNN:alpha` (contains evaluation seed data
  the agent must not see) and `ghcr.io/evigbyen/evaluation-codex:alpha`.
- **Clean workspace.** The buggy commit is fetched shallowly at build time and
  flattened exactly like upstream `clone_workspace()`: one commit, no remote, no
  refs, so the reference fix is not reachable from the agent's checkout.
- **Running product.** The container entrypoint starts the product the way
  upstream `product.sh` does (`HOST=0.0.0.0 PORT=3000 product-yarn start`, served
  at `127.0.0.1:13200`); a Harbor healthcheck waits for the same readiness rule
  (any non-5xx answer, up to 600 s). `/workspace/task-shell` and the `product-*`
  wrappers named in the prompt exist. Node 20.11.1, the Playwright 1.63.0 CLI and
  Chromium are installed as in the upstream agent images.
- **Upstream egress policy.** The agent phase uses a Harbor allowlist of model
  provider hosts, like upstream `runtime/allowlist.txt`.
- **Patch collection.** A `[[verifier.collect]]` hook writes the agent's change
  with the upstream rules (tracked edits plus new non-empty files up to 1 MB that
  are not runtime junk, `git diff --binary`).
- **Upstream verifier, unchanged.** The separate verifier environment resets a
  clean checkout, applies the patch, starts a fresh product, and runs the
  pinned upstream `browser.py`, `parse_results.py` and Codex adapter in the
  `evaluation-codex` sidecar. Workflow-level retries (invalid JSON / errors) and
  phase-level retries (verifier `uncertain`, up to three attempts) follow
  upstream `evaluate_case()`.
- **Reward.** `1` when both `target` and `preservation` pass, else `0`. The full
  verdict (per-category result, counts, ids) is written to
  `/logs/verifier/sweeper-verdict.json`, and all verifier trajectories are kept
  under `/logs/verifier/sweeper/`.
- **Oracle.** `solution/reference.patch` is `git diff --binary
  base_commit reference_commit` from the app repository.
- **No custom agents** were added.

## Generated Task Structure

```
sweeper-bench/
├── sweeper-NNN/
│   ├── task.toml                  # agent allowlist, healthcheck, collect hook, separate verifier
│   ├── instruction.md             # upstream scoped + browser prompt
│   ├── environment/
│   │   ├── Dockerfile             # FROM sweeper-NNN-prediction; checkout; Playwright
│   │   └── sweeper/               # checkout.sh, agent-entrypoint.sh, task-shell.sh, collect-patch.sh
│   ├── solution/
│   │   ├── solve.sh               # git apply reference.patch
│   │   └── reference.patch
│   └── tests/                     # separate verifier environment (never visible to the agent)
│       ├── Dockerfile             # FROM sweeper-NNN (evaluation product); clean checkout; test.sh
│       ├── docker-compose.yaml    # + browser-verifier sidecar sharing main's network namespace
│       ├── verifier.Dockerfile    # FROM evaluation-codex + pinned upstream runtime files
│       ├── test.sh                # reset → apply → start product → verify → reward
│       ├── workflows.yaml         # hidden target / preservation workflows
│       ├── runtime/               # upstream browser.py, parse_results.py, harnesses/evaluation (MIT)
│       └── sweeper/               # checkout.sh, verifier_server.py (port forward + command channel)
```

Adapter code:

```
src/sweeper-bench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml / uv.lock
├── run_sweeper-bench.yaml
├── scripts/run_upstream_local.py  # original side without Modal (see Parity)
└── src/sweeper_bench/
    ├── adapter.py  main.py
    ├── upstream_prompts/          # upstream prompt templates (pinned)
    └── task-template/
```

## Run Evaluation / Harness

The dataset is not registered yet; use locally generated tasks.

```bash
# from the adapters repository root, after generating datasets/sweeper-bench
harbor run -c src/sweeper-bench/run_sweeper-bench.yaml            # oracle (default)
harbor run -p datasets/sweeper-bench -a claude-code -m anthropic/<model>
harbor trial start -p datasets/sweeper-bench/sweeper-001 -a oracle
```

The verifier calls a model, so every run (including oracle and NOP) needs
verifier credentials:

```bash
# OpenAI API key (upstream default: Codex + gpt-5.6-luna, effort xhigh)
export OPENAI_API_KEY=sk-...
# or a Codex/ChatGPT subscription login
export SWEEPER_VERIFIER_AUTH=subscription
export CODEX_AUTH_JSON="$(cat ~/.codex/auth.json)"
```

Other verifier settings (`[verifier.env]`): `SWEEPER_VERIFIER_MODEL`,
`SWEEPER_VERIFIER_EFFORT`, `SWEEPER_VERIFIER_HARNESS`, and
`SWEEPER_EVALUATE_EMPTY_PATCH=1` to verify an empty diff as the upstream
`baseline` phase instead of scoring it 0 without a verifier call (used for the
NOP check below).

## Usage: Create Task Directories

```bash
cd src/sweeper-bench
uv sync
uv run sweeper-bench --output-dir ../../datasets/sweeper-bench            # all 200 tasks
uv run sweeper-bench --output-dir /tmp/sweeper --split pilot              # the three pilot tasks
uv run sweeper-bench --output-dir /tmp/sweeper --task-ids sweeper-001 sweeper-073
```

Flags: `--output-dir`, `--task-ids`, `--limit`, `--overwrite`, `--split`,
`--dataset-file` (local `test.jsonl`), `--dataset-revision`,
`--image-registry`, `--image-tag`, `--cache-dir`. Generation needs `git` and
network access to the app repositories (for the reference patches); it does
not need Docker.

## Comparison with Original Benchmark (Parity)

This section reports the parity experiment, the full oracle run, and the
pilot that compared Harbor with the upstream runner phase by phase.

### Parity: Gemini CLI + gemini-3.8-flash, 10 tasks × 3 runs

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|
| gemini-cli@0.60.0 | gemini-3.8-flash (thinking `HIGH`) | solved rate (%) | 3 | 10 (5% of 198) | 20.00 ± 5.77 | 23.33 ± 6.67 |

Per run: original 10 / 20 / 30, Harbor 30 / 10 / 30. The run ranges overlap;
the mean squared difference of paired per-task solve rates is 0.056. SEM is
the sample standard error over the three runs.

| Task | Original (r1 r2 r3) | Harbor (r1 r2 r3) |
|---|---|---|
| `sweeper-001` | 0 0 0 | 0 0 0 |
| `sweeper-008` | 1 1 1 | 1 1 1 |
| `sweeper-019` | 0 0 0 | 0 0 0 |
| `sweeper-058` | 0 0 0 | 0 0 0 |
| `sweeper-073` | 0 0 0 | 1 0 1 |
| `sweeper-075` | 0 0 0 | 0 0 0 |
| `sweeper-097` | 0 0 0 | 0 0 0 |
| `sweeper-157` | 0 0 1 | 1 0 0 |
| `sweeper-179` | 0 1 1 | 0 0 1 |
| `sweeper-180` | 0 0 0 | 0 0 0 |

Setup, identical on both sides unless noted:

- **Cohort.** `--split parity`: `sweeper-001, -008, -019, -058, -073, -075,
  -097, -157, -179, -180` (fixed ids, mixed languages).
- **Agent.** Gemini CLI 0.60.0 (the version in upstream's
  `prediction-gemini-cli:alpha` image), `gemini-3.8-flash`, thinking level
  `HIGH` (`--pred-effort high` upstream, `--ak reasoning_effort=high` in
  Harbor), upstream `scoped` + `browser` prompt, unlimited time budget, 8 h agent
  timeout. Model calls go to Vertex AI (`global`) on both sides.
- **Verifier.** Upstream default: Codex from `evaluation-codex:alpha`,
  `gpt-5.6-luna`, effort `xhigh`, ChatGPT subscription login.
- **Original side.** Runner fork
  [`Chenngzz/sweeper-bench@0437080`](https://github.com/Chenngzz/sweeper-bench/tree/harbor-parity)
  (upstream `96f86f2` plus the parity changes listed below; the raw logs
  record the same tree under its earlier commit id `653f038`), run with
  `scripts/run_upstream_local.py` (privileged Docker-in-Docker per VM instead
  of Modal; one VM per case and stage, as upstream).
- **Harbor side.** Harbor 0.27.0, Docker environment, tasks from this adapter.
- **When / where.** 2026-11-26, a3u amd64 hosts, all 60 trials in parallel.
  Agent phase median 12 min (Harbor) and 15 min (original); verifier median
  5 min. No verifier `uncertain` cells and no infrastructure retries in the
  scored results.

Parity-only changes on top of upstream (none of them changes the benchmark):

- The runner fork adds `provider = "vertex"` to the Gemini CLI harness (same
  CLI and settings, Application Default Credentials instead of an API key) and
  allows `aiplatform.googleapis.com` / `oauth2.googleapis.com` in the
  prediction egress allowlist. It also sets `GEMINI_CLI_TRUST_WORKSPACE=true`:
  Gemini CLI 0.60 otherwise exits immediately in headless mode in an untrusted
  folder, so upstream's `gemini-cli` harness cannot run at all without it
  (Harbor's Gemini CLI agent sets the same variable).
- Harbor side: `-m vertex_ai/gemini-3.8-flash`, the credential file mounted
  read-only into the agent container (`--mounts`), and
  `--allow-agent-host aiplatform.googleapis.com --allow-agent-host
  oauth2.googleapis.com`.
- Gemini CLI settings differ slightly between the two harnesses: upstream's
  model alias extends `chat-base-3`; Harbor's alias sets `includeThoughts` and
  excludes the web-search and web-fetch tools (the egress allowlist blocks
  those hosts on both sides anyway).
- Reruns before scoring, all for infrastructure reasons: an earlier attempt with
  Gemini API keys was discarded entirely because the shared key quota returned
  429 to most agents; on Harbor, three trials that failed to start (Docker ran
  out of network address pools) were started again; on the original side, one
  trial (r2 `sweeper-179`) whose agent blocked for over 30 minutes on a
  foreground server command was killed and run again from scratch.

```bash
# original side (one run; parity fork checked out in upstream-runner/)
export GHCR_TOKEN=... GHCR_USER=... CODEX_AUTH_FILE=~/.codex/auth.json
export GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=global GEMINI_VERTEX_ADC_FILE=<adc.json>
python src/sweeper-bench/scripts/run_upstream_local.py --runner upstream-runner \
  --dataset-file test.jsonl --cases sweeper-001 sweeper-008 ... --mode run --auth subscription \
  --pred-harness gemini-cli --pred-provider vertex --pred-model gemini-3.8-flash --pred-effort high \
  --concurrency 10 --out runs/parity/r1
# Harbor side (one run)
uv run sweeper-bench --output-dir /tmp/sweeper-parity --split parity
export SWEEPER_VERIFIER_AUTH=subscription CODEX_AUTH_JSON="$(cat ~/.codex/auth.json)"
export GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=global GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-adc.json
harbor run -p /tmp/sweeper-parity -a gemini-cli -m vertex_ai/gemini-3.8-flash \
  --ak version=0.60.0 --ak reasoning_effort=high \
  --mounts '[{"type":"bind","source":"<adc.json>","target":"/run/secrets/gcp-adc.json","read_only":true}]' \
  --allow-agent-host aiplatform.googleapis.com --allow-agent-host oauth2.googleapis.com -n 10 -y
# summary
python src/sweeper-bench/scripts/summarize_parity.py --original runs/parity/r1 runs/parity/r2 runs/parity/r3 \
  --harbor <run-1 job dirs> <run-2 job dirs> <run-3 job dirs> --task-ids-file parity_ids.json --out parity_summary.json
```

### Oracle: all tasks

Adapter commit `65d790a`, Harbor 0.27.0, verifier Codex + `gpt-5.6-luna`
(`xhigh`) through the parity API endpoint, Docker environment:

| Tasks | Reward 1.0 | Notes |
|---|---|---|
| 198 adapted | **198 / 198 (100%)** | `sweeper-002` and `sweeper-109` re-run once after the product hit the shared host's inotify limit (`EMFILE`); `sweeper-172` re-run once after a verifier `fail` (it passes in every other run); 7 tasks re-launched because Harbor's startup probe timed out before their trials began |
| 2 excluded | 0 / 2 | `sweeper-045`, `sweeper-150`: the upstream runner's own `reference` phase also fails the target workflow |

The verifier is an LLM agent, so a correct patch can occasionally be judged
`fail`; across the development runs the oracle failed this way on a handful of
tasks that passed on every other run.

### Pilot: same verifier, both sides

Each pilot task was run four ways with the same verifier (Codex CLI from the
`evaluation-codex:alpha` image, `gpt-5.6-luna`, effort `xhigh`, ChatGPT
subscription login):

- **Upstream `baseline`**: unpatched buggy commit, upstream runner.
- **Upstream `reference`**: upstream fix applied, upstream runner.
- **Harbor NOP**: `-a nop` with `SWEEPER_EVALUATE_EMPTY_PATCH=1`.
- **Harbor Oracle**: `-a oracle`.

| Case | App / why chosen | Upstream baseline (target / preservation) | Upstream reference | Harbor NOP | Harbor Oracle |
|---|---|---|---|---|---|
| `sweeper-001` | AbanteCart (PHP): PHP + MariaDB storefront; target workflow uploads a file | fail / pass → not solved | pass / pass → solved | fail / pass → reward 0 | pass / pass → reward 1 |
| `sweeper-073` | Gotify (Go + React): Go server with a React UI and an OIDC (Dex) sidecar process | fail / pass → not solved | pass / pass → solved | fail / pass → reward 0 | pass / pass → reward 1 |
| `sweeper-102` | linkding (Python/Django): Django; bookmark import with URL normalisation | fail / pass → not solved | pass / pass → solved | fail / pass → reward 0 | pass / pass → reward 1 |

Totals: Harbor Oracle 3/3 (reward 1.0, 0 exceptions); Harbor NOP 0/3 solved (reward 0.0, 0 exceptions); upstream reference 3/3 solved; upstream baseline 0/3 solved. Every verdict was reached on the first phase attempt (no `uncertain`, no infrastructure retries). Runs on 2026-11-24 on one amd64 host (a3u-0), Harbor 0.27.0 Docker environment. Wall clock: upstream 284–748 s per case (both phases); Harbor 17–18 min per job of three trials including image builds.

Image digests (tag `alpha`, pulled 2026-11-24): `sweeper-001` `7830f447…`, `sweeper-001-prediction` `6e6cc2c5…`, `sweeper-073` `d3f84e67…`, `sweeper-073-prediction` `a9e0c84b…`, `sweeper-102` `c617aa40…`, `sweeper-102-prediction` `570bf458…`, `evaluation-codex` `bf80bf95…`.

Original side: the unmodified upstream `install.sh` + `worker.py` (runner
`96f86f2`) in the upstream VM image, run with
`scripts/run_upstream_local.py` as a privileged Docker-in-Docker container
instead of a Modal sandbox. Harbor side: `harbor run -p <pilot tasks> -a
oracle|nop` with Harbor 0.27.0, Docker environment.

```bash
# original side
export GHCR_TOKEN=... GHCR_USER=... CODEX_AUTH_FILE=~/.codex/auth.json   # or OPENAI_API_KEY with --auth api
python src/sweeper-bench/scripts/run_upstream_local.py --runner <sweeper-bench checkout> \
  --dataset-file test.jsonl --cases sweeper-001 sweeper-073 sweeper-102 \
  --phases baseline reference --reference-patch-dir ~/.cache/harbor-sweeper-bench/reference-patches \
  --out runs/upstream --auth subscription
# Harbor side
harbor run -p /tmp/sweeper -a oracle -y
harbor run -p /tmp/sweeper -a nop -y --ve SWEEPER_EVALUATE_EMPTY_PATCH=1
```

Links: [original benchmark](https://github.com/zlab-princeton/sweeper-bench) ·
[adapter PR #39](https://github.com/harbor-index/adapters/pull/39) · [parity fork](https://github.com/Chenngzz/sweeper-bench/tree/harbor-parity) · dataset PR: TBD · [parity PR (HF #317)](https://huggingface.co/datasets/harborframework/parity-experiments/discussions/317) · [parity artifacts](https://huggingface.co/datasets/LCZZZZ/sweeper-bench-parity-experiments). `harborframework/parity-experiments` is over Hugging Face's 1,000,000-file limit and rejects new files, so the artifacts are in a separate dataset with the same `adapters/sweeper-bench/` layout.

## Notes & Caveats

- **Pinned inputs.** Runner commit `96f86f2bb28f0de6c302c910aabe7baf3fb90b6a`,
  dataset revision `f10cd684be52322667f1373c8295624e13791b8f`, images
  `ghcr.io/evigbyen/*:alpha`. The `alpha` tag can move; image digests used in
  the pilot are recorded in the trial logs.
- **One container for agent and product.** Upstream runs the coding agent in a
  separate `prediction-<harness>` container and reaches the product through
  `/workspace/task-shell` (`docker exec`). In Harbor the agent runs inside the
  product container, so `task-shell` runs the same `bash -lc` command locally.
  The Playwright CLI/Chromium that upstream ships in the agent image are added
  to the product image under `/opt/sweeper-agent-node` and `/ms-playwright`.
- **Reference patches.** Upstream withholds its `data/patches/<case_id>.patch`
  files. The oracle uses `git diff --binary base_commit reference_commit`; this
  includes the PR's test changes, which do not affect the browser verifier.
- **Verifier container.** Upstream starts a new product container per phase
  attempt. Here the verifier environment is fresh for every trial, but the up to
  three phase attempts inside one trial reuse it (the checkout is reset and all
  product processes are killed between attempts; on-disk data such as a database
  created by the product survives). Retries only happen when the verifier says
  `uncertain`.
- **Empty patches.** Upstream does not evaluate an empty prediction (the agent
  must leave a non-empty diff). Harbor scores an empty diff 0 without a verifier
  call unless `SWEEPER_EVALUATE_EMPTY_PATCH=1`, which runs the upstream
  `baseline` phase instead.
- **Uncertain and infrastructure results.** A verdict of `uncertain` from the
  verifier agent (after up to three phase attempts) is a complete result and
  scores 0, as upstream (uncertainty is never a pass). Verifier crashes or
  unparseable output that survive upstream's three workflow retries, missing
  result cells, or an unpatched product that does not start end the trial
  without a reward file (`RewardFileNotFoundError`), so infrastructure failures
  are not counted as unsolved tasks. A patched product that does not start
  scores 0.
- **Patch collection quirk kept.** Like upstream, the patch is `git diff` of the
  working tree after intent-to-add; changes the agent commits or stages with
  `git add` are not collected (the prompt forbids committing).
- **Network.** The agent allowlist mirrors upstream for the providers Harbor
  supports; Harbor adds the configured inference endpoint. The verifier needs
  internet access to its model API (upstream does not firewall evaluation
  either).
- **Images and platforms.** All upstream images are `linux/amd64` and
  Debian/Ubuntu based (Debian 10 to Ubuntu 26.04). The agent Dockerfile installs
  the Playwright 1.63.0 CLI with `--with-deps` where Playwright supports the
  distro; elsewhere it installs the Chromium runtime libraries one by one
  (including `t64` package names), uses the Ubuntu 22.04 Chromium build when
  Playwright refuses the distro, and fails the build unless Chromium launches.
- **Image entrypoints.** Upstream starts every product container with the
  image's `product-container-entrypoint`; some images use it to link prebuilt
  `node_modules` or copy `dist` into the checkout. Both Harbor containers run
  that setup (without its final `exec sleep infinity`) before starting the
  product, and the verifier repeats it for each phase attempt.
- **Product start scripts that block GitHub.** Some `product-yarn start`
  scripts map GitHub and similar hosts to `0.0.0.0` in `/etc/hosts`. Upstream's
  agent lives in another container and never sees that file. Here the agent
  entrypoint removes only the tooling hosts (GitHub, nodejs.org, npm, PyPI) once
  the product is up, so Harbor can install the agent; during the agent phase the
  Harbor allowlist blocks them anyway, as upstream's egress proxy does.
- **Oracle and product side effects.** Upstream applies the reference patch to
  the clean base before starting the product. Here the product is already
  running when the oracle runs, so `solve.sh` first undoes tracked edits and
  removes untracked files that the start script created (ignored files such as
  dependencies stay), then applies the patch. Agents see the started tree, as
  upstream agents do.
- **Reference patch bytes.** Patches are kept as raw bytes end to end; several
  repositories commit CRLF files.
- **Readiness probe.** Like upstream's `curl` probe, any non-5xx answer
  (including a redirect, without following it) counts as ready.
- **Shared-host limits.** Many concurrent trials on one host share the kernel's
  inotify limits; apps with file watchers can then fail with `EMFILE`. Upstream
  runs each case on its own VM. Lower concurrency avoids it.
- **Licenses.** Runner code (MIT) is vendored under `tests/runtime/` with its
  license. Task text and workflows are CC BY 4.0 (SWEeper-Bench authors). App
  code is cloned from each app's public repository at build time and keeps its
  own license.

## Installation / Prerequisites

- Docker with Compose v2 (the verifier is a two-service compose project), about
  10 GB of image storage per task, amd64 hosts.
- Harbor ≥ 0.27.0 (separate verifier environments, `[[verifier.collect]]`,
  per-phase network policy). The pilot used Harbor 0.27.0.
- Python ≥ 3.12 and `uv` for the adapter; `git` and network access to GitHub
  (and codeberg.org for `sweeper-157`) for generation and image builds.
- Verifier credentials (see above). The original side additionally needs
  `GHCR_TOKEN` (the images are public, but the upstream boot script logs in).

## Troubleshooting

- `RewardFileNotFoundError`: read `verifier/test.log` in the trial directory.
  Typical causes are missing verifier credentials, a model error that persisted
  through three workflow retries, or an unpatched product that did not start.
- `cannot join network namespace of a non running container`: the verifier
  `main` service exited; check that `tests/Dockerfile` still clears the image
  entrypoint.
- Healthcheck timeouts: the product did not answer on `127.0.0.1:13200` within
  600 s; look at `/tmp/product-dev-server.log` inside the agent container.

## Citation

```bibtex
@article{yao2026sweeperbench,
  title   = {SWEeper-Bench: Can Agents Discover Bugs in Interactive Software?},
  author  = {Yao, Yang and Chen, Haozhe and Kang, Bingyi and
             Narasimhan, Karthik R and Liu, Zhuang},
  journal = {arXiv preprint arXiv:2610.14096},
  year    = {2026}
}
```

## Authors & Contributions

Adapter by Chengzhi Liu (chengzhi@ucsb.edu) for Harbor-Index v2.0. The
benchmark, runner, images and verifier are by the SWEeper-Bench authors.
