## Online-Mind2Web → Harbor Adapter

## Overview

Online-Mind2Web tests whether a web agent can complete realistic, everyday tasks on **live websites**. Examples are finding a product with specific filters, looking up a schedule, or filling out a search form.

- **Size:** all **300** tasks from the upstream release (`Online_Mind2Web.json` on the gated Hugging Face dataset [`osunlp/Online-Mind2Web`](https://huggingface.co/datasets/osunlp/Online-Mind2Web)). One upstream task becomes one Harbor task; nothing is excluded.
- **Coverage:** 147 websites. Tasks are in English.
- **Difficulty:** each task has an upstream label based on how many steps a human needed: easy (80), medium (141), hard (79).
- **Parity split:** a fixed 100-task subset with the same difficulty mix: easy 27, medium 47, hard 26.
- **License:** the dataset is CC BY 4.0 and the upstream code is MIT.
- **Not the same as Mind2Web.** Online-Mind2Web is derived from the original Mind2Web, but Mind2Web is an *offline* benchmark scored on cached web pages with step-level accuracy. Online-Mind2Web runs on the real web and scores whole tasks.

**Main changes for Harbor:**
- **A browsing agent is included.** Upstream ships tasks and an evaluator but no agent. This adapter includes a [Browser Use](https://github.com/browser-use/browser-use) agent, `online_mind2web.browser_use_agent:BrowserUseAgent`. The agent runs headless Chromium inside the task container and writes the upstream v2 submission format.
- **WebJudge runs as the verifier.** WebJudge is the upstream evaluator, used here in Online-Mind2Web mode. It runs in a **separate verifier container** (`[verifier] environment_mode = "separate"`), built fresh from the same image, so the agent can't tamper with the grader. Harbor collects the agent's submission (`artifacts = ["/app/output"]`) and re-creates it at `/app/output` in the verifier container.
- **No oracle.** The benchmark has no reference solutions, so `solution/solve.sh` is a placeholder. See [Notes & Caveats](#notes--caveats).

## What is Online-Mind2Web?

Online-Mind2Web comes from the paper [*An Illusion of Progress? Assessing the Current State of Web Agents*](https://openreview.net/forum?id=6jZi4HSs6o) (Xue et al., COLM 2025) by the OSU NLP Group. The paper argues that earlier web-agent benchmarks overstated progress, and introduces a benchmark that runs on live websites to give a more realistic picture.

- **Who it's for:** researchers building and comparing web agents.
- **Metric:** **task success rate.** It's the share of tasks that WebJudge labels a success.
- **How WebJudge decides:**
  1. An LLM picks out the key requirements of the task.
  2. It scores every screenshot the agent took from 1 to 5 for relevance, and keeps those that score at or above a threshold (3 by default).
  3. It judges success from the agent's action history and the kept screenshots.
- **Judge reliability:** upstream reports about 85–87% agreement with human labels. With GPT-6 Luna as the judge model, it's 87.4%.
- **Leaderboard and source:** https://github.com/OSU-NLP-Group/Online-Mind2Web

## Adapter Features

- **Dataset loading:** the adapter downloads the task file from the gated Hugging Face dataset, or reads a local copy with `--dataset-path`. It checks that every task has the required fields and that task IDs and their directory names are unique.
- **Parity split:** `--split parity` selects a 100-task subset with a fixed random seed (42), sampling each difficulty level in proportion to the full set. The same selection code is used by the original-side harness.
- **Container:** each task gets Python 3.12 (Debian bookworm), Chromium, and Noto fonts including CJK and emoji, so non-Latin pages render properly. The judge has its own virtualenv at `/opt/webjudge`.
- **Browser Use agent** (`browser-use==0.13.14`, installed into `/opt/browser-use`):
  - Starts on the task's website and does one action per step, so every action has its own screenshot for the judge.
  - Defaults to 50 steps and `reasoning_effort=low`. Browser Use's built-in self-judge is turned off.
  - Records the run in the upstream v2 format: `result.json` plus `trajectory/NNNN.png`.
  - Reports token usage and cost back to Harbor.
- **Verifier:**
  - Uses the upstream WebJudge code, copied verbatim from upstream commit `1a8db93`.
  - The only change: `utils.generate` sends `max_completion_tokens` and no `temperature` for reasoning models, since GPT-5/6-family models reject those parameters. The same patch is applied on the original side.
  - Before judging, it checks the submission against the v2 schema. A missing or malformed submission scores 0.
  - If the judge API call fails (network error, wrong key, etc.), the verifier exits non-zero and writes no reward. That way an outage shows up as an error instead of a silent 0.
- **Prompt:** the task text plus `Start from this website: <url>`, identical on both sides of the parity comparison.

## Generated Task Structure

```
online-mind2web/
├── {task_slug}/                      # lower-cased upstream task_id
│   ├── task.toml                     # metadata (difficulty, website, upstream id), timeouts, verifier env
│   ├── instruction.md                # task + starting website
│   ├── environment/
│   │   ├── Dockerfile                # Debian + Chromium + fonts + WebJudge venv
│   │   └── task.json                 # task metadata, copied to /app/task.json
│   ├── solution/
│   │   └── solve.sh                  # placeholder (no oracle exists)
│   └── tests/
│       ├── test.sh                   # runs WebJudge, writes /logs/verifier/reward.txt
│       ├── task.json                 # trusted copy of the task used by the judge
│       └── webjudge/
│           ├── run_webjudge.py       # validates the v2 submission, calls WebJudge
│           ├── webjudge_online_mind2web.py   # upstream, verbatim
│           └── utils.py              # upstream + reasoning-model patch
```

Adapter code layout:

```
src/online-mind2web/
├── README.md
├── adapter_metadata.json
├── parity_experiment.json
├── pyproject.toml
├── run_online-mind2web.yaml
└── src/online_mind2web/
    ├── adapter.py                # task loading, parity split, task generation
    ├── main.py                   # CLI
    ├── browser_use_agent.py      # Harbor agent (installs and runs Browser Use in the container)
    ├── run_browser_use.py        # Browser Use runner + v2 trajectory export (shared with the original side)
    └── task-template/
```

During a run:
- The agent writes its submission to `/app/output/<task_id>/`. It is also copied to `/logs/agent/trajectory/`, and Harbor saves it as a trial artifact under `artifacts/app/output/`, which is what the separate verifier grades. Saved submissions can therefore be regraded without rerunning the agent.
- The verifier saves the trajectory it judged to `/logs/verifier/trajectory/` and WebJudge's full output to `/logs/verifier/webjudge_result.json`.

## Run Evaluation / Harness

All commands are run from the root of the adapters repo. The agent and the judge both need `OPENAI_API_KEY`. `OPENAI_BASE_URL` is also passed through to the agent.

Two flags appear in every command:
- `PYTHONPATH` must point at the adapter's `src/` directory so Harbor can import the custom agent.
- `-y` skips Harbor's prompt asking you to confirm the environment variables it passes to the agent.

### Running with Datasets Registry

Once the dataset is registered:

```bash
PYTHONPATH=src/online-mind2web/src uv run --project src/online-mind2web --extra harbor \
  harbor run -d online-mind2web \
  -a online_mind2web.browser_use_agent:BrowserUseAgent -m openai/gpt-6-luna -y
```

### Using Job Configurations

```bash
PYTHONPATH=src/online-mind2web/src uv run --project src/online-mind2web --extra harbor \
  harbor run -c src/online-mind2web/run_online-mind2web.yaml -y

# Or point at a locally generated dataset directly (here: the parity split, 3 tasks at a time)
PYTHONPATH=src/online-mind2web/src uv run --project src/online-mind2web --extra harbor \
  harbor run -p datasets/online-mind2web-parity \
  -a online_mind2web.browser_use_agent:BrowserUseAgent -m openai/gpt-6-luna -n 3 -y
```

The agent accepts these settings via `--ak`:
- `max_steps`, default 50
- `max_actions_per_step`, default 1
- `reasoning_effort`, default `low`

The verifier reads these from `[verifier.env]` in each task's `task.toml`:
- `JUDGE_MODEL`, default `gpt-6-luna`
- `JUDGE_SCORE_THRESHOLD`, default 3
- `JUDGE_MAX_COMPLETION_TOKENS`, default 16384

### Running Individual Trial

```bash
PYTHONPATH=src/online-mind2web/src uv run --project src/online-mind2web --extra harbor \
  harbor trial start -p datasets/online-mind2web/<task_slug> \
  -a online_mind2web.browser_use_agent:BrowserUseAgent -m openai/gpt-6-luna
```

## Usage: Create Task Directories

The task file is gated on Hugging Face. To get it:
1. Accept the terms at https://huggingface.co/datasets/osunlp/Online-Mind2Web.
2. Log in with `hf auth login` or set `HF_TOKEN`.

Alternatively, download `Online_Mind2Web.json` yourself and pass `--dataset-path`.

```bash
# All 300 tasks
uv run --project src/online-mind2web online-mind2web --output-dir datasets/online-mind2web

# The 100-task parity split
uv run --project src/online-mind2web online-mind2web --split parity --output-dir datasets/online-mind2web-parity
```

Flags:
- `--output-dir`: where to write tasks (default `datasets/online-mind2web`).
- `--limit N`: only the first N tasks, sorted by task_id, after the split is applied.
- `--overwrite`: regenerate existing task directories.
- `--task-ids ID [ID ...]`: only these upstream task IDs.
- `--dataset-path PATH`: use a local `Online_Mind2Web.json` instead of downloading it.
- `--split {full,parity}` and `--parity-size N` (default 100).

## Comparison with Original Benchmark (Parity)

Parity ran on the 100-task parity split, with 3 runs per side. Full details are in `parity_experiment.json`.

Links: [original benchmark](https://github.com/OSU-NLP-Group/Online-Mind2Web) · [parity fork](https://github.com/adityalahiri/Online-Mind2Web/tree/harbor-parity) · [adapter PR](https://github.com/harbor-index/adapters/pull/47) · [parity artifacts (HF)](https://huggingface.co/datasets/harborframework/parity-experiments/discussions/315) · dataset PR: to follow after review

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Benchmark Performance | Harbor Adapter Performance |
|-------|-------|--------|------------------|--------------|------------------------------|----------------------------|
| browser-use@0.13.14 | gpt-6-luna | Success rate (%) (WebJudge, gpt-6-luna) | 3 | 100 (parity split of 300) | 33.00 ± 1.53 | 32.33 ± 0.67 |

- **Per-run scores:** original 30.0, 35.0 and 34.0; Harbor 31.0, 33.0 and 33.0.
- **The score ranges overlap.** Both sides finished all 100 tasks in every run, with 0 errors and no missing trajectories.
- **Individual tasks still vary between runs.** The websites are live, so a single task can flip from success to failure from one run to the next on either side. For example, the two sides agreed on 77 of 100 tasks in run 1. This is why parity compares the mean over 3 runs.
- **Cost:** about $15 in total. Agent tokens were about $7 on the Harbor side (including one earlier trial full run) and $6 on the original side, plus about $1.50 of WebJudge calls.
- **An earlier trial full run on Harbor** used 4 GB per task and scored 32%. One task was killed when it ran out of memory, so the final runs use 8 GB.
- **Grader setup during parity:** the Harbor parity runs graded in the agent's container. The adapter has since switched to a separate verifier. The grading code is identical, and on a saved known-good and known-wrong submission the separate verifier gives 1 and 0, on both Harbor 0.24.0 and 0.27.0.

**Setup.** Upstream has no agent of its own, so both sides run the same Browser Use runner (`run_browser_use.py`) with the same prompt, model and settings. What differs between the sides:
- **Harbor side:** Chromium in the task container; WebJudge runs as the Harbor verifier.
- **Original side:** Browser Use runs on the host with Google Chrome; scoring uses upstream's unmodified `src/run.py` (WebJudge Online-Mind2Web mode, threshold 3).
- **Both sides:** a task with no trajectory, for example after a timeout, counts as a failure.

**Reproduce the original side.** Use the fork https://github.com/adityalahiri/Online-Mind2Web, branch `harbor-parity`, built on upstream commit `1a8db93`:

```bash
git clone -b harbor-parity https://github.com/adityalahiri/Online-Mind2Web && cd Online-Mind2Web
export OPENAI_API_KEY=...
# 1. run the agent on the parity split
uv run --python 3.12 --with browser-use==0.13.14 --with huggingface_hub \
  python harbor_parity/run_original.py --adapter-src <adapters-repo>/src/online-mind2web/src \
  --split parity --output-dir runs/original_run1
# 2. score with the unmodified upstream evaluator
uv run --no-project --python 3.11 --with-requirements requirements.txt python harbor_parity/eval_upstream.py \
  --mode WebJudge_Online_Mind2Web_eval --model gpt-6-luna --trajectories_dir runs/original_run1 \
  --api_key "$OPENAI_API_KEY" --output_path runs/original_run1_result --score_threshold 3 --num_worker 1
# 3. success rate over the fixed task list (missing trajectories count as 0)
python harbor_parity/score.py \
  --results runs/original_run1_result/WebJudge_Online_Mind2Web_eval_gpt-6-luna_score_threshold_3_auto_eval_results.json \
  --task-list-dir runs/original_run1_logs
```

**Reproduce the Harbor side:**

```bash
uv run --project src/online-mind2web online-mind2web --split parity --output-dir datasets/online-mind2web-parity
PYTHONPATH=src/online-mind2web/src uv run --project src/online-mind2web --extra harbor \
  harbor run -p datasets/online-mind2web-parity \
  -a online_mind2web.browser_use_agent:BrowserUseAgent -m openai/gpt-6-luna -n 4 -y
```

**Reading the scores:**
- Each task's reward is 1 if WebJudge outputs `Status: success` and 0 otherwise.
- A run's score is the mean reward over the 100 tasks.
- Results across runs are reported as mean ± sample SEM.

## Notes & Caveats

- **No oracle.** Online-Mind2Web has no reference solutions or replayable trajectories, and its tasks run on live websites, so an oracle run can't reach 100%. `solution/solve.sh` only prints a notice. Fidelity is checked by parity instead.
- **Live websites change.** Sites change their layout and content, and items go out of stock. Scores can drift over time and are not exactly reproducible. Run both parity sides around the same time.
- **Bot blocking.** Some sites block automated or headless browsers. For example, CarMax returns "Access Denied". These tasks are kept, since they also fail in the original setup, and they count against the agent.
- **Judge variance.** WebJudge uses an LLM, and GPT-6-family models don't accept `temperature=0`, so the same trajectory can occasionally get a different verdict. The judge uses the model's default reasoning effort (medium), as upstream recommends.
- **The judge never sees the agent's final answer.** It sees only the actions and screenshots. This is upstream's behaviour. Information the agent read but never showed on screen cannot earn credit.
- **Internet access.** The container needs outbound internet access for the websites and the OpenAI API.
- **Resources.** Each task uses 2 CPUs and 8 GB of memory (a few pages, e.g. large sitemaps, exhausted 4 GB), with a 40-minute agent timeout and a 15-minute verifier timeout. The first image build downloads Chromium and fonts, about 1 GB.
- **Cost.** With gpt-6-luna at `reasoning_effort=low`, the agent cost about $0.005–0.04 per task in our sanity runs, plus the judge's calls.

## Installation / Prerequisites

- Docker installed and running. Daytona should also work, since the tasks only need outbound internet access.
- [uv](https://docs.astral.sh/uv/).
- Adapter dependencies: `uv sync --project src/online-mind2web --extra harbor`. This pins `harbor==0.24.0`.
- Access to the gated Hugging Face dataset (see [Usage](#usage-create-task-directories)).
- `OPENAI_API_KEY`, used by both the agent and the judge. Optionally `OPENAI_BASE_URL` for an OpenAI-compatible proxy.
- For the original side only: Google Chrome on the host. Set `CHROME_PATH` if it isn't at the default macOS path.

## Troubleshooting

- **`Either datasets or tasks must be provided`**: pass `-p <dataset dir>`, `-d online-mind2web` or `-c <config.yaml>`.
- **Agent class can't be imported**: set `PYTHONPATH=src/online-mind2web/src`.
- **`harbor run` stops at a "Proceed?" prompt about environment variables**: add `-y`.
- **401 from Hugging Face**: accept the dataset terms, then run `hf auth login`.
- **Verifier error with no reward**: the judge API call failed. Check `OPENAI_API_KEY`, rate limits and `/logs/verifier/` for details. A missing or malformed trajectory, by contrast, scores 0 with the reason in `webjudge_result.json`.
- **`Unsupported value: 'temperature'` or `'max_tokens'`**: you are running the unpatched upstream `utils.py` with a GPT-5/6-family model. Use the patched copy from this adapter, or from the `harbor-parity` branch on the original side.
- **On macOS, upstream `src/run.py` fails with `cannot pickle '_thread.RLock'`**: run it through `harbor_parity/eval_upstream.py`, which switches multiprocessing to `fork`.

## Citation

```bibtex
@inproceedings{xue2025an,
  title={An Illusion of Progress? Assessing the Current State of Web Agents},
  author={Tianci Xue and Weijian Qi and Tianneng Shi and Chan Hee Song and Boyu Gou and Dawn Song and Huan Sun and Yu Su},
  booktitle={Second Conference on Language Modeling},
  year={2025},
  url={https://openreview.net/forum?id=6jZi4HSs6o}
}

@inproceedings{deng2023mind2web,
  author={Deng, Xiang and Gu, Yu and Zheng, Boyuan and Chen, Shijie and Stevens, Sam and Wang, Boshi and Sun, Huan and Su, Yu},
  title={Mind2Web: Towards a Generalist Agent for the Web},
  booktitle={Advances in Neural Information Processing Systems},
  volume={36},
  pages={28091--28114},
  year={2023}
}
```

## Authors & Contributions

This adapter is developed and maintained by [Aditya Lahiri](https://github.com/adityalahiri).

**Issues and Contributions:**
- Submit issues and pull requests to the main repository.
- Report outdated or invalid tasks to [upstream](https://github.com/OSU-NLP-Group/Online-Mind2Web/issues).
