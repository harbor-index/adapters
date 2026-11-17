# GameDevBench → Harbor Adapter

## Overview

This adapter converts all **333 tasks** in [waynchi/GameDevBench](https://github.com/waynchi/gamedevbench) at `54589a477d6c1a15255707a861db6b01126797aa`. The count comes from 333 starter ZIPs in `tasks/`, each matched to a reference ZIP in `tasks_gt/`. No tasks are excluded. This is Wayne Chi's Godot benchmark, distinct from GameCraft and similarly named projects.

Tasks cover 2D and 3D graphics, animation, user interfaces, and gameplay logic, using Godot scenes, GDScript, shaders, and visual assets. The upstream repository is Apache-2.0 licensed; retain attribution included with task assets. Conversion preserves the official task prompts and formal verifier bytes.

Development status: all 333 tasks are generated and source-audited. The final AP/Docker Harbor oracle passed **333/333**, with zero execution errors, complete unique task coverage, unchanged formal verifier hashes, strict confinement and Godot 4.4.1 on every task. This uses the verified SG shared runtime; the equivalent public recipe independently passed a source-build oracle trial. The initial 325/333 adapter baseline and all failed attempts remain preserved. Model parity, Daytona validation, dataset registration and publication are pending.

## What is GameDevBench?

[GameDevBench](https://waynechi.com/gamedevbench) evaluates agents that edit existing Godot projects to satisfy natural-language requirements derived from tutorials. Each formal task has an executable Godot test. A task receives binary success from the official validator; aggregate performance is the fraction passed. The [paper](https://arxiv.org/abs/2602.11103) describes the benchmark and multimodal feedback settings.

## Adapter Features

- Verifies an embedded manifest of 666 original task ZIPs and upstream runtime files. Git LFS assets must be materialized.
- Reproduces official solver-file filtering and `create_task_prompt`. MCP and runtime-video prompt flags default to false and are recorded.
- Uses Godot **4.4.1**, pinned official Python harness code, and strict Bubblewrap confinement for import and validation.
- Places formal `tasks/` tests in a separate verifier container. Only `/workspace` transfers from the agent. The agent image contains neither private tests nor reference solutions.
- Uses filtered `tasks_gt/` project files only for Harbor's oracle agent, excluding upstream tests.
- Applies an explicit, hash-checked reference-only compatibility profile; `--oracle-profile original` reproduces unmodified GT payloads.
- Records task identity, package and verifier hashes, compatibility actions, outer security state, and official validation results. Identity or hash mismatches fail verification.
- Provides a public Docker recipe, verified by an actual source-build oracle trial, and optional `--runtime-image` for a shared runtime. Internal SG images are deployment assets, not public prerequisites.
- Adds no custom agent implementation. OpenCode parity requires the same agreed, pinned version on both sides before inference.

## Generated Task Structure

```text
datasets/gamedevbench/
├── dataset.toml
├── generation_manifest.json
└── gamedevbench-task-0002/
    ├── task.toml
    ├── instruction.md
    ├── source_manifest.json
    ├── environment/
    │   ├── Dockerfile
    │   ├── docker-compose.yaml
    │   ├── runtime/                  # Public build only
    │   └── project-task_0002/        # Filtered starter project
    ├── solution/
    │   ├── solve.sh
    │   └── project/                  # Filtered reference project
    └── tests/
        ├── Dockerfile
        ├── docker-compose.yaml
        ├── test.sh
        ├── verify.py
        └── payload-task_0002/
            ├── binding.json
            ├── original_task_config.json
            └── official/            # Original test.gd, UID and test.tscn
```

The standalone adapter at `src/gamedevbench/` contains `pyproject.toml`, `uv.lock`, documentation, metadata, and `src/gamedevbench/{adapter.py,main.py,upstream_manifest.json,task-template/}`. Stable task names are `gamedevbench/gamedevbench-task-XXXX`. Dependencies and harness code are shared; starter, verifier, and reference payloads are task-specific.

## Run Evaluation / Harness

Install the pinned Harbor checkout separately. From the adapters repository root, after generating tasks:

    harbor run -p datasets/gamedevbench -a oracle -n 2 --max-retries 0

The reference job configuration is [run_gamedevbench.yaml](run_gamedevbench.yaml):

    harbor run -c src/gamedevbench/run_gamedevbench.yaml

Resume with the saved configuration:

    harbor job resume -p jobs/JOB_DIRECTORY

Oracle needs no model API. The current agent and verifier configurations use `network_mode = "no-network"`. Model runs require an agreed API egress policy and frozen agent/model configuration. The dataset is not registered yet; use local paths.

## Usage: Create Task Directories

Obtain the pinned upstream revision and materialize its Git LFS task ZIPs. Preserve the original ZIPs and source files. From `src/gamedevbench` in the adapters repository:

    uv sync --frozen
    uv run gamedevbench --source-dir /path/to/pinned/gamedevbench --output-dir ../../datasets/gamedevbench

For a diagnostic subset:

    uv run gamedevbench --source-dir /path/to/pinned/gamedevbench --output-dir ../../datasets/gamedevbench-smoke --task-ids task_0002 task_0051 task_0052 task_0059 task_0069 task_0082 task_0089 task_0104

| Flag | Purpose |
| --- | --- |
| `--output-dir` | Destination; defaults to `datasets/gamedevbench` in the adapters repository |
| `--source-dir` | Required upstream snapshot matching the embedded manifest |
| `--limit` | First N selected tasks; N must be positive |
| `--task-ids` | Explicit upstream IDs, such as `task_0002` |
| `--overwrite` | Regenerate existing output belonging to this adapter and revision |
| `--runtime-image` | Existing shared runtime; pin and record its digest for deployment |
| `--workers` | Local packaging threads; defaults to 4; no model calls |
| `--oracle-profile` | `formal-verifier-v1` (default) applies declared oracle-only adjustments; `original` preserves the filtered original GT |
| `--use-mcp` | Upstream MCP-enabled prompt setting; requires matching agent-side tools |
| `--use-runtime-video` | Upstream runtime-video prompt setting; requires matching tools on both sides |

The adapter does not download or silently update upstream data. Conversion uses the standard library; `uv_build` is its build dependency.

## Comparison with Original Benchmark (Parity)

**No model parity has run.** [parity_experiment.json](parity_experiment.json) is intentionally empty. No model scores or inference costs are reported.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Performance | Harbor Performance |
| --- | --- | --- | --- | --- | --- | --- |
| OpenCode version pending | Pending team agreement | Pass rate (mean ± sample SEM) | 0 | 0 | Not run | Not run |

Validation targets Harbor `99218a4611e3bd76e49ce3b5f977e8c8134ad3ac` (0.23.0), the upstream revision above, and Godot 4.4.1. Structural auditing compares prompts with the actual upstream prompt function, agent files with the actual sandbox-filter method, and private tests byte-for-byte with `tasks/`.

Final formal-test oracle evidence: evaluation `gdb-harbor-final-20261117-f1718e9d`, AP job `ap-xts-gamedevbench-harbor-1ec082fd5bb3439f-d2`, 333/333 rewards of 1.0, zero exceptions. The full Harbor job took 27m45s; AP controller including setup and archive preparation took 1746.320 seconds. Original formal test hashes match before and after all 333 trials. Task bundle SHA256: `2d6e4abc5d1e23c896f08bbfa0e13ccd3525dcb43a29c3b152db98793185fdec`; raw AP artifact SHA256: `ecaa513eaad0a0b42268e03292e60d8ffc9270ca9cb23bb72957cac97367aea4`. This is oracle correctness evidence; model parity remains pending.

An earlier AP run passed 333/333 reference projects using each `tasks_gt/` package's own tests. That is a GT integrity check, not model parity or the formal Harbor oracle result. Harbor instead evaluates reference projects against formal `tasks/` tests.

For future original-side parity, use a separate environment at the pinned upstream commit, extract tasks, install the agreed OpenCode version, and supply the agreed YAML task subset. The official entry point is:

    uv run python gamedevbench/src/benchmark_runner.py --agent opencode --model PROVIDER/MODEL --confinement strict run --task-list /path/to/agreed-tasks.yaml

The Harbor entry point, after configuring API egress and model mapping, is:

    harbor run -p datasets/gamedevbench -a opencode -m PROVIDER/MODEL

These are command shapes, not an approved parity configuration. Freeze agent version, provider/model, reasoning settings, prompts, tools, timeout, task subset, resources, image digests, and both commits. Run symmetric sanity checks, then at least two independent repeats per side (three recommended). Report mean pass rate ± sample SEM and retain every raw run and cost record. Formal contribution parity requires the Harbor team's agreed Daytona and 2077AI setup.

## Notes & Caveats

- **Oracle evidence:** see the [terminal screenshot](docs/oracle-terminal-screenshot.png), [captured stdout](docs/oracle-terminal.txt), and [result summary](docs/oracle-summary.json). The screenshot displays saved output from the completed 333/333 run.
- **Upstream reference fixes:** the five oracle-file changes for 0082, 0089, 0107 and 0112 are proposed in [waynchi/gamedevbench#16](https://github.com/waynchi/gamedevbench/pull/16). The PR is open and maintainer acceptance is pending. This adapter keeps the original pinned source and applies the same changes through its hash-checked oracle overlay. The three GT self-check script updates in that PR are not used here: Harbor always runs the unchanged formal `tasks/` tests.
- **Formal tests versus GT tests:** upstream has 11 differing `test.gd` files and 34 differing `test.tscn` files between `tasks/` and `tasks_gt/`. Formal `tasks/` bytes are retained. Resolve any resulting reference or specification issues with recorded evidence rather than substituting GT tests.
- **Declared Linux compatibility:** task 0051 adds a missing `scenes/Main.tscn` alias to `main.tscn`; task 0052 adds `scenes/player.tscn` to `Player.tscn`; tasks 0030 and 0059 default to display-enabled validation. Aliases apply only when absent and the canonical target exists. Original ZIPs, original project files, and verifier bytes stay intact. Each action is recorded.
- **Resolved execution issues:** initial Docker trials had private-file mismatches and Bubblewrap `pivot_root` denial. Unique task context paths and the declared verifier permissions resolved these; the full baseline had no identity/hash/orchestration errors.
- **Oracle-only adjustments:** the formal-verifier-v1 profile changes reference payloads for 0082, 0089, 0107 and 0112. For 0082 it retains all fifteen walk atlas regions and appends two existing frames to each cyclic run timeline to meet the original seventeen-frame check. For 0089 it retains the combined Label and adds the legacy statistic Labels demanded by the unchanged verifier. The 0082/0089 adjustments passed actual strict Harbor validation. The 0107 adjustment removes a premature automatic shader swap so the callable method receives original base materials; 0112 moves the wall frame to the Background rectangle border. All four adjusted references passed actual strict Harbor oracle checks with unchanged verifier hashes. The original task/specification discrepancies remain disclosed. No task prompt, starter project, or formal test changes; original ZIPs and failed baseline records are preserved.
- **Cold build evidence:** a later six-task public-build job exhausted its 1200-second setup budget on every task before grading (6/6 execution errors). Logs show slow dependency downloads and installation. Public generation now allows 3600 seconds for builds; solver and verifier execution budgets are unchanged. AP full-oracle validation uses the previously verified SG shared runtime to remove repeated cold-download variability. The public recipe independently passed a source-build oracle trial.
- **Reference packaging correction:** the initial adapter incorrectly reused the solver blacklist for oracle data and removed legitimate `test_cube.tres`/`test_sphere.tres` resources in 0024, 0025 and 0122. Oracle packaging now retains these original GT resources and excludes actual grader files by exact basename. Agent filtering stays exactly upstream. The 325/333 baseline is preserved as adaptation evidence; these three failures are adapter errors, not upstream GT defects.
- **Isolation:** the separate verifier requests `SYS_ADMIN` and `NET_ADMIN` for official nested namespaces. Its current Docker diagnostic profile also uses unconfined AppArmor and Seccomp. Strict inner validation has no credentials or network. These permissions remain unverified on Daytona; Compose configuration does not establish provider support.
- **Budgets:** agent 600 seconds, matching upstream; verifier 1320 seconds for two official 600-second stages plus overhead. Each environment requests 2 CPUs and 4 GiB. Cold public image builds have a separate 3600-second setup budget; prebuilt-runtime mode keeps 1200 seconds.
- **Storage:** original ZIPs and generated projects occupy several GiB. The large `task_0021` starter is an LFS object; a pointer is invalid input.
- **Optional prompts:** MCP/runtime-video flags preserve upstream prompt settings only. Their complete tools and behavior are not parity-validated yet.
- **Publication:** no adapter PR, registry tag, or Hugging Face evidence upload has been created. Local AP records are development evidence.

## Installation / Prerequisites

Use Python 3.11+ and uv for the adapter, Python 3.12+ for Harbor, and Docker with Compose v2 and Buildx. Allocate disk for projects and image layers. The host must support strict Bubblewrap namespaces inside the verifier.

    cd src/gamedevbench
    uv sync --frozen

Install the pinned Harbor checkout in a separate environment. The public task Dockerfile installs Godot 4.4.1, upstream locked Python dependencies, Bubblewrap, Mesa, Xvfb, ffmpeg, and Node.js. Private SG access is needed only when explicitly selecting internal prebuilt images.

## Troubleshooting

- **Source checksum mismatch:** verify the upstream revision and LFS materialization. Do not change expected hashes to accept unreviewed input.
- **Private task or verifier hash mismatch:** retain trial evidence and inspect build contexts and actual image identities. Keep the identity and hash guards enabled.
- **`pivot_root` or `/proc` permission error:** inspect recorded kernel, capabilities, Seccomp, and AppArmor state. Confirm the provider applies verifier permissions. An agent-only test does not exercise nested validation.
- **Missing reward:** inspect Harbor exceptions and verifier logs. Normal validation writes reward 0 or 1.
- **Display errors:** check Godot 4.4.1 and Xvfb. Display corrections for tasks 0030/0059 are explicit and recorded.
- **Dataset not found in registry:** use generated local paths until publication.

## Citation

```bibtex
@misc{chi2026gamedevbenchevaluatingagenticcapabilities,
  title={GameDevBench: Evaluating Agentic Capabilities Through Game Development},
  author={Wayne Chi and Yixiong Fang and Arnav Yayavaram and Siddharth Yayavaram and Seth Karten and Qiuhong Anna Wei and Runkun Chen and Alexander Wang and Valerie Chen and Ameet Talwalkar and Chris Donahue},
  year={2026},
  eprint={2602.11103},
  archivePrefix={arXiv},
  primaryClass={cs.AI},
  url={https://arxiv.org/abs/2602.11103}
}
```

## Authors & Contributions

Original benchmark: Wayne Chi and the GameDevBench authors above. Adapter development: xts (`xutianshi.xts@alibaba-inc.com`). Generated metadata credits all eleven original authors. Contribution follows the [Harbor-Index workflow](https://www.harbor-index.org/contribute) and [agent guide](https://www.harbor-index.org/tutorial/agent-guide), with contract snapshot `harbor-index/adapters@7b4df9deb2381ae32f26e6cebec06aaa2975dc56`.
