# RoboDojo → Harbor Adapter

## Overview

This draft adapts the [RoboDojo](https://robodojo-benchmark.com/) simulation track to Harbor in **Direct mode**: a vision model controls the robot. For every observation the model sees the native task instruction, the camera frames and the measured robot state, and answers with one bounded end-effector motion for both arms. The adapter generates 42 canonical task directories.

Nothing in this adapter has run on a GPU yet. The code is exercised by CPU tests with a fake simulator and a fake model, and the CPU mock task has run through Harbor and Docker Compose. GPU execution, a passing oracle, parity experiments, and registry publication are pending, and no score is claimed.

The simulator is pinned to [RoboDojo `f57e1633ffed59761bc8e568cfe7530c4b33381c`](https://github.com/RoboDojo-Benchmark/RoboDojo/tree/f57e1633ffed59761bc8e568cfe7530c4b33381c), whose XPolicyLab gitlink is `5d0be1499634d3176258c8f2e31f5fe816497b0a`.

## What is RoboDojo?

[RoboDojo](https://arxiv.org/abs/2607.04434) evaluates generalist robot manipulation policies in simulation and on physical robots. Its simulation track contains 42 bimanual tasks on ARX X5: Generalization (12), Memory (6), Precision (8), Long-Horizon (8), and Open (8). All 18 real-world tasks are excluded from this adapter.

The simulation uses Isaac Sim, Isaac Lab, CuRobo, and XPolicyLab. Evaluation reports success rate and a partial-progress score. Generalization tasks combine 25 standard and 25 randomized episodes; each other task uses 50 episodes. The 12 randomized counterparts bring the runtime inventory to 54 configurations, not 54 canonical tasks. The overall benchmark score averages the five dimensions equally.

## Adapter Features

- Generates stable Harbor task names with task selection, limits, and overwrite control.
- Runs the unchanged native evaluation. Direct mode is installed as one more XPolicyLab policy, `HarborDirect`, so the native loop keeps ownership of layouts, invalid-layout replacement, episode termination, reward checks, and the `_result.json` files.
- Provides `robodojo.direct_agent:DirectAgent`, a Harbor agent that calls a vision model through one tool and forwards each decision to the simulator.
- Validates every decision next to the simulator against the measured robot pose. A rejected decision executes nothing and can be corrected.
- Writes `reward.json` with `success_rate` and `score`, both in `[0, 1]`. The native percentage score is divided by 100; no partial-progress threshold is introduced.
- Reports a reward only for a complete evaluation. Aborted, incomplete, or inconsistent runs produce no reward instead of a zero.
- Provides aggregation across all 42 tasks with equal weighting of the five dimensions.

### How a trial runs

Each task starts two containers on a private Compose network:

| Container | Contents | Role |
|-----------|----------|------|
| `main` | `python:3.12-slim` plus `bridge_client.py` | The only place Harbor executes agent commands. It has no assets, no simulator code, and no grading output. |
| `simulator` | Pinned RoboDojo image plus this adapter's policy, broker, and supervisor | Runs the native policy server and the native simulator client on the GPU, and owns all results. |

Harbor applies the `[environment]` resources of `task.toml` to `main` only, so they describe the small agent container: 1 CPU, 2 GB of memory, and no GPU. The simulator's limits are set on the `simulator` service in the generated `docker-compose.yaml`: 8 CPUs, 64 GB of memory, and one NVIDIA GPU. The same values are repeated under `[metadata]` in `task.toml` for reference. They are provisional.

1. The supervisor in `simulator` starts the native policy server for `HarborDirect`, then the native simulator client once per variant, with `--eval-num native`.
2. For each episode the native loop calls the policy's `eval_one_episode` hook. The hook posts the native observation to a broker inside the policy server and waits for a decision. Physics advances only while an accepted decision is executing.
3. `DirectAgent` runs on the Harbor host. It fetches the observation through `bridge_client.py` in `main`, calls the vision model, and submits the decision. Model credentials stay on the host.
4. The simulator converts the end-effector targets to joint commands with a robot-only local inverse-kinematics update and steps the native environment.
5. When all episodes are done, Harbor copies `/harbor-robodojo/results` out of the `simulator` container. A separate verifier container validates those files and writes the reward.

The agent container cannot write the files the verifier reads. The verifier also requires that every natively scored episode matches exactly one Direct episode in the broker's ledger, with the same layout and outcome, ended by native termination, and with every control step covered by an accepted decision.

### Action contract

A decision has `request_id`, `mode` (`eef`), `steps`, `reason`, and a `target` for `left` and `right`. Each target has a `position` in meters, a unit `quaternion_wxyz`, and `gripper_closed`. Poses refer to each arm's `link6` in the shared environment-origin frame.

| Limit | Value |
|-------|-------|
| Target distance from the measured pose | 0.05 m and 0.35 rad per arm |
| Control steps per decision | 1 to 5, at the native 25 Hz |
| Inverse-kinematics update per control step | 0.02 m, 0.1 rad, and 0.05 rad per joint |

The contract and the inverse kinematics follow the public Direct controller of [GPT-as-Policy](https://github.com/anonymous-report-421/GPT-as-Policy/tree/8f3d362b077d8efb77e2a7274d5b2c20e2243846) (MIT). `direct/kinematics.py` is adapted from that code; its license is in `direct/GPT_AS_POLICY_LICENSE`.

### Model scaffold

The model gets one tool, `robodojo_act`, and no shell, file, or image tools. An answer counts as a decision only if it is exactly one call of that tool; a call of any other name, or more than one call, executes nothing and is returned to the model as an invalid answer. Parallel tool calls are also switched off in the API request where the provider supports it. The system prompt in `direct/prompts.py` explains the robot, the observation fields, and the action limits. It adds no task-specific hints; the task instruction is the native one, passed through unchanged. Model history is reset at the start of every episode.

| `DirectAgent` option | Default | Meaning |
|----------------------|---------|---------|
| `image_history` | `1` | Number of most recent observations that keep their camera frames in the prompt. |
| `history_window` | `0` | Number of most recent observations kept in the prompt; `0` keeps the whole episode. |
| `max_invalid_responses` | `5` | Consecutive invalid answers before the scaffold holds both arms for one step. The hold is recorded as a scaffold action, not a model decision. |
| `max_decisions_per_episode` | `0` | Safety limit; `0` is unlimited. Reaching it aborts the run. |
| `max_cost_usd` | none | After each response, abort before sending its action if the accumulated cost reaches the limit or the price is unknown. The provider may already have charged for that response. |
| `temperature`, `reasoning_effort`, `max_output_tokens`, `model_seed` | none | Passed to the model API when set. |
| `tool_choice` | `required` | Use `auto` for models that reject a forced tool call. |
| `api_retries` | `4` | Number of additional attempts per completion. Set to zero for one attempt. The player records API attempts and successful model calls separately. |
| `api_base`, `api_key_env` | none | Endpoint override and the name of the environment variable that holds the API key. |

The model, the prompt hash, the contract version, and these settings are recorded in each trial's agent metadata. An aborted run has no reward: a limit that stops an episode early is never reported as a native failure.

The metadata also records where the requests went, without credentials:

- `endpoint.api_base` is the configured `api_base`, reduced to scheme, host, port, and path. User names, passwords, query strings, and API keys are never recorded.
- `endpoint.environment` holds the `<PROVIDER>_API_BASE` and `<PROVIDER>_BASE_URL` variables for the model's provider when set without an explicit `api_base`. For an unprefixed model such as `gpt-4o`, LiteLLM resolves the provider.
- `served` holds what the responses reported: the model identifiers that answered and, where LiteLLM states them, the provider and endpoint.

For parity runs, set `api_base` explicitly. A redirect made in any other way, such as a provider variable with a different name or a proxy behind the same host, is only visible if it changes the served model identifier.

## Generated Task Structure

```text
datasets/robodojo/
└── <canonical-task-id>/
    ├── task.toml
    ├── instruction.md
    ├── environment/
    │   ├── Dockerfile              # agent container
    │   ├── simulator.Dockerfile    # simulator container, from the pinned image
    │   ├── docker-compose.yaml
    │   ├── bridge_client.py
    │   └── simulator/              # policy, broker, supervisor, task_spec.json
    ├── solution/
    │   └── solve.sh
    └── tests/
        ├── Dockerfile              # verifier container
        ├── test.sh
        ├── task_spec.json
        └── robodojo/               # result validation only
```

The simulator container uses a locally prepared pinned image and mounts existing assets read-only. `solution/solve.sh` exits with an explicit message because no passing oracle has been provided.

## Run Evaluation / Harness

Registry execution is unavailable: this draft has not been published. Use local generated tasks during development.

From the adapters repository root, install this package with its optional Harbor dependency:

```bash
uv sync --project src/robodojo --extra harbor
```

Use the project environment for execution so Harbor can import the custom agent. Record the actual Harbor revision used for experiments; this adapter was developed against `harbor==0.26.0`.

Set the model in `src/robodojo/run_robodojo.yaml`, export the provider's API key in the shell that runs Harbor, then run:

```bash
uv run --project src/robodojo --extra harbor harbor run -c src/robodojo/run_robodojo.yaml
```

The model name in the configuration is a placeholder and must be replaced with a vision model that supports tool calls. The configuration uses one concurrent trial because each trial needs the GPU. Because the GPU is requested by the `simulator` service and not through `task.toml`, Harbor does not check for it in advance: on a host without a usable NVIDIA runtime the trial fails when Docker starts the simulator. Its default is the Direct agent rather than oracle: no validated oracle is available, so the standard oracle-default template cannot honestly be used.

Tasks need a Compose-capable Harbor environment with an NVIDIA GPU, because the verifier reads artifacts from the `simulator` service.

After a complete run, aggregate the Harbor job directory from this adapter directory:

```bash
cd src/robodojo
uv run python -m robodojo.aggregate /path/to/harbor-job --output /path/to/summary.json
```

Aggregation requires exactly 42 unique completed tasks that share the layout seed group, the model, the inference endpoint, the prompt hash, and the scaffold settings. Every trial must have been answered by exactly one served model identifier, and that identifier must be the same for all trials. It rejects failed or incomplete trials and does not report a full-benchmark score for a subset. Aggregate repeated runs separately before calculating parity mean and sample SEM. A flat average of 42 Harbor task rewards is not the official overall RoboDojo score.

### CPU mock without the simulator

The adapter includes a mock for checking the plumbing on a machine with no GPU. It is **not a RoboDojo task**. A small service draws an invented table scene with NumPy: two grippers, a red disc, and a blue disc, seen by a top-down head camera and two wrist cameras. The task is to put each gripper over its disc, lower it, and close it.

The mock replaces only the simulator. The broker, the bridge client, the action contract, the episode loop, `DirectAgent`, and the verifier are the real ones.

Run it on one machine, with no Docker and no API key, using the scripted solver:

```bash
cd src/robodojo
uv run --extra harbor --group dev python -m robodojo.mock.local --episodes 2 --output /path/to/new-directory
```

The command prints the mock reward and saves the pictures of every observation under `observations/` in the output directory. To let a real vision model play, add `--model provider/model-name`, and optionally `--api-base` and `--max-cost-usd`. This calls the model API and costs money; the key is read from the environment as in a Harbor run. Two episodes need roughly 6 to 40 model calls with three small pictures each.

To also test the model API path without a key, add `--fake-openai` instead of `--model`. The real `DirectAgent` and the real LiteLLM client then talk over HTTP to a local fake of the OpenAI Chat Completions endpoint. The fake checks every request as the real endpoint would (tool definition, forced tool call, PNG data URLs, tool-call and tool-message pairing, no unknown parameters, the bearer key) and answers in the OpenAI response format with a move planned by the scripted solver. It shows that the request and response formats fit. It is not a model, so it says nothing about real token counts, prices, or whether a model can read the pictures.

To exercise Harbor and Docker Compose as well, generate the mock task and run it:

```bash
uv run robodojo --output-dir ../../datasets/robodojo-mock --mock --mock-episodes 2
uv run --extra harbor harbor run -p ../../datasets/robodojo-mock/mock-reach-targets -a oracle
uv run --extra harbor harbor run -p ../../datasets/robodojo-mock/mock-reach-targets \
  -a robodojo.direct_agent:DirectAgent -m provider/model-name
```

The oracle of the mock task is the scripted solver, so the first `harbor run` needs no model. The second calls a real model. To run `DirectAgent` in Harbor without a key, serve the fake endpoint in another terminal and point the agent at it:

```bash
export ROBODOJO_FAKE_OPENAI_KEY=any-throwaway-value
uv run --extra harbor --group dev python -m robodojo.mock.fake_openai --port 8765
```

```bash
export ROBODOJO_FAKE_OPENAI_KEY=any-throwaway-value
uv run --extra harbor harbor run -p ../../datasets/robodojo-mock/mock-reach-targets \
  -a robodojo.direct_agent:DirectAgent -m openai/fake-vision-model \
  --ak api_base=http://127.0.0.1:8765/v1 --ak api_key_env=ROBODOJO_FAKE_OPENAI_KEY --ak api_retries=0
```

On November 17, 2026 the oracle run and the fake-endpoint run both completed on an Apple M1 with Docker 29.5.2 under Colima and Harbor 0.26.0: one trial each, two episodes, six decisions, no exception, mock reward 1.0 from the separate verifier container. The run with a real model has not been done. These runs show that the three containers build, start, reach each other, and hand their files to Harbor. They are not benchmark evidence.

What the mock does not cover: Isaac Sim, the pinned image and its GPU setup, the native policy WebSocket, the robot's inverse kinematics, native recovery from invalid layouts, and the difficulty of any real task. A model that solves the mock has shown nothing about RoboDojo.

Mock results are kept apart from benchmark results. The task is named `robodojo-mock/reach-targets`, its specification and its results carry `"mock": true`, the native supervisor refuses the mock task, the verifier refuses mock output for a benchmark task, and aggregation rejects mock trials. Do not register the mock task or use it as parity evidence.

## Usage: Create Task Directories

Run from `src/robodojo`. `--assets-dir` must be an absolute path to an existing local `Assets/` directory:

```bash
uv sync
uv run robodojo \
  --output-dir ../../datasets/robodojo \
  --assets-dir /absolute/path/to/RoboDojo/Assets
```

The default image is `robodojo-harbor:f57e1633ffed`, and the default seed is `0`. For a subset:

```bash
uv run robodojo \
  --output-dir /path/to/task-output \
  --assets-dir /absolute/path/to/RoboDojo/Assets \
  --image robodojo-harbor:f57e1633ffed \
  --seed 1 \
  --task-ids stack_bowls play_Xylophone \
  --limit 2 \
  --overwrite
```

`--task-ids` accepts canonical upstream identifiers, preserving their original case and underscores. Randomized variants belong to their Generalization task and are not selected separately. The seed selects the layout seed group; it must be nonnegative and have matching layout assets. Regenerate tasks when changing the image, seed, or absolute asset path.

## Comparison with Original Benchmark (Parity)

No parity experiments have run. `parity_experiment.json` is an empty array, not estimated results. There are no matching-score claims, oracle screenshots, dataset PRs, or uploaded parity artifacts.

| Agent | Model | Metric | Runs | Dataset Size | Original (mean ± SEM) | Harbor (mean ± SEM) |
|-------|-------|--------|------|--------------|-----------------------|---------------------|

RoboDojo's published results are for learned policies, and the GPT-as-Policy paper evaluates a different scaffold (a Codex agent with shell, image, and note tools) on a 60-case panel of 10 tasks. Neither is a baseline for this scaffold. Parity here means the same `DirectAgent`, model, prompt, settings, pinned sources, layout seed group, and episode counts, once outside Harbor and once through Harbor. Agree the subset and the number of runs with the Harbor team before spending on model calls. The adapter contract requires at least two runs per side.

For the original side, generate a fresh task with this adapter version. In the first terminal on the GPU host, activate the native RoboDojo environment and start its supervisor. Use the pinned upstream checkout with its assets and the two revision markers created by the image preparation script. Set `HARBOR_ROBODOJO_HOME` to the generated task's `environment/simulator` directory. It contains the matching spec, supervisor, and policy.

```bash
export HARBOR_ROBODOJO_HOME=/absolute/path/to/generated-task/environment/simulator
export HARBOR_ROBODOJO_RESULTS=/absolute/path/to/attempt-01-results
PYTHONPATH="$HARBOR_ROBODOJO_HOME${PYTHONPATH:+:$PYTHONPATH}" python -m robodojo.direct.supervisor \
  --root /absolute/path/to/RoboDojo --results-dir "$HARBOR_ROBODOJO_RESULTS" --exit-when-done
```

The supervisor installs the policy for the attempt, runs each native variant, and serves the driver on loopback. At completion it waits up to 30 seconds for delivery of the final verdict, stops its policy server, and removes the policy it installed. Use a fresh results directory for each repetition. A pre-existing installation at `XPolicyLab/policy/HarborDirect` prevents startup.

In a second terminal on the same host, run the model driver from the adapter directory. Use the same absolute spec and results paths as the supervisor and a separate empty output directory.

```bash
cd src/robodojo
uv run --frozen --extra original python -m robodojo.original \
  --model provider/model-name --api-base https://api.provider.example/v1 \
  --bridge-url http://127.0.0.1:19100 --output /absolute/path/to/attempt-01-player \
  --spec /absolute/path/to/generated-task/environment/simulator/task_spec.json \
  --results-dir /absolute/path/to/attempt-01-results
```

Both sides use `DirectPlayer` with the same prompt, tool, and option defaults. The driver reads its API key from the environment. Before inference, it matches the live bridge's attempt ID to the results directory. Before grading, it matches accepted requests and episode ends in the player's log to the broker ledger. After successful verification, `original_side.json` records the attempt ID, model provenance, native totals, and comparable reward.

No original-side command has run against the real simulator. On the CPU mock, a Harbor trial in Docker and the driver outside Harbor were run with the same seed and the fake model endpoint: the recorded provenance and counters were identical, the six decisions were identical, and both rewards were 1.0. This checks that the two sides execute the same loop. It is not a parity result.

For the Harbor side, regenerate tasks with the matching seed, run `run_robodojo.yaml`, and use `robodojo.aggregate`. Preserve raw per-episode results on both sides, then populate the parity JSON, raw run arrays, the comparison table, and artifact links.

## Notes & Caveats

- This is a draft, not a review-ready adapter. A passing oracle, GPU execution, parity evidence, and dataset registration remain outstanding.
- **Not validated on a GPU.** The simulator-side code has only run against fakes. On the first GPU run, check the camera frames, the measured poses, the kinematics check against `link6`, the registration of native completion conditions, and one bounded action before paying for model inference. The episode loop aborts if the kinematics check fails or if no completion condition is registered.
- **Scale.** The pinned task step limits range from 200 to 1,900 control steps per episode, which bounds one full benchmark run at 1,640,000 control steps. At five steps per decision that is up to 328,000 model calls, each with three camera frames, and between 2,000 and 19,000 calls for a single task. Episodes that succeed early need fewer. No wall time, token count, or cost has been measured.
- The 604,800-second agent timeout and the simulator resources are provisional and need calibration on the target GPU.
- All 18 real-world tasks are excluded: physical hardware, scene resets, and the remote evaluation service are not packaged.
- No upstream prompt, reward, physics, or episode-count modifications are intended. Native observations reach the policy server unchanged. The model receives the instruction, one lossless PNG per native camera, the joint state, and the measured end-effector poses; it never receives object poses, rewards, or progress scores.
- Direct mode sends joint commands computed by this adapter's local inverse kinematics. It does not use RoboDojo's native end-effector action route, which solves inverse kinematics with CuRobo.
- With the default `image_history` of 1, the model sees only the current frames and must carry earlier observations in its own `reason` text. This matters most for Memory tasks, where a demonstration is shown early in the episode. Raise `image_history` to keep more frames at a higher cost.
- The scaffold is narrower than the agent in the GPT-as-Policy paper. It also omits that project's task notes, one of which describes an instruction that the pinned RoboDojo revision no longer uses. Results from the two are not comparable.
- A lost acknowledgement is not retried. If a call that submits an action fails in transport, the agent stops the run, because it cannot know whether the action executed.
- Native recovery from a physics fault abandons the affected layout and continues with a replacement, as it does for learned policies. The broker records the interrupted episode as unscored. A fault that restarts the simulator process has not been exercised and may leave the run incomplete, which yields no reward.
- A trial that stops partway cannot be resumed; the task must be run again from its first episode.
- The layout seed group, the layout index, and the model's `model_seed` are separate values and are recorded separately.
- Any Harbor agent can drive the robot through `bridge_client.py`, as `instruction.md` describes, but only `DirectAgent` has been written for it. Isolation rests on Compose service boundaries and has not been reviewed against an adversarial agent with root in `main`.
- The operator supplies existing assets. This adapter does not download assets or accept asset terms on the operator's behalf.
- The optional image script builds the upstream Dockerfile, including its upstream license-consent environment variables. Review the pinned Dockerfile and applicable terms before building.
- Generated tasks embed an absolute host asset path. Assets are mounted both at `/workspace/RoboDojo/Assets` and their original host path to resolve CuRobo configurations with embedded absolute paths. Regenerate tasks for another host.
- The image preparation script adds revision markers and the OCI source-revision label. These support consistency checks; they are not a secure provenance attestation or an immutable image digest.

## Installation / Prerequisites

Task generation needs Python 3.12+ and `uv`. Simulator execution needs Linux x86_64, an NVIDIA GPU with driver 570 or newer, Docker, and NVIDIA Container Toolkit. The upstream image contains Isaac Sim 5.1, Isaac Lab, and CuRobo. Upstream estimates an approximately 200 GB image and recommends 300 GB free disk including build cache. A Mac can generate tasks but cannot run this CUDA simulator locally.

The model runs through a remote API, so the GPU is used for simulation only. No policy checkpoint or second GPU is needed.

On the GPU host, run from `src/robodojo`:

```bash
bash scripts/prepare_image.sh
# Optional image tag and new checkout directory:
bash scripts/prepare_image.sh robodojo-harbor:f57e1633ffed /path/to/new/RoboDojo-checkout
```

The script clones the pinned source and submodules, builds the upstream image, and adds revision markers in a separate layer. Existing checkout directories must be clean and at the exact pinned revision; they are never reset. It does not download `Assets/`.

Prepare assets separately using the [pinned upstream asset instructions](https://github.com/RoboDojo-Benchmark/RoboDojo/blob/f57e1633ffed59761bc8e568cfe7530c4b33381c/scripts/init_assets.sh). Record the dataset revision and path preparation. Assets must include robots, objects, materials, evaluation layouts for the chosen seed, and the support trajectories that some tasks replay.

Run the local adapter checks from `src/robodojo`:

```bash
uv run --extra harbor --group dev python -m unittest discover -s tests -v
```

These checks cover generation, the action contract, kinematics on a synthetic arm, the broker, the episode loop, the agent, result validation, and the CPU mock. They use a fake or mock simulator and a fake model, so they do not prove that the GPU simulator runs or that any model can solve a task.

## Troubleshooting

- **Missing Docker or GPU:** generate locally, then regenerate and execute on a supported Linux NVIDIA host. Check `nvidia-smi` and NVIDIA Container Toolkit.
- **Agent setup times out:** the bridge answers only after the native policy server has started in the `simulator` container. Read `logs/policy_server.log` in the collected results, or the container log.
- **Observations never arrive:** Isaac Sim startup and scene resets can take minutes. `DirectAgent` gives up after `max_idle_sec` (default 7,200 seconds). Read `logs/<variant>.log` in the collected results.
- **`Kinematics validation failed`:** the URDF kinematics do not reproduce the measured `link6` pose. Do not loosen the tolerance; check the robot configuration against the pinned revision.
- **`Native task completion conditions are empty`:** the native reward conditions were not registered for this task. The run stops rather than report an immediate success.
- **Model rejects the request:** try `tool_choice: auto`, or remove options the provider does not support. Unsupported optional parameters are dropped by the client library.
- **Missing assets or robot paths:** verify the asset directory, the selected seed's layouts, and absolute CuRobo configuration paths. Regenerate tasks after changing the host asset path.
- **No reward:** read the verifier log and the collected `supervisor.json`. Aborted and incomplete evaluations have no reward by design; do not fill missing episodes or present partial suites as complete.
- **Oracle fails immediately:** expected until a real reference policy is provided and validated.

## Citation

```bibtex
@article{chen2026robodojo,
  title={{RoboDojo}: A Unified Sim-and-Real Benchmark for Comprehensive Evaluation of Generalist Robot Manipulation Policies},
  author={Chen, Tianxing and Chen, Yue and Li, Zixuan and Tang, Junyuan and Su, Kailun and Wan, Weijie and Chen, Baijun and Lu, Haoran and Yan, Haowen and Su, Honghao and others},
  journal={arXiv preprint arXiv:2607.04434},
  year={2026}
}
```

## Authors & Contributions

The [original RoboDojo authors](https://github.com/RoboDojo-Benchmark/RoboDojo) provide the benchmark, task definitions, simulator, and scoring. XPolicyLab provides the native policy deployment interface. The Direct action contract and the local inverse kinematics follow [GPT-as-Policy](https://github.com/anonymous-report-421/GPT-as-Policy/tree/8f3d362b077d8efb77e2a7274d5b2c20e2243846) by Yu-Mool Shu and Lipxin Zheng. Adapter builder: Jia Guo (`jia@green.microsoft.com`). This draft contributes Harbor packaging, the Direct agent, the simulator bridge, native-evaluation orchestration, result validation, and dimension-aware aggregation; no benchmark results are claimed.
