# EmbodiedLab → Harbor Adapter

## Overview

This adapter converts [EmbodiedLab](https://github.com/HexaFuture/EmbodiedLab) 0.2.1 into Harbor tasks. In each task
a robot agent runs its own experiment in a physics-based laboratory (NVIDIA Isaac Sim) to determine a quantity it
cannot look up: it operates a Franka arm through a Python API, reads the instruments through three cameras and
submits a value with the evidence it recorded.

- **Adapted:** 32 tasks, one per experiment, at interface level T0 (raw control), on one public dev instance each.
  28 are determination experiments (a hidden quantity within a tolerance) and 4 are optimization experiments (the
  best result the apparatus allows).
- **Not yet included:** `truss_bridge`, `lens_focal` and `optical_relay`, until they have a validated oracle replay
  (see Notes & Caveats). Interface level T1 is not adapted.
- **Reward:** 1 when the EmbodiedLab evaluator marks the task completed, else 0, so the mean reward is the success
  rate, the benchmark's headline metric. The verifier also writes the process score (0 to 1) to `details.json`.
- **Hardware:** one RTX-class GPU with 24 GB per trial (Isaac Sim renders with RT cores).

## What is EmbodiedLab?

EmbodiedLab ([paper](https://arxiv.org/abs/2610.35385)) benchmarks embodied agents that *act to know*: the agent
gets an objective and the apparatus, with no method, changes the state of a physical system, observes the feedback
and keeps exploring until it knows what the objective asks. It has 35 quantitative science experiments in mechanics,
circuits, optics, thermal physics and chemistry. The hidden parameters of every instance are drawn anew, so the answer
cannot be known in advance, yet it can be scored exactly.

A run succeeds only if all of the following hold: the result is within the tolerance the instruments allow (0.2% to 5%
for most quantities), it is grounded in readings recorded during the episode, and the bench is left in the state the
task card requires. On an optimization experiment the first condition becomes a distance from the best result the
apparatus allows. All of this is computed offline from what the environment recorded; the agent's own words are never
used.

## Adapter Features

- **One container per trial, with the GPU in it.** The task image holds Isaac Sim, the EmbodiedLab environment daemon
  and benches, and a small tool gateway. The agent runs as an unprivileged user (`[agent].user = "agent"`).
- **The agent sees only the public interface.** The environment daemon listens on a Unix socket only root can open;
  the gateway forwards exactly the three public routes of the EmbodiedLab API (list tools, call a tool, poll an
  operation) on `127.0.0.1:18731`. The instance record, the task settings and the evidence directory are readable by
  root only. The agent's workspace is rendered by the gateway at start, as the original trial runner renders it:
  `README.md`, `task_card.md`, `task_spec.json`, `tools.json` and the standard-library client `embodiedlab_api`.
- **Starts without an entrypoint.** Cloud sandboxes do not run image entrypoints, so the environment healthcheck
  (`/opt/el-harbor/up.sh`, run as root before the agent is set up) starts the daemon and the gateway on its first call
  and passes once the workspace is ready.
- **Scoring in a separate verifier.** A `[[verifier.collect]]` step asks the simulator to seal the episode's evidence;
  the evidence travels as an artifact to the verifier container, which holds the instance record and runs the
  unchanged EmbodiedLab offline evaluator without network. Reference optima that the evaluator would otherwise
  compute while scoring (the active filter) are computed when the task is generated and shipped with the verifier.
- **Oracle from recorded runs.** `solution/` replays a recorded successful T0 run of the same instance through the
  public API, call for call; only the episode id inside state and evidence ids is rewritten. The 32 recorded runs
  (`embodiedlab-oracle-0.2.1.tar`) are not public, because each ends in the submitted answer of its task's
  instance; we provide the bundle to the Harbor team privately. `--oracle-bundle` takes it (a local path or a private
  https link) and checks its SHA-256 against `oracle.json`; without it the tasks are generated without their replay.
- **Timeouts from the benchmark.** The agent timeout is the experiment's wall-clock limit (2 to 8 hours for the
  determination experiments, 11.5 to 34.5 hours for the four optimization experiments); the simulated-time budget is
  enforced inside the simulator, as in the original.

## Generated Task Structure

```
embodiedlab/
├── hooke-k/
│   ├── task.toml              # metadata, the shared image, one GPU, healthcheck, agent user, timeouts
│   ├── instruction.md         # the original agent prompt (the task itself is in the rendered task_card.md)
│   ├── environment/
│   │   └── Dockerfile         # names the shared image
│   ├── solution/
│   │   ├── solve.sh           # oracle entry point
│   │   ├── replay.py
│   │   └── calls.jsonl.xz     # the recorded successful run of this instance
│   └── tests/
│       ├── test.sh            # writes /logs/verifier/reward.txt
│       ├── score.py           # runs the EmbodiedLab offline evaluator on the sealed evidence
│       ├── Dockerfile         # the verifier image
│       ├── docker-compose.yaml  # the verifier gets no network
│       └── el/                # evaluator packages, the instance record, reference optima where needed
├── pendulum-g/
└── ...
```

The adapter also writes the build context of the shared image to `<output-dir>-image/`. It contains the instance
record and settings of every task, so build the image from the same adapter run that generated the tasks.

Adapter code:

```
src/embodiedlab/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_embodiedlab.yaml
├── scripts/build_image.sh
└── src/embodiedlab/
    ├── adapter.py
    ├── main.py
    ├── oracle.json            # oracle index: dev seed, call count and checksum of each recorded run
    ├── image/                 # the shared image: Dockerfile, start script, gateway, workspace renderer
    └── task-template/
```

## Run Evaluation / Harness

Generate the tasks and build the image first (see Usage). Then, from the adapters repository root:

```bash
# oracle (default in the config)
harbor run -c src/embodiedlab/run_embodiedlab.yaml

# an agent on the local dataset
harbor run -p datasets/embodiedlab -e docker -n 1 -a <agent> -m <model>

# one task
harbor run -p datasets/embodiedlab -e docker -n 1 -a oracle -t embodiedlab/hooke-k

# Daytona (GPU sandboxes): push the image to a registry the sandboxes can pull and generate with --image <ref>
harbor run -p datasets/embodiedlab -e daytona -a <agent> -m <model>
```

Each trial uses one GPU; on a local Docker host run one trial per GPU. After publication:

```bash
harbor run -d embodiedlab/embodiedlab -a <agent> -m <model>
```

## Usage: Create Task Directories

```bash
cd src/embodiedlab
uv sync

# all 32 tasks, without the oracle replays (clones EmbodiedLab v0.2.1)
uv run embodiedlab --output-dir ../../datasets/embodiedlab

# all 32 tasks with the oracle replays, from the privately provided bundle
uv run embodiedlab --output-dir ../../datasets/embodiedlab --oracle-bundle /path/to/embodiedlab-oracle-0.2.1.tar

# the parity subset
uv run embodiedlab --split parity --output-dir ../../datasets/embodiedlab-parity

# selected tasks, from a local checkout and a local copy of the oracle runs
uv run embodiedlab --output-dir ../../datasets/embodiedlab --task-ids hooke-k pendulum-g \
  --source-dir /path/to/EmbodiedLab --oracle-dir /path/to/oracle --overwrite

# the shared image (needs the lab image embodiedlab/lab:0.2.1 built from the EmbodiedLab checkout)
scripts/build_image.sh ../../datasets/embodiedlab-image
```

Options: `--limit N`, `--overwrite`, `--task-ids` (task names or experiment ids), `--split full|parity`,
`--source-dir`, `--oracle-bundle`, `--oracle-dir`, `--image` (the image reference written into the tasks; use a
registry reference for cloud sandboxes), `--image-context`, `--cache-dir`, `--all-experiments` (also generate
`truss_bridge`, `lens_focal` and `optical_relay`, which have no oracle replay yet).

## Comparison with Original Benchmark (Parity)

Parity has not run yet. The plan, to be agreed with the Harbor team: the parity subset (`--split parity`, ten
determination experiments: the shortest wall-clock limits, with the shortest thermal experiment swapped in so that
every discipline appears), at least two runs per side, the same agent and model on both sides, the success rate as
the metric.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Performance | Harbor Performance |
|-------|-------|--------|----------------|--------------|----------------------|--------------------|
| TBD | TBD | Success rate (%) | TBD | 10 | TBD | TBD |

Reproduction, original side (the EmbodiedLab trial runner; give the printed prompt to the agent with the trial's
`workspace/` as its working directory):

```bash
git clone https://github.com/HexaFuture/EmbodiedLab.git && cd EmbodiedLab && git checkout v0.2.1
./embodiedlab setup && ./embodiedlab build
EMBODIEDLAB_SERVICES=off ./embodiedlab up
./embodiedlab trial start --exp hooke_k --seed 0 --exposure T0 --agent <agent>
./embodiedlab trial finish <trial_id>
./embodiedlab trial score
```

Use the dev seed in each task's `[metadata].seed`. Harbor side:

```bash
uv run embodiedlab --split parity --output-dir ../../datasets/embodiedlab-parity
harbor run -p datasets/embodiedlab-parity -e daytona -a <agent> -m <model>
```

## Notes & Caveats

- **`truss_bridge`, `lens_focal` and `optical_relay` not yet included.** They do not have a validated oracle replay
  yet (the `truss_bridge` oracle run is in progress); they follow in an update. `--all-experiments` generates them
  now, each with a `solve.sh` that fails.
- **One instance per experiment.** The original benchmark draws a new instance per seed. Each task uses the dev seed
  of its oracle run (`[metadata].seed`); the hidden values of that instance are in the verifier only.
- **Oracle provenance.** Each oracle replays a successful T0 run of the same instance: for 29 experiments a run an
  agent produced in our T0 evaluation, for `active_filter`, `fiber_coupling` and `vibration_absorber` a run of the
  experiment's reference controller through the same T0 interface. Every replay is validated on 0.2.1. A replay issues
  the same calls with the same arguments, including the calls the run got refused, and is scored by the unchanged
  evaluator. Where the recording client had timed out on a call that the simulator still carried out, the replay waits
  for the simulated time the recorded run had advanced, and a command refused as based on a stale robot state is sent
  once more with the current state id. In `reaction_yield` one such call, a robot motion, is restored into the
  recording, recomputed with the agent's own deterministic trajectory code from the state the previous call returned.
- **T1 not adapted.** T1 adds SAM3 segmentation and cuRobo motion planning, which run as separate GPU services.
- **The image is large** (about 35 GB, Isaac Sim). Cloud sandboxes need it in a registry they can pull and enough
  disk for it.
- **Agent CLI in the image.** The image has Python 3 with NumPy, SciPy, Pillow and OpenCV for the agent (in
  `/opt/agent-python`, first on `PATH`). Harbor installs the agent's own CLI at setup.
- **Process score.** The verifier writes the six-level process score to `/logs/verifier/details.json`; it is not
  part of the reward.

## Installation / Prerequisites

- Linux x86_64 with an NVIDIA RTX GPU with 24 GB (tested on RTX 4090), the NVIDIA driver, Docker with the NVIDIA
  Container Toolkit, and about 40 GB of disk for the images.
- The EmbodiedLab lab image `embodiedlab/lab:0.2.1`, built as the EmbodiedLab README describes (it needs the Isaac
  Sim base image `nvcr.io/nvidia/isaac-sim:6.0.1` from NGC).
- Harbor (`uv tool install harbor`) and `uv` for this adapter.
- For Daytona: GPU sandboxes with an RTX-class GPU (`rtx-4090`, `rtx-5090` or `rtx-pro-6000`) and the task image in a
  registry the sandboxes can pull.

## Troubleshooting

- **The healthcheck never passes.** Isaac Sim compiles its shaders on the first start, which can take several
  minutes; the start period is 30 minutes. Read `/var/log/el/envd.log` and `/var/log/el/gateway.log` in the container
  (root only).
- **`no such task`** from the healthcheck: the image was built from a different adapter run than the tasks. Rebuild
  it from the `<output-dir>-image/` of the run that generated the tasks.
- **Reward 0 with `sealed: false`** in `details.json`: the collect step could not seal the evidence (the simulator had
  stopped); see `envd.log`.
- **Verifier image build is slow:** it installs NumPy and SciPy from PyPI; set `PIP_INDEX_URL` to a mirror.

## Citation

```bibtex
@article{gao2026embodiedlab,
  title   = {{EmbodiedLab}: Beyond Manipulation, Benchmarking Embodied Agents That Act to Know},
  author  = {Gao, Hongcheng and Zheng, Zelin and Zhou, Alex and Zhu, Jay and Ge, Shijia and Wang, Yazhe and Li, Xiping and Zeng, Jianshu and Wu, Di and Shangguan, Xuan and Wan, Yexing and He, Lingyu and Wang, Yongli and Yu, Xiaohan and Jia, Zhiqi and Liu, Yue and Hooi, Bryan and Dong, Yinpeng and Zhang, Wentao and Wu, Sihang and He, Xiao},
  journal = {arXiv preprint arXiv:2610.35385},
  year    = {2026}
}
```

## Authors & Contributions

This adapter is developed and maintained by the EmbodiedLab team (Zelin Zheng, zhengzelin200303@163.com; Hongcheng
Gao, gaohongcheng2000@gmail.com). Issues and pull requests are welcome in the
[adapters repository](https://github.com/harbor-index/adapters).
