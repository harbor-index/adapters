# DistDebug-Bench → Harbor Adapter

## Overview

[DistDebug-Bench](https://github.com/OrderLab/distdebug-bench) asks an agent to find the root cause of a
real bug in a distributed system. The agent reads the bug report (title, description, attached logs) and
the source at the pre-fix commit, and writes `diagnosis.json` listing the code blocks it blames. It does
not write a fix. The diagnosis is scored against human-labeled root-cause blocks.

- **Tasks:** 34, the official **Lite** split (`benchmark/lite.txt`), across 11 systems (Elasticsearch,
  etcd, HBase, HDFS, Ignite, Ignite 3, Kafka, Kubernetes, Ray, TiKV, ZooKeeper). No task is excluded.
  `--split full` generates all 120 bugs (oracle 120/120).
- **Source:** upstream commit [`c3e929b`](https://github.com/OrderLab/distdebug-bench/tree/c3e929b9ee0082345337358e352ae37fc472df4c) (v1.1 labels).
- **Reward:** `block_coverage`, the paper's main metric. The other upstream metrics are in `reward.json`.

## What is DistDebug-Bench?

DistDebug-Bench ([paper](https://distdebug.com/assets/paper.pdf), NeurIPS 2026 Evaluations & Datasets;
[website](https://distdebug.com); [dataset](https://huggingface.co/datasets/OrderLab/distdebug-bench))
contains 120 bugs from 11 distributed systems. These bugs involve timing, message ordering and partial
failures, and they rarely come with a failing test. Each bug has human-labeled root-cause blocks. A
diagnosis is scored for precision and coverage at the file, function, line and block level, plus
`strict_block_coverage` and `mention_coverage`. The headline metric is `block_coverage`: the share of
labeled blocks hit by a predicted block in the same file, with a 5-line tolerance. Upstream provides
Mini-SWE-Agent, Codex CLI and OpenCode harnesses and a leakage checker.

## Adapter Features

- **Prompt.** `instruction.md` is byte-identical to the prompt upstream gives its Codex CLI and OpenCode
  harnesses (`cli_prompt(build_task(...))`); `scripts/check_prompt_equivalence.py` checks all 34 tasks.
  `configs/harbor_mini_swe_agent.yaml` makes Harbor's built-in `mini-swe-agent` send the same system and
  instance messages as upstream's Mini-SWE-Agent runner (`scripts/check_mini_prompt.py`).
- **Workspace.** `environment/build_snapshot.sh` follows upstream `repos._checkout_pruned_revision`:
  it fetches the pinned commit with its ancestors, checks it out detached, and deletes all refs,
  `FETCH_HEAD`, `ORIG_HEAD` and the reflog, so the fix is unreachable. History is kept because agents use
  `git log` and `git show`. Upstream refs (tags and short SHAs) are pinned to full SHAs in
  `snapshots.json`, and the build fails if `HEAD` differs. `.validate_diagnosis.py` and the bug's
  attachments (`.artifacts/`) are staged as in upstream `agent.stage_workspace`.
- **Scoring.** `tests/` holds the upstream `distdebug_bench` package, copied unchanged and checked
  against SHA-256 hashes at generation time, plus this bug's `report.json` and `blocks.json`.
  `tests/score_diagnosis.py` calls upstream `score.score_run`. The verifier runs in a separate container
  without network, and only `/workspace/diagnosis.json` is passed to it, so the labels never enter the
  agent's image.
- **Reward file.** `reward.json` has `reward` (= `block_coverage`), `valid_diagnosis`, and all upstream
  metrics. `diagnosis_scores.json` and `scoring_details.json` stay in the verifier logs.
- **Network.** Upstream runs agent commands with no network. Here the agent phase uses
  `network_mode = "allowlist"` with LLM API hosts only (`--allowed-hosts`, or `extra_allowed_hosts` per run).
- **Oracle.** `solution/solve.sh` writes the labeled blocks as a valid diagnosis (results under Notes & Caveats).
- **Tools for parity.** `scripts/harbor_job_to_distdebug.py` converts Harbor jobs into the upstream
  results layout, so upstream `score` and `leakage` run on them unchanged. `scripts/parity_compare.py`
  rescores both sides with the same `score_run`.
- No custom agent. Any Harbor agent can run the tasks.

## Generated Task Structure

```
distdebug-bench/
├── zookeeper-2845/
│   ├── task.toml                  # name, authors, metadata, agent allowlist, offline verifier, artifacts
│   ├── instruction.md             # upstream prompt: issue + output schema + workflow
│   ├── environment/
│   │   ├── Dockerfile             # ubuntu:24.04 + git, python3, ripgrep; snapshot at /workspace
│   │   ├── build_snapshot.sh      # pinned commit + ancestors, no refs/remotes/reflog
│   │   ├── validate_diagnosis.py  # -> /workspace/.validate_diagnosis.py (upstream)
│   │   └── artifacts/             # -> /workspace/.artifacts/ (bugs with attachments only)
│   ├── solution/solve.sh          # oracle
│   └── tests/                     # verifier image
│       ├── Dockerfile, test.sh, score_diagnosis.py
│       ├── distdebug_bench/       # upstream scorer package
│       ├── benchmark/             # systems.json + this bug's report.json / blocks.json
│       ├── bug_id.txt
│       └── upstream_manifest.json # source commit + hashes of the copied files
```

The adapter code is in `src/distdebug_bench/` (`adapter.py`, `main.py`, `task-template/`,
`snapshots.json` with the pinned commits of all 120 bugs, `parity_subset.txt`). Run configs are
`run_distdebug-bench.yaml` (oracle) and `run_distdebug-bench_parity.yaml`; helper scripts are in `scripts/`.

## Run Evaluation / Harness

### Running with Datasets Registry

```bash
harbor run -d orderlab/distdebug-bench                               # oracle
harbor run -d orderlab/distdebug-bench -a <agent> -m "<model>"
```

### Using Job Configurations

From the repository root, after generating the tasks:

```bash
harbor run -c src/distdebug-bench/run_distdebug-bench.yaml           # oracle, all 34 tasks
harbor run -p datasets/distdebug-bench -a codex -m "openai/gpt-5-mini"
# Mini-SWE-Agent with the upstream prompt
harbor run -p datasets/distdebug-bench -a mini-swe-agent -m "<model>" \
  --ak version=2.2.8 --ak config_file=src/distdebug-bench/configs/harbor_mini_swe_agent.yaml \
  --ak 'session_id_headers=[]'
```

Keep `session_id_headers=[]` with `mini-swe-agent` (see Notes & Caveats). If your model host is not in
the allowlist, add it with `extra_allowed_hosts` or regenerate with `--allowed-hosts`.

### Running Individual Trial

```bash
harbor trial start -p datasets/distdebug-bench/zookeeper-2845                     # oracle
harbor trial start -p datasets/distdebug-bench/zookeeper-2845 -a <agent> -m "<model>"
```

Harbor averages each key of `reward.json` over trials, which matches upstream aggregation (attempts
averaged per bug, bugs weighted equally). To use the upstream tools on a Harbor job:

```bash
python scripts/harbor_job_to_distdebug.py jobs/<job> results/<name> --redact-env <API_KEY_VAR>
cd /path/to/distdebug-bench      # at c3e929b
python -m distdebug_bench.score /abs/path/results/<name> --subset lite --attempts 1
python -m distdebug_bench.leakage /abs/path/results/<name>
```

## Usage: Create Task Directories

```bash
cd src/distdebug-bench
uv sync
uv run distdebug-bench --output-dir ../../datasets/distdebug-bench                        # Lite (34)
uv run distdebug-bench --split parity --output-dir ../../datasets/distdebug-bench-parity  # parity (12)
uv run distdebug-bench --split full --output-dir ../../datasets/distdebug-bench-full      # full (120)
uv run distdebug-bench --task-ids zookeeper-2845 --overwrite
```

Besides `--output-dir`, `--limit`, `--overwrite` and `--task-ids`: `--split {lite,full,parity}`
(default `lite`); `--source-dir` to use an existing upstream checkout instead of cloning the pinned commit
into `~/.cache/harbor-distdebug-bench/`; `--agent-network {allowlist,public,no-network}`,
`--allowed-hosts` and `--agent-timeout-sec` (default 3600 s, as upstream's CLI harnesses). Generation
stops if the upstream scorer files do not match the pinned hashes (`--allow-unpinned-source` overrides).
Source snapshots are fetched when images are built, not at generation time.

## Comparison with Original Benchmark (Parity)

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|----------------|--------------|--------------------------------|----------------------------|
| mini-swe-agent@2.2.8 | step-5-preview | block_coverage | 3 | 12 of 34 (35%) | 60.19 ± 5.44 | 58.52 ± 2.48 |
| mini-swe-agent@2.2.8 | step-5-preview | file_coverage | 3 | 12 of 34 (35%) | 80.56 ± 7.22 | 75.46 ± 4.04 |

Values are % (mean over 12 bugs), mean ± sample SEM over 3 runs. Per-run block_coverage: original
49.31 / 65.97 / 65.28, Harbor 53.75 / 62.08 / 59.72. Per-run file_coverage: original 68.06 / 80.56 /
93.06, Harbor 76.39 / 81.94 / 68.06. The run ranges also overlap for the other upstream metrics:
function_coverage 45.83 ± 5.56 vs 43.52 ± 6.52, line_coverage 52.49 ± 6.61 vs 53.05 ± 3.19,
strict_block_coverage 57.41 ± 5.16 vs 53.70 ± 2.45, block_precision 51.58 ± 1.38 vs 49.10 ± 1.31,
mention_coverage 72.59 ± 1.77 vs 74.72 ± 2.74.

Links: original [OrderLab/distdebug-bench@c3e929b](https://github.com/OrderLab/distdebug-bench/tree/c3e929b9ee0082345337358e352ae37fc472df4c)
(run unmodified; no fork) · adapter [harbor-index/adapters#32](https://github.com/harbor-index/adapters/pull/32) ·
dataset [harbor-framework/harbor-datasets#288](https://github.com/harbor-framework/harbor-datasets/pull/288) ·
parity artifacts [harborframework/parity-experiments#294](https://huggingface.co/datasets/harborframework/parity-experiments/discussions/294),
mirrored at [Nhckdvrl/distdebug-bench-parity-experiments](https://huggingface.co/datasets/Nhckdvrl/distdebug-bench-parity-experiments)
because the shared repo is over the Hub's file limit.

**Setup (same on both sides).**
- Mini-SWE-Agent 2.2.8, the harness of the paper's main table, and Harbor's built-in `mini-swe-agent`.
- StepFun `step-5-preview` via the Step Plan OpenAI-compatible endpoint (chat completions).
- Tasks: the 12-bug `parity` split (`parity_subset.txt`): the first Lite bug of each system plus
  `kafka-13668`, fixed before any run (10% of the 120-bug benchmark).
- Prompt identical (checked on the saved trajectories; only the `uname` line in `<system_information>`
  differs: x86_64 host vs aarch64 VM). `step_limit: 0`, `cost_limit: 3.0` (inactive, since LiteLLM has no
  price for this model), 60 s command timeout. Harbor's wall clock is capped at 2 h (upstream has none).
- One attempt per bug per run. A missing or invalid diagnosis would score 0; none occurred. Upstream's
  leaderboard retries until it gets a valid diagnosis; Harbor cannot retry inside a trial, so neither
  side retried. Both sides are rescored with the same upstream `score_run`.
- Ramp-up: a sanity run on the original side (zookeeper-2845, not in the parity set) and a check of the
  rendered prompts and agent command on the Harbor side, then run 1 on both sides, then runs 2 and 3.

**Attempt ledger.** Original side: 36 of 36 attempts scored, no reruns. Harbor side: 36 scored trials,
plus two that were not counted:
- run 1, kafka-13407: Harbor `AgentSetupTimeoutError` while installing the agent, so the agent never ran
  and there was no trajectory. Rerun as an infrastructure failure.
- run 2, elasticsearch-128583: flagged by the upstream leakage check, because the agent read the bug id
  from Harbor's agent log (see Notes & Caveats). Excluded, and rerun after the fix. Run 3 used the fix.

Both are kept in the parity upload (`_infra_failures/`, `_excluded/`). After them, the leakage check
flags 0 of 36 traces on each side, and the in-container `reward.json` equals upstream's offline
rescoring on 36 of 36 Harbor trials.

Per-bug block_coverage (runs 1 / 2 / 3). Disagreements go both ways (hbase-28376, kafka-13407); the
low-scoring trajectories are normal completions that blame a different component.

| Bug | Original | Harbor |
|---|---|---|
| elasticsearch-128583 | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 |
| etcd-17507 | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 |
| hbase-28376 | 1.0 / 1.0 / 1.0 | 0.0 / 1.0 / 0.0 |
| hdfs-10512 | 0.67 / 0.67 / 0.67 | 0.67 / 0.67 / 0.67 |
| ignite-6860 | 0.0 / 0.0 / 0.0 | 0.2 / 0.2 / 0.0 |
| ignite3-21827 | 0.5 / 0.5 / 0.5 | 0.5 / 0.5 / 0.5 |
| kafka-13407 | 0.0 / 0.0 / 0.0 | 0.0 / 0.0 / 1.0 |
| kafka-13668 | 0.25 / 0.25 / 0.5 | 0.25 / 0.25 / 0.5 |
| kubernetes-135981 | 0.5 / 0.5 / 0.5 | 0.5 / 0.5 / 0.5 |
| ray-14264 | 0.0 / 1.0 / 0.67 | 0.33 / 0.33 / 1.0 |
| tikv-18458 | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 |
| zookeeper-2581 | 0.0 / 1.0 / 1.0 | 1.0 / 1.0 / 0.0 |

**Reproduction.** Original side, in a checkout of `OrderLab/distdebug-bench` at `c3e929b` on Linux with
bubblewrap:

```bash
pip install -e ".[agent]"
cp <adapter>/configs/original/mini-swe-agent-step-5-preview.yaml configs/mini-swe-agent/step-5-preview.yaml
export OPENAI_API_KEY=<Step Plan key>
for i in 1 2 3; do for b in $(cat <adapter>/src/distdebug_bench/parity_subset.txt); do
  python -m distdebug_bench.run --config configs/mini-swe-agent/step-5-preview.yaml \
    --bug $b --runs 1 --max-attempts 1 --output results/parity-original-$i
done; done
```

Harbor side, from the repository root:

```bash
(cd src/distdebug-bench && uv run distdebug-bench --split parity --output-dir ../../datasets/distdebug-bench-parity)
export STEPFUN_API_KEY=<Step Plan key>
for i in 1 2 3; do
  harbor run -c src/distdebug-bench/run_distdebug-bench_parity.yaml --job-name distdebug-parity-$i
  python src/distdebug-bench/scripts/harbor_job_to_distdebug.py jobs/distdebug-parity-$i \
    results/parity-harbor-$i --redact-env STEPFUN_API_KEY
done
python src/distdebug-bench/scripts/parity_compare.py --upstream /path/to/distdebug-bench \
  --bugs src/distdebug-bench/src/distdebug_bench/parity_subset.txt \
  --original results/parity-original-{1,2,3} --harbor results/parity-harbor-{1,2,3}
```

## Notes & Caveats

- **Oracle.** With the final adapter on local Docker: Lite 34/34 and the full split 120/120 at reward 1.0,
  0 exceptions (two Elasticsearch images failed their first build on a network fetch and passed on rebuild).
  NOP scores 0 with `valid_diagnosis = 0`. Some secondary metrics stay below 1.0 for the oracle, by
  upstream's definitions: 9 bugs (Lite: `ray-25699`; full: also `elasticsearch-105065` and 7 Ray bugs) have a
  label with `start_line == end_line`, which covers no lines under the scorer's half-open ranges, and on
  `tikv-18756` the label `RangeLatch::acquire` has a generic method name, so `mention_coverage` looks for
  `rangelatch.acquire`, which Rust-style `::` text does not contain. These are left as is.
- **Sandbox.** Upstream runs agent commands in bubblewrap with no network. Here the agent runs in the
  task container, and only LLM API hosts are reachable. Issue trackers, GitHub and the benchmark site
  are not. The image has `git`, `python3`, `ripgrep`, `less`, `file` and `procps`, but no compilers or JDKs
  (the task is diagnosis, not reproduction). The workspace is `/workspace` instead of upstream's random
  `workspace-<uuid>`.
- **Bug id in Harbor's mini-swe-agent log.** By default Harbor's `mini-swe-agent` passes the trial name
  as an `X-Session-ID` header on its command line, and mini-swe-agent prints that line to
  `/logs/agent/mini-swe-agent.txt`, which the agent can read. The trial name starts with the bug id. In
  one parity trial the agent found it there. The configs therefore set `session_id_headers: []`; with
  it, the bug id is not in that log, the process list, the environment or the hostname (checked in a
  running trial). The same log line contains the model API key, so convert traces with `--redact-env`
  before sharing them.
- **Upstream text the agent sees.** `.validate_diagnosis.py` is upstream's validator, copied unchanged.
  Its docstring says "the benchmark's output schema" but does not name the benchmark. Some issue
  descriptions mention other issue keys (e.g. ZOOKEEPER-2678 in zookeeper-2845); that is the original
  report text.
- **Invalid diagnoses and leakage.** Upstream discards attempts without a valid diagnosis and attempts
  in which the agent saw the original issue or fix, then retries. Harbor scores the first case 0 with
  `valid_diagnosis = 0` and cannot detect the second inside the verifier. Run upstream
  `distdebug_bench.leakage` on converted traces to apply that rule.
- **Label paths.** Some HDFS and Ignite labels omit the module prefix (e.g. `hadoop-hdfs/src/...`).
  The upstream scorer matches by containment, so repository-relative answers still match. Every labeled
  file of the 34 tasks resolves to exactly one file at its pinned commit.
- **Image sizes.** Snapshots keep full ancestor history: Elasticsearch 1.4–1.6 GB, Kubernetes 1.5 GB,
  every other Lite bug at most 0.45 GB; about 15 GB for all 34. `build_timeout_sec` is 3600 for
  Elasticsearch and Kubernetes and 1800 otherwise.
- **Local run settings for the Harbor parity runs** (macOS, Docker via Colima, one task at a time):
  a PyPI mirror for the agent install (`--ae UV_DEFAULT_INDEX=...`; same package versions), an
  `--extra-docker-compose` overlay pinning `api.stepfun.com` in `/etc/hosts` (Colima's embedded DNS
  cannot resolve once the allowlist is active), and a proxy for GitHub during image builds only. None of
  these changes the task, prompt or scorer.

## Installation / Prerequisites

- Docker, or another Harbor environment with separate verifiers and `allowlist` networking (e.g.
  Daytona); GitHub access during image builds.
- Harbor CLI: `uv tool install harbor`.
- Adapter: `cd src/distdebug-bench && uv sync` (Python ≥ 3.11; depends only on PyYAML).
- API keys for your agent's model, with its host in the allowlist.
- Original side of parity: Linux with bubblewrap, Python ≥ 3.10.

## Troubleshooting

- **Snapshot fetch fails during the build:** usually GitHub rate limits or a slow link. The fetch
  aborts after 60 s below 1 KB/s and retries 3 times; rebuild the task. If GitHub needs a proxy, give it
  to the builds only (`proxies` in `~/.docker/config.json`) and remove it before running agents: proxy
  variables inside the containers conflict with Harbor's egress control.
- **Low disk space:** run with `-n 1` and `docker builder prune` between tasks. With Colima or Docker
  Desktop, cap the VM disk so it cannot outgrow the host's free space.
- **Agent cannot reach its model:** add the host with `extra_allowed_hosts` or `--allowed-hosts`. If the
  environment has no `allowlist` support, generate with `--agent-network public` and run the leakage
  check afterwards.
- **`valid_diagnosis: 0`:** no `/workspace/diagnosis.json`, or one that fails the schema;
  `scoring_details.json` says why.
- **"Upstream files differ from the pinned commit":** `--source-dir` is not at `c3e929b`.
- **Windows hosts:** check out with LF line endings (`git config core.autocrlf false`); a CRLF
  `test.sh` or `build_snapshot.sh` fails in the container.

## Citation

```bibtex
@inproceedings{jiang2026distdebugbench,
  title     = {{DistDebug-Bench}: Can {LLM} Agents Diagnose Bugs in Distributed Systems?},
  author    = {Jiang, Yuxuan and Liu, Chenglin and Rose, John and Miller, Mason and Thavamani, Ruthesh and Wu, Juntao and Huang, Yinwei and Xu, Jiahao and Huang, Peng},
  booktitle = {Advances in Neural Information Processing Systems (NeurIPS)},
  year      = {2026}
}
```

## Authors & Contributions

This adapter is developed and maintained by Xiang Ding (GitHub: Nhckdvrl) for Harbor Index v2.0.

- Issues and pull requests: [harbor-index/adapters](https://github.com/harbor-index/adapters).
- Questions about the benchmark: [OrderLab/distdebug-bench](https://github.com/OrderLab/distdebug-bench).
