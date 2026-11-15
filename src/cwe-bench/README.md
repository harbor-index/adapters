# CWE-bench → Harbor Adapter

## Overview

This adapter packages the **CWE-bench public set** for Harbor: 100 defensive security
tasks covering 91 distinct CWE types across 8 languages (Go 27, Java 15, JavaScript 15,
C++ 14, C 10, Python 10, Rust 5, TypeScript 4). In each task an agent audits a real
open-source codebase that contains one security weakness and must find and fix it
without breaking the code's normal behavior.

- **Provenance:** Collinear AI — [paper](https://arxiv.org/abs/2610.17020),
  [website](https://cwe-bench.com/),
  [dataset](https://huggingface.co/datasets/collinear-ai/cwe-bench).
- **License:** Apache-2.0 (each task's `environment/repo.tar.gz` holds code from an
  upstream open-source project that stays under that project's license).
- **Access:** the upstream dataset is gated on Hugging Face; see
  [Installation / Prerequisites](#installation--prerequisites).
- **Task count:** 100, which is every task package in upstream release 2.0.1 and the
  whole public set of the 250-task benchmark the paper describes. The other CWE-bench
  sets cannot be adapted: the 150-task held-out set is not released, and the earlier
  120-task benchmark (described only in the first arXiv version of the paper) was
  private.
- **Modifications:** none to instructions, environments, verifiers or reference
  solutions. CWE-bench is already distributed as Harbor tasks, so the adapter copies
  each task package verbatim and only sets the `[task]` identity in `task.toml`.

## What is CWE-bench?

CWE-bench measures whether coding agents can *defend* software rather than attack it.
Each task gives the agent a checkout of a real open-source repository and a
description of its threat model, but not the weakness, where it is, or how many
vulnerable sites there are. The agent audits the code and patches what it finds; it is
never asked to write an exploit.

Scoring is fully executable and deterministic. A task's verifier runs exploit inputs
and legitimate cases against the patched code: `reward = 1` only if every exploit
input is closed **and** every legitimate case still works, otherwise `0`. There is no
partial credit. A verifier that cannot evaluate at all (for example because the
environment is broken) writes no reward: upstream calls that trial void and reruns it
instead of scoring it. Upstream reports **pass@1** as the mean reward over 4 valid
attempts per task (400 trials on the public set), with a one-hour agent limit per
attempt, and also reports pass@4.

## Adapter Features

- **Pinned upstream release.** Defaults to release 2.0.1: commit
  `d80ebe1014b49a147e77a09d67fd0a4101333ad6`, which is the commit upstream's
  `registry.json` pins for every task of `cwe-bench@2.0.1`. It differs from the
  leaderboard release 2.0 (commit `e0224c430f47575506297a7d55d3635f46d82b2d`) only by
  verifier fixes for two tasks so their reference solutions score 1 in a plain Docker
  setup. `--revision` accepts a release name (`2.0.1`, `2.0`), a commit hash, a tag
  or a branch.
- **Verbatim task packages.** `instruction.md`, `environment/`, `tests/` and
  `solution/` are copied byte-for-byte. `task.toml` keeps every upstream key and
  table; only two fields of `[task]` change: `name` becomes
  `collinear-ai/cwe-bench__<id>` (upstream ships `collinear-ai/<id>`), and the
  paper's authors are appended to `authors` (upstream credits its generation pipeline
  on 65 tasks and lists no author on the other 35). `keywords` would be filled in
  only if upstream set none; at release 2.0.1 every task already has them.
- **Stable registry names.** `collinear-ai/cwe-bench__<upstream-task-id>`, generated in
  sorted order, identical across runs.
- **Gated-download handling.** Fetches through `huggingface_hub` with your own
  credentials and explains how to get access when it is missing.
- **Verified snapshot.** Every file under `tasks/` of the pinned commit, plus
  `LICENSE` and `NOTICE`, is checked against the Hub's listing (presence and size)
  before anything is generated, and anything else found under the snapshot's `tasks/`
  directory stops the run. The listing is recorded inside the snapshot directory so
  later runs can re-check it without network access. A
  `--source-dir` copy is used as it is: it is not compared with the Hub, and only the
  fail-closed checks below run (plus the recorded listing, when the directory is a
  snapshot this adapter downloaded).
- **Fail-closed generation.** All selected task packages are checked before the first
  one is written: a missing required file, an unreadable file, a Git LFS pointer in
  place of a file, a special file, or a link that leaves its task package stops the
  run with nothing written. Each `task.toml` rewrite is compared with the upstream
  file and refused if anything but the identity fields would change. Each task is
  built in a hidden staging directory inside the output directory and moved into
  place with a rename, so an interrupted or failed run does not leave a half-written
  task in the dataset, also with `--overwrite`. Only one run at a time writes to an
  output directory; a second one is refused. This relies on a file lock: where the
  file system has none the adapter warns and carries on, and on a network file system
  the lock may not exclude a run on another host.
- **Normalized file modes.** Inside the output directory, files are written as 0644,
  or 0755 when upstream marks them executable, and directories as 0755, whatever the
  umask; other mode bits are not carried over. `solution/solve.sh` and
  `tests/test.sh` are always written 0755 (a Hub download carries no executable
  bit). The output directory itself keeps the mode you or your umask give it.
- **`--omit-solutions`.** Writes a stub in place of each reference solution, for
  copies of the dataset that must not carry them (see
  [Notes & Caveats](#notes--caveats) for what it does not remove). The upstream
  `solution/` directories are then never copied into the output directory, not even
  into its staging area. The upstream snapshot in `--cache-dir` (or your
  `--source-dir` copy) still contains them, and the adapter needs them there, so keep
  that directory out of anything you publish.
- **One source and one setting per output directory.** `ADAPTER_NOTICE.txt` records
  the upstream revision and whether reference solutions are included, and that record
  holds for every task directory next to it. A run that would leave task directories
  from another revision, another local copy or the other `--omit-solutions` setting
  in place is refused; to convert a directory, regenerate all of it with
  `--overwrite` and without `--limit` or `--task-ids`. Anything else found at a
  task's path (for example a directory that lost its `task.toml`) is covered by the
  same rule, and with `--omit-solutions` it is never skipped, only replaced.
- **Attribution.** Copies the upstream `LICENSE` and `NOTICE` into the output
  directory (when a directory is converted to a source that lacks one of them, the
  earlier file is removed) and writes `ADAPTER_NOTICE.txt` (the upstream revision and
  the changes made). These are dataset-root files; Harbor registry packages do not include them
  (see "What a registry copy would contain" under [Notes & Caveats](#notes--caveats)).
- **Agents.** No custom agent is added; the upstream harness is Harbor itself.

## Generated Task Structure

```
cwe-bench/
├── LICENSE                           # upstream Apache-2.0 license
├── NOTICE                            # upstream attribution
├── ADAPTER_NOTICE.txt                # upstream revision, solutions setting, changes made
└── {task_id}/                        # e.g. c-cwe191-be1d924f-hardened-v2
    ├── task.toml                     # upstream config + [task] identity
    ├── instruction.md                # the audit brief given to the agent
    ├── environment/
    │   ├── Dockerfile                # builds the vulnerable codebase into /app
    │   ├── docker-compose.yaml
    │   ├── repo.tar.gz               # the vulnerable codebase
    │   └── ...                       # per-task extras (env.json, wheels/, ...)
    ├── solution/
    │   ├── solve.sh                  # the reference fix
    │   └── ...                       # per-task extras (patches, files/)
    └── tests/
        ├── test.sh                   # verifier entry point
        ├── verify_programmatic.py    # deterministic exploit + legitimate-case checks
        └── ...                       # per-task extras (probes/, verify.py, ...)
```

Task ids follow upstream: `<language>-cwe<N>-<id>`, with a `-hardened-vK` suffix
marking a hardened revision of a base task. Two upstream packages carry an extra
top-level entry that is copied as is: `c-cwe329-fadca8b4/task_src/` and
`java-cwe565-5fde8582/README.md`.

The adapter itself:

```
src/cwe-bench/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── uv.lock
├── run_cwe-bench.yaml
└── src/cwe_bench/
    ├── __init__.py
    ├── adapter.py
    ├── main.py
    └── task-template/
        ├── task.toml                 # the [task] identity applied to every task
        ├── instruction.md            # placeholder (upstream file is copied)
        ├── environment/Dockerfile    # placeholder (upstream file is copied)
        ├── solution/solve.sh         # stub written by --omit-solutions
        └── tests/test.sh             # placeholder (upstream file is copied)
```

## Run Evaluation / Harness

### Running with Datasets Registry

The dataset is **not in the Harbor registry**. Upstream's access conditions forbid
republishing its reference solutions, so whether and how a copy is registered still
has to be agreed with the Harbor maintainers and Collinear AI. A registered copy would
be one generated with `--omit-solutions` and would be run like this, following the
upstream protocol of 4 attempts per task:

```bash
harbor run -d collinear-ai/cwe-bench -a <agent_name> -m "<model_name>" -k 4 \
  --allow-agent-host <model API host>
```

The oracle agent cannot pass on such a copy, because it carries no reference
solutions. To run the oracle, or to run anything before a copy is registered, generate
the tasks locally with your own access ([Usage](#usage-create-task-directories)) and
use the commands below. What a registered copy would and would not contain is listed
under "What a registry copy would contain" in [Notes & Caveats](#notes--caveats).

### Using Job Configurations

```bash
# From the repository root: oracle verification with the reference config
harbor run -c src/cwe-bench/run_cwe-bench.yaml

# The same config with an agent
harbor run -c src/cwe-bench/run_cwe-bench.yaml -a <agent_name> -m "<model_name>" \
  --allow-agent-host <model API host>

# Or point at the generated dataset directly
harbor run -p datasets/cwe-bench -a <agent_name> -m "<model_name>" -k 4 \
  --allow-agent-host <model API host>

# Resume a previously started job
harbor job resume -p jobs/<job_name>

# Run trials lost to infrastructure again: void, cancelled, or the environment did
# not start (see "Void trials" under Notes & Caveats)
harbor job resume -p jobs/<job_name> -f CancelledError -f RewardFileNotFoundError \
  -f EnvironmentStartTimeoutError
```

Results are saved in `jobs/` by default (`jobs_dir` in the YAML config).

### Running Individual Trial

```bash
# Oracle on a single task
harbor trial start -p datasets/cwe-bench/<task_id>

# A specific agent and model on a single task
harbor trial start -p datasets/cwe-bench/<task_id> -a <agent_name> -m "<model_name>" \
  --allow-agent-host <model API host>
```

## Usage: Create Task Directories

```bash
cd src/cwe-bench
uv sync
uv run cwe-bench --output-dir ../../datasets/cwe-bench
```

Available flags:

- `--output-dir` — directory to write generated tasks (default: `datasets/cwe-bench`
  at the repository root)
- `--limit` — generate only the first N tasks
- `--overwrite` — replace task directories that already exist (otherwise they are
  skipped). Changing the upstream revision, the local copy or the `--omit-solutions`
  setting of an existing directory needs `--overwrite` without `--limit` or
  `--task-ids`
- `--task-ids` — only generate these tasks; accepts upstream ids
  (`go-cwe117-...`), upstream names (`collinear-ai/go-cwe117-...`) or registry names
  (`collinear-ai/cwe-bench__go-cwe117-...`)
- `--revision` — upstream release name (`2.0.1`, `2.0`) or Hugging Face revision:
  commit hash, tag or branch (default: `2.0.1`); cannot be combined with
  `--source-dir`
- `--source-dir` — use a local copy of the upstream repository (the directory that
  contains `tasks/`) as it is instead of downloading. The copy is not compared with a
  release (a complete 2.0.1 copy has 100 task packages), and `ADAPTER_NOTICE.txt`
  then records a fingerprint of the copy instead of a revision, unless the directory
  is a snapshot this adapter downloaded, which is re-checked and recorded under its
  commit
- `--cache-dir` — where the upstream snapshot is downloaded (default:
  `$CWE_BENCH_CACHE_DIR` or `~/.cache/harbor-adapters/cwe-bench`); cannot be combined
  with `--source-dir`
- `--omit-solutions` — write a stub instead of each reference solution; a directory
  generated without it can only be converted as a whole (`--overwrite` without
  `--limit` or `--task-ids`)

## Comparison with Original Benchmark (Parity)

**Status: parity experiments have not been run yet.** They require agreeing the agent,
model and number of runs with the Harbor team first.

CWE-bench's original harness is Harbor, and the generated tasks are the upstream task
packages with only the `[task]` identity changed, so the comparison is between two
ways of loading the same tasks: the upstream registry (original side) and this
adapter's output (Harbor side).

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|
| pending | pending | pass@1 (mean reward) | pending | 100 (100%) | pending | pending |

Reproduction steps:

```bash
# Original side: upstream task packages resolved from the upstream registry
harbor run --repo "https://huggingface.co/datasets/collinear-ai/cwe-bench" \
  -d cwe-bench@2.0.1 -a <agent> -m "<model>" --allow-agent-host <model API host>

# Harbor side: tasks generated by this adapter
cd src/cwe-bench && uv run cwe-bench --output-dir ../../datasets/cwe-bench && cd ../..
harbor run -c src/cwe-bench/run_cwe-bench.yaml -a <agent> -m "<model>" \
  --allow-agent-host <model API host>

# File-level equivalence: only the 100 task.toml files may differ (in their [task]
# table), plus the added LICENSE, NOTICE and ADAPTER_NOTICE.txt at the dataset root
diff -r <upstream snapshot>/tasks datasets/cwe-bench
```

The original-side command reads the gated repository with git (and git-lfs for the
`repo.tar.gz` archives), not with `huggingface_hub`, so `hf auth login` or `HF_TOKEN`
alone is not enough there: git must be able to authenticate to huggingface.co with a
token that can read gated repositories, for example with
`hf auth login --add-to-git-credential` and a git credential helper.

The metric is the mean of per-task rewards (each 0 or 1) over valid trials, which is
how upstream computes pass@1. Void trials and trials lost to infrastructure (for
example `EnvironmentStartTimeoutError`) must be run again on both sides before the
means are compared (see "Void trials" under Notes & Caveats). Results will be recorded
in `parity_experiment.json`.

## Notes & Caveats

- **Access conditions.** The upstream dataset asks users not to train models that will
  be evaluated on CWE-bench on its tasks or solutions, and not to republish its
  solutions. Generating tasks locally with your own access is the intended use, and
  the oracle result reported below comes from such a local copy. A copy submitted to
  the Harbor datasets repository or registry would be the `--omit-solutions` one unless
  Collinear AI permits otherwise.
- **What `--omit-solutions` does and does not remove.** It replaces each task's
  `solution/` directory with a stub `solve.sh` that exits non-zero, so the oracle agent
  scores 0 on such a copy. Everything else is copied as upstream shipped it: the
  verifiers in `tests/`, every `task.toml` key (including the CVE and advisory ids and
  `golden_solution_path` under `[metadata]`), and the two extra top-level entries
  `java-cwe565-5fde8582/README.md` and `c-cwe329-fadca8b4/task_src/`. At release 2.0.1
  that README and the same task's `tests/test_constructor_compatibility.py` contain
  upstream author notes that describe the reference solution, so confirm with upstream
  before publishing even an omit copy.
- **Which CWE-bench this is.** CWE-bench v2, as described in the current paper: 250
  tasks spanning 185 CWEs, of which the 100 public tasks (91 CWEs) are adapted here
  and 150 are held out. The first arXiv version of the paper described the earlier
  private 120-task benchmark spanning 73 CWEs.
- **Release pin.** The leaderboard reports release 2.0. This adapter defaults to
  2.0.1, which upstream describes as verifier fixes for
  `java-cwe350-47963208-hardened-v3` and `cpp-cwe862-574610fa` with no change to what
  any check requires and no change to any model score.
- **Network policy.** Every task runs the agent phase with no network access and
  expects the harness to admit only the model API host (`--allow-agent-host`). Image
  builds and the verifier keep network access.
- **Dependencies are fetched while the image builds.** Each upstream Dockerfile
  downloads the project's dependencies on a best-effort basis during the build (Go
  modules through `proxy.golang.org`, plus npm, crates.io, Maven Central and PyPI where
  the project needs them) and then switches Go and Cargo to offline mode. If a
  registry cannot be reached during the build, the image still builds, but the
  verifier cannot compile the project: the trial is then void or, for some tasks,
  scored 0, for every agent including the oracle.
- **Limits.** All 100 tasks set a 3600 s agent timeout, 2 CPUs, 2 GB memory and 4 GB
  storage. The image build timeout is 600 s for 68 tasks and 1200 s for 32; verifier
  timeouts range from 3614 s to 9614 s.
- **Void trials.** If a verifier cannot evaluate (for example, the project does not
  compile because its dependencies were not downloaded), upstream writes no reward
  and treats the trial as void rather than as a failure. Harbor does not apply that
  rule to its job summary: it records the trial as a `RewardFileNotFoundError`, keeps
  it on a plain `harbor job resume`, and counts it as 0 in the job-level mean. It
  does the same with a trial whose environment never started
  (`EnvironmentStartTimeoutError`). Before reporting a score, check the exception
  table and run those trials again with
  `harbor job resume -p jobs/<job_name> -f CancelledError -f RewardFileNotFoundError -f EnvironmentStartTimeoutError`,
  adding one `-f` for any other infrastructure exception listed there; report any
  that remain void separately instead of as failures. Do not filter
  `AgentTimeoutError`: a rollout that reaches the one-hour agent limit is graded on
  the code as it stands.
- **Pass-through check.** At release 2.0.1 a recursive diff between the upstream
  snapshot's `tasks/` directory and the generated dataset shows differences only in
  the 100 `task.toml` files (the two `[task]` fields described above) plus the three
  added root files `LICENSE`, `NOTICE` and `ADAPTER_NOTICE.txt`. File contents are
  otherwise identical. File modes are normalized, and it is the adapter, not
  upstream, that sets the executable bit on `solution/solve.sh` and `tests/test.sh`.
- **Harbor version.** This adapter was exercised with Harbor 0.25.0. Upstream's task
  configs name 0.22 as the minimum for the per-phase `network_mode` they rely on.
- **Disk use.** Tasks build on a shared base image of about 3.3 GB; individual task
  images reach several GB for the larger JavaScript and Go projects. In the author's
  runs Harbor 0.25.0 left untagged images behind after trials, so prune unused images
  (`docker image prune`) between large runs.
- **Oracle verification.** With Harbor 0.25.0 on local Docker (a 16-core host), the
  oracle agent was run on all 100 generated tasks. Every one of the 100 reference
  solutions scored 1 on this host, though not in a single run and, for the Go tasks,
  not from unmodified task files. In detail:
  - 74 scored 1 in the full run on the unmodified tasks, and `python-cwe200-fa968a47`
    scored 1 on a rerun after its image build had timed out while the host was
    overloaded.
  - `typescript-cwe306-43fe6624` scored 1 with `--override-memory-mb 6144`. Its image
    build also timed out in the full run, and it scored 0 on two reruns under the
    task's own 2 GB memory limit: its verifier builds a Next.js app, which starts one
    worker fewer than the host has cores (15 here), and that build was killed by the
    memory limit.
  - The 24 Go tasks that have to download third-party modules could not download
    them in the full run (the other three build from the `vendor/` directory they
    ship; `go-cwe613-75fa8fbd` ships one too, but its build still fetches modules),
    because `proxy.golang.org` is not reachable from the author's network: 14 trials
    were void and 10 scored 0. They were run again from a scratch copy whose
    Dockerfile had one added line that points Go at the modules' source repositories
    (`ENV GOPROXY=direct`), with nothing else changed. 23 scored 1. Their image builds
    took between 52 s and 11 min; one (`go-cwe295-558d2730`, 657 s) was longer than
    the task's own build limit of 600 s, which had been raised for this run with
    `--environment-build-timeout-multiplier`.
  - `go-cwe272-cb670134` needed one more step. One of its dependencies,
    `gitea.dev/actions-proto-go v0.6.0`, cannot be fetched from its source repository,
    which carries no tag for that version; it is available only from the module
    proxy. With `GOPROXY=direct` alone the image is built without it and the verifier
    reports a void trial. The three files of that module version were therefore
    fetched from `proxy.golang.org` on another network, checked against the checksums
    in the task's `go.sum`, and copied into the image's module cache with one further
    added line in the scratch Dockerfile. The task then scored 1.

  The reruns of `python-cwe200-fa968a47` and `typescript-cwe306-43fe6624` used
  `--timeout-multiplier 3`; their builds and verifiers finished inside the tasks' own
  limits. A single oracle run of all 100 unmodified tasks on a host that can reach
  `proxy.golang.org` is still outstanding and will be reported here.
- **Inherited from upstream.** The tasks keep upstream's `schema_version = "1.1"` and
  its shared-container verifier. `harbor lint` (0.25.0) has no check for the schema
  version; it reports `shared-verifier` for all 100 tasks and `unknown-config-field`
  for the `[provenance]` table in `cpp-cwe916-c8e15f06`. The upstream `test.sh`
  writes no reward on its void path by design.
- **What the agent can see (upstream, unchanged).** Every task image contains a small
  world-readable marker file under `/etc` whose name and content label the container
  as a security evaluation; the agent runs as root, and no verifier script in release
  2.0.1 refers to the file. The Dockerfiles of 65 tasks delete release-note files
  matched by name (changelogs, `NEWS`, `SECURITY.md` and similar) from `/app` as a
  memorization mitigation; the 35 tasks with no upstream author entry have no such
  step, so most of them keep those files. In both groups other project documents that
  cite earlier CVEs can remain. No task archive contains version-control history.
- **What a registry copy would contain.** `harbor publish` uploads each task as its
  own package (`task.toml`, `instruction.md`, `README.md`, `environment/`, `tests/`,
  `solution/`) plus the dataset's `dataset.toml` metadata and `README.md`. The
  dataset-root `LICENSE`, `NOTICE` and `ADAPTER_NOTICE.txt` belong to no package, and
  `harbor init` does not list them in `dataset.toml`. Upstream asks redistributors to
  keep the license and the NOTICE attribution, so at registration the Apache-2.0
  license reference, the NOTICE text and the modification notice have to go into the
  dataset `README.md` and the `dataset.toml` description, or the three files have to
  be listed under `[[files]]` in `dataset.toml` if the Harbor team accepts that. In a
  package, `c-cwe329-fadca8b4/task_src/` is left out, as is the bytecode cache that
  upstream ships under `c-cwe319-dcf620e7/tests/__pycache__/` (the `.py` file is
  packaged), and `java-cwe565-5fde8582/README.md` is published as that task's readme.

## Installation / Prerequisites

- Linux or macOS. The adapter relies on Unix file locking (`flock`) and has not been
  run on Windows.
- Docker installed and running, with access to the package registries listed under
  "Dependencies are fetched while the image builds".
- Harbor (`uv tool install harbor`); tested with 0.25.0.
- Adapter dependencies:
  ```bash
  cd src/cwe-bench
  uv sync
  ```
- Access to the gated upstream dataset:
  1. Sign in to Hugging Face and open
     <https://huggingface.co/datasets/collinear-ai/cwe-bench>.
  2. Review and accept the access conditions (access is granted automatically).
  3. Authenticate on the machine that runs the adapter: `uv run hf auth login` from
     `src/cwe-bench` (the adapter's environment includes the `hf` command), or export
     `HF_TOKEN` with a token that can read gated repositories.
- For the original-side parity command only: git must also be able to authenticate to
  huggingface.co (see [Comparison with Original Benchmark
  (Parity)](#comparison-with-original-benchmark-parity)).
- The upstream snapshot is about 0.6 GB, and the generated dataset about the same.

## Troubleshooting

- **`Could not read the gated dataset`** — complete the access steps above; the
  message is also printed when the token is missing or cannot read gated repositories.
- **`The upstream snapshot ... is incomplete or holds entries that are not part of the
  release`** — the snapshot directory no longer matches the release: files were added
  under `tasks/` (a file manager's `.DS_Store` is enough), or files of the release
  were removed or changed in size. An interrupted download does not normally cause
  this; the next run completes it. Remove the entries reported as unexpected; if files are missing or have the wrong
  size, delete the directory named in the message so that it is downloaded again. For
  a `--source-dir` copy that carries the adapter's `.cwe-bench-manifest.json`, restore
  the files, or remove that manifest to use the copy as it is; it is then recorded as
  an unverified local copy and can no longer continue a directory generated from the
  release.
- **`... holds task directories that were generated from another upstream revision
  ...`** — the output directory was generated from another revision or local copy or
  with the other `--omit-solutions` setting, or it has no `ADAPTER_NOTICE.txt` (which
  is also the state an interrupted conversion leaves behind). Regenerate all of it
  with `--overwrite` and without `--limit` or `--task-ids`, or use another directory.
- **`Another cwe-bench run is writing to ...`** — only one run at a time may write to
  an output directory; wait for the other run to finish.
- **A hidden `.staging-*` directory inside the output directory** — a run was killed
  before it could clean up, or it stopped with `Could not remove ...` (see the next
  entry). A killed run can also leave a hidden `.LICENSE.new`, `.NOTICE.new` or
  `.ADAPTER_NOTICE.txt.new` file, which the next run replaces. The directory holds at most one unfinished task and the
  previous copy of a task that was being replaced. The next run that holds the
  directory's lock removes it before doing anything else, and it is safe to delete
  when no adapter run is active. Where the file system has no locks the adapter does
  not remove it and stops with `... was left by another cwe-bench run`; delete it
  yourself once no other run is active.
- **`Could not remove ...`** — an earlier copy of a task, or a leftover staging
  directory, could not be deleted, usually because some of its directories are not
  writable. The run stops instead of finishing while that copy (which may include a
  reference solution) is still inside the output directory, and writes no
  `ADAPTER_NOTICE.txt` if it was converting the directory. The copy stays where the
  message says until a later run can delete it or you delete it yourself, so do not
  copy or publish the output directory in this state. Make the output directory
  writable (`chmod -R u+rwX <output-dir>`) and run the adapter again.
- **A task directory is skipped** — it already exists in the output directory; pass
  `--overwrite` to regenerate it. A warning that it `exists but holds no task.toml`
  means something that is not a complete task sits at that path; `--overwrite`
  replaces it. With `--omit-solutions` the run stops instead of skipping such an
  entry, because it may hold a reference solution.
- **Go tasks are void or score 0 for every agent, including the oracle** — the image
  was built without its Go modules (`module lookup disabled by GOPROXY=off`, or
  `permission denied` under `/go/pkg/mod/cache/download`, in
  `verifier/programmatic.log`). `proxy.golang.org` was unreachable during the build,
  which some corporate networks block. Rebuild on a network that can reach it. Where
  that is not possible, pointing Go at the modules' source repositories
  (`ENV GOPROXY=direct` in a private copy of the task's Dockerfile) works for all but
  one of these tasks; `go-cwe272-cb670134` also needs one module that only the proxy
  serves (see "Oracle verification").
- **`EnvironmentStartTimeoutError`** — the image build exceeded the task's build
  timeout, which happens on a heavily loaded host. To run those trials again at the
  same limits, use the `harbor job resume` command with
  `-f EnvironmentStartTimeoutError` shown under
  [Using Job Configurations](#using-job-configurations). `harbor job resume` reuses
  the saved configuration, so to lower the concurrency or allow a longer build, start
  a new run restricted to those tasks, for example
  `harbor run -p datasets/cwe-bench -i <task_id> -a <agent_name> -m "<model_name>" --environment-build-timeout-multiplier 3`,
  and report its result together with the original job, whose mean still counts
  those trials as 0. `--timeout-multiplier` would also stretch the one-hour agent
  limit.
- **`The execution phase network policy differs from a broader setup or shared phase
  policy`** — Harbor's check for network-policy support did not finish within its 30 s
  limit, typically because Docker was busy or was pulling the small probe image for
  the first time. Run the job again once Docker is idle.
- **Agent cannot reach its model API** — the agent phase has no network access; add
  `--allow-agent-host <host>` for the provider you use. If the host still cannot be
  resolved, check whether the Docker daemon sets a custom `dns` server: on the
  author's machine (Docker 25, `dns` set in `daemon.json`) name resolution failed
  inside the agent phase until the run was given `--extra-docker-compose` with a file
  that sets `network_mode: bridge` on the `harbor-docker-egress-control-sidecar`
  service.
- **`typescript-cwe306-43fe6624` scores 0 with `the application does not build
  (exit=-9)`** — on a host with many CPU cores the verifier's Next.js build exceeds
  the task's 2 GB memory limit (see "Oracle verification"). It passed with
  `--override-memory-mb 6144`; use the same setting on both sides of any comparison.
- **Oracle scores 0 everywhere** — check whether the dataset was generated with
  `--omit-solutions`.

## Citation

```bibtex
@misc{gonzalezpumariega2026cwebench,
  title         = {CWE-bench: Refocusing Agentic Security from Exploitation to Defense},
  author        = {Gonzalez-Pumariega, Gonzalo and Jagannath, Ajay and Jain, Adit and
                   Gogia, Guram and Bakshi, Soumyadeep and Rajani, Nazneen},
  year          = {2026},
  eprint        = {2610.17020},
  archivePrefix = {arXiv},
  url           = {https://arxiv.org/abs/2610.17020}
}
```

## Authors & Contributions

This adapter is developed and maintained by
[Himanshu Gupta](mailto:him141195@gmail.com).

**Issues and Contributions:**
- Submit issues and pull requests to the adapters repository.
- Problems with an individual task (instruction, environment or verifier) belong
  upstream: write to cyber@collinear.ai with the task name.
