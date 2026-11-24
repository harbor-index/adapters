# GameLogicBench Harbor Adapter

## Overview

**Draft.** The adapter covers 72 tasks and 1,474 scenario/seed cells. Harbor Oracle passed all 72 tasks and all 1,474 cells with zero exceptions. The dataset and parity evidence PRs are pending. See [the current Oracle report](validation/release-72-oracle.json).

Build private Harbor task directories from pinned local GameLogicBench checkouts. The adapter source contains the generator and integration code. It does not distribute upstream games, judges or reference solutions.

## What is GameLogicBench?

GameLogicBench evaluates gameplay logic in Godot 4.4 projects. An agent edits the project, then a deterministic judge runs the submission across public and hidden scenarios. The pinned upstream snapshot contains 72 tasks and 1,474 scenario/seed cells. The paper reports 1,451 cells. The reason for the count difference has not been confirmed.

## Adapter Features

- One stable Harbor task per supported original task ID. Full and parity generation use the same conversion path.
- The agent receives the upstream brief and playable project. The verifier runs the frozen judge and the submitted controller under separate users and processes.
- Oracle installs the upstream `proper` overlay into the editable project and goes through the normal artifact and verifier path. Use `--reference naive` in a separate local directory for negative calibration.
- Headless validation uses the pinned Godot 4.4 runtime. The manifest preserves Press arguments, per-seed resource settings and declared delivery paths.
- The runtime selects the official Godot AMD64 or ARM64 archive for the Docker build platform and verifies its pinned checksum. Tasks with the upstream native GDExtension pin the game and verifier services to AMD64; other tasks and Harbor's sidecar can run natively on ARM64.

## Generated Task Structure

```text
<output>/<original_task_id>/
  task.toml
  instruction.md
  LICENSE, LICENSE-CC-BY-4.0, LICENSE-HARNESS, NOTICE.md
  environment/Dockerfile
  environment/game/
  solution/solve.sh
  solution/reference/
  tests/Dockerfile
  tests/test.sh
  tests/verify.py
  tests/private/{task.yaml,evaluation.json,game/,judge/}
```

## Licensing and attribution

GameLogicBench is sourced from NJU-LINK. The upstream harness and the original benchmark code are MIT. Task text, task specifications, and documentation are CC BY 4.0. The pinned task repository's notice identifies third-party material and the license files retained with it. [Task repository notice](https://github.com/NJU-LINK/GameLogicBench-Tasks/blob/fcc0b01fcc2fd2d95998478152222e6ab916fcdf/NOTICE.md)

**Non-commercial game resources.** The `repo_turbofat_piece_phase` task includes CC BY-NC 4.0 game resources. Its upstream audio was replaced by silent placeholders. The YouTD2 tasks use placeholder art and sound and preserve the upstream license for the remaining original text files.

## Run Evaluation / Harness

The registry entry is pending. The [publication-package Oracle report](validation/release-72-github-oracle.json) records 72/72 full rewards and 1,474/1,474 passing cases. Each executed task digest matches the manifest submitted in [dataset PR #302](https://github.com/harbor-framework/harbor-datasets/pull/302/commits/b4fa685c0618e3fb34cd155730208a3f0ffd295e). Generated task Dockerfiles include the runtime build and can run on a clean Docker backend.

The [51-task reference validation](validation/release-51-reference.json) records
51/51 full Harbor Oracle rewards (1,055/1,055 cells), 51/51 zero rewards for
the unchanged workspace, and 148/148 original-to-Harbor verdict agreements
for proper and naive reference solutions on three representative tasks.
The generated registry candidate also passed all 51 tasks and 1,055 cells in
Harbor 0.27.0, with zero exceptions or retries.

```bash
harbor trial start -p ../../datasets/gamelogicbench-poc/atom_active_frames -a oracle
harbor run -c run_gamelogicbench.yaml
```

The bundled run config targets the full generated dataset. It starts two concurrent trials and uses sequential seed evaluation within each verifier.

## Usage: Create Task Directories

Run from the adapter directory. Set the paths to the pinned upstream repositories first.

```bash
uv run gamelogicbench --harness-dir "$GLB_HARNESS" --tasks-dir "$GLB_TASKS" \
  --output-dir ../../datasets/gamelogicbench-poc \
  --task-ids atom_active_frames combo_reverse_dock repo_watch_rotation

uv run gamelogicbench --harness-dir "$GLB_HARNESS" --tasks-dir "$GLB_TASKS" \
  --output-dir ../../datasets/gamelogicbench
```

Use `--limit N` to select a sorted prefix and `--overwrite` to regenerate tasks in place. To avoid mixing subsets, use a clean output directory for each independent run. Generated folders include the benchmark materials and the upstream notices. Preserve those notices and the non-commercial label when staging the dataset.

## Comparison with Original Benchmark (Parity)

The full release compares 24 of 72 tasks across three paired rounds. The earlier
20 tasks were drawn from the 51-task release by ranking SHA-256 hashes of
`gamelogicbench-parity-release-51:<task_id>` within each task category. We reuse
those rounds after rescoring 60 saved Harbor submissions with the expansion
verifier. All 1,314 case verdicts match. The four additions cover rendered lighting,
live character physics, a single-module replacement, and a multi-file replacement.
Each addition ran on both sides three times through the reviewer API.

Both sides used Codex 0.144.1 with GPT-5 mini, high reasoning effort and a
3,600-second active solve budget. The overall pass-rate ranges meet at 8.33%.
The scores below are means with sample standard errors.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Performance | Harbor Performance |
| --- | --- | --- | ---: | ---: | --- | --- |
| codex@0.144.1 | gpt-5-mini | Task pass rate (%) | 3 per side | 24/72 (33.3%) | 11.1111 ± 1.3889 | 6.9444 ± 1.3889 |

Generate the [fixed 24-task subset](src/gamelogicbench/parity_task_ids.txt)
from the pinned checkouts. Use the agreed reviewer gateway and keep its API key
private. The reproduction command starts one Harbor round.

```bash
uv run gamelogicbench --harness-dir "$GLB_HARNESS" --tasks-dir "$GLB_TASKS" \
  --output-dir ../../datasets/gamelogicbench-parity --split parity \
  --allow-host app.ppapi.ai --runtime-image gamelogicbench-parity:codex-0.144.1

harbor run -c run_gamelogicbench_parity.yaml --job-name gamelogicbench-parity-round-1
```

### Historical 72-task comparison

The historical provisional cohort contains three paired runs on 24 tasks (8 atom / 8 combo / 8 repo, 472 cells per run). Scores are mean ± sample SEM; all per-kind and overall run-score ranges overlap. The Harbor cohort mixes driver revisions, so these observations are not final frozen-configuration parity confirmation.

All seven historical whole-task disagreement pairs were cross-graded in the opposite evaluator: **14 submissions, 294/294 matching cell verdicts**. These replays describe the historical cohort. See [acceptance findings](validation/acceptance.md) and [cross-grade evidence](validation/crossgrade-disagreements.json).

The pilot is excluded. Model-budget timeouts are retained and judged; four unusable attempts are archived and replaced. Counting the oversized-tool-argument attempt's original zero leaves the reported aggregate unchanged. See the linked reproduction notes for the complete policy and driver history.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Performance | Harbor Performance |
| --- | --- | --- | ---: | ---: | --- | --- |
| codex@0.144.1 | gpt-5-mini | Task pass rate (%) | 3 per side | 24/72 (33.3%) | 13.8889 ± 2.7778 | 18.0556 ± 3.6747 |

See [per-run and per-task summaries](validation/parity-results.json), [the historical task list](validation/parity_task_ids.historical.txt), and [historical reproduction instructions](validation/parity-reproduction.md). The historical experiment ran in Colima Linux.

## Notes & Caveats

- The upstream harness starts a container per seed. The adapter starts one separate verifier container per trial and isolates sequential seed executions inside it. Check the target backend's isolation and egress policy before using the adapter with models.
- The final candidate includes the 21 tasks excluded from the first release. The [historical exclusion list](excluded_tasks.json) describes the first 51-task candidate.
- The verifier supervisor writes the task reward using the trusted judge's results. Missing or unusable judge results produce an explicit infrastructure failure.
- Verifier logs include Godot diagnostics and a private `cells.jsonl` with every scenario/seed verdict. Keep these detailed logs private; publish only reviewed summaries and terminal screenshots.
- Source checkouts must match the fixed SHAs below and have clean tracked files. Generation copies Git-tracked assets only.
- NJU-LINK approved public Harbor hosting at the pinned revisions. Preserve the notices and licenses listed above in every published task package.
- Confirm the task registry namespace and contributor contact through the v2 review workflow.

## Installation / Prerequisites

Use Python 3.11+, uv, Git, and a Linux Docker backend with the pinned Harbor release or checkout. The current adapter specification describes Harbor baseline `99218a4611e3bd76e49ce3b5f977e8c8134ad3ac`.

```bash
git clone https://github.com/NJU-LINK/GameLogicBench.git
git -C GameLogicBench fetch origin 979cd919689c8bba021ef56808887bc58e49f3db
git -C GameLogicBench checkout --detach 979cd919689c8bba021ef56808887bc58e49f3db
git clone https://github.com/NJU-LINK/GameLogicBench-Tasks.git
git -C GameLogicBench-Tasks fetch origin fcc0b01fcc2fd2d95998478152222e6ab916fcdf
git -C GameLogicBench-Tasks checkout --detach fcc0b01fcc2fd2d95998478152222e6ab916fcdf

uv sync --locked
```

Set `GLB_HARNESS` and `GLB_TASKS` to the respective absolute paths. The runtime Dockerfile pins the base image and checks the exact Godot version and archive checksum. Use `--runtime-image` to target an equivalent prebuilt image accessible from a different backend.

## Troubleshooting

- If generation rejects a checkout, verify its head, root and tracked status.
- If a task built with `--runtime-image` cannot resolve the image, regenerate without the override or use an image reachable from the Docker backend.
- For a failed Oracle, compare the generated verifier log with the separately evaluated upstream `proper` solution. Start with `atom_move_navigation` to isolate headless operation.
- Confirm the Linux Docker backend supports separate verification, no-network execution and the egress sidecar’s required kernel features for allowlists.

## Citation

```bibtex
@article{che2026gamelogicbench,
  title = {GameLogicBench: Evaluating Coding Agents on Runtime Game Logic with Tick-Level State Assertions},
  author = {Che, Xinyu and Ge, Yunfei and Li, Shihao and Liu, Yanchen and Yan, Hang and Lei, Xinping and Wang, Yanghai and Dong, Zixuan and Yao, Yifan and Xie, Qianqian and Zhu, Letian and Liu, Jiaheng},
  journal = {arXiv preprint arXiv:2609.21562},
  year = {2026}
}
```

## Authors & Contributions

The benchmark is by Xinyu Che and colleagues at NJU-LINK. The Harbor adapter was prepared by ShawVentus with Codex.
