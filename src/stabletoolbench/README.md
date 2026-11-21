# StableToolBench

## Overview

This adapter generates five tool-use tasks from the official `G1_instruction` split. An agent calls MirrorAPI-Cache and submits its answer with `Finish`. StableToolEval grades the resulting transcript using Solvable Pass Rate (SoPR). Formal parity is pending.

| Query ID | Request | Tool |
|---|---|---|
| 608 | Find details and clips for a streaming channel | Kick.com API |
| 1073 | Find sites and keywords for birthday-party ideas | Keyword Analysis |
| 23982 | Find anime jokes by series and keyword, and browse the collection | Anime Jokes |
| 66018 | Look up movie listings and sessions for a particular film | Movie Showtimes |
| 79620 | Find Dallas airports and look up DFW by code | Airports Finder |

## What is StableToolBench?

StableToolBench pairs natural-language requests with tool definitions and an API simulator. Its solvable-query dataset contains 765 queries in six splits. The five queries in this adapter belong to the 163-query `G1_instruction` split. [Upstream repository](https://github.com/THUNLP-MT/StableToolBench/tree/aa4ed9f4737ad98bd706663f01d63623c3427812)

## Adapter Features

- Each trial has its own Compose service named `broker`. It runs the original tool environment with the task's published tool schemas, the native 1,024-character observation budget, error codes and `Finish` handling.
- A CLI agent can use `stb-tool list` and `stb-tool call NAME JSON_ARGUMENTS`. The broker accepts function calls, records observations it obtains from the simulator, and closes the trace on a terminal action.
- The separate verifier receives `/stb-trace/answer.json` from that broker. It runs the upstream converter and `eval_pass_rate.py` with three evaluations. Each label contributes 1 (Solved), 0.5 (Unsure), or 0 (Unsolved). The per-task reward is their arithmetic mean. Infrastructure errors leave the score pending.
- The reference solution replays the tool names and arguments from a published CoT@1 trajectory. It builds the final answer from the observations returned in the current trial. The original tool environment truncates long observations to the first 1,024 characters and appends `...`.

## Generated Task Structure

```text
g1-instruction-1073/
  task.toml
  instruction.md
  environment/{Dockerfile,client.py,docker-compose.yaml,broker/}
  solution/{solve.sh,oracle.py,calls.json}
  tests/{Dockerfile,test.sh,grade.py,query.json,tooleval/}
```

The agent container contains the CLI. Task labels, archived tool responses, reference solutions and Judge credentials are absent from that image. After the agent stops, Harbor runs a collection command in the broker that exports its final in-memory trajectory. A simulator transport exception marks the trial unscoreable. An agent-visible tool error follows its upstream code. The Judge key is supplied to the separate verifier.

## Run Evaluation / Harness

Set `STB_MIRROR_URL` to the full private `/virtual` URL reachable from the **broker container**. The simulator must load the tool definitions used to generate the tasks. Set `STABLETOOLBENCH_JUDGE_MODEL`, `STABLETOOLBENCH_JUDGE_BASE_URL` and `STABLETOOLBENCH_JUDGE_API_KEY` in the runner environment. Use the approved exact Judge model ID. It must accept the original function-calling request. The verifier creates a temporary key pool for the upstream client.

```sh
harbor run -e docker -p datasets/stabletoolbench -a nop -n 1
harbor run -e docker -p datasets/stabletoolbench -a oracle -n 1
```

A Compose-capable Daytona runner can also execute the generated tasks when its broker has an authorized route to the same simulator. `127.0.0.1` within that container refers to its own network namespace. For a private simulator running on the controller, the optional adapter backend relays only this task’s permitted simulator calls. It requires Harbor’s Daytona SDK dependency. [Private Daytona relay](RELAY.md)

**All five oracle tasks scored 1.0 on Daytona.** The Judge (`gpt-4.1-2025-04-14`) gave three Solved votes on each task. All five no-action controls scored 0.0. For queries 608 and 1073, we verified that the generated files, executable permissions and both Harbor task hashes matched the previously graded tasks, then reused those results. Queries 23982, 66018 and 79620 ran with the same simulator and Judge settings. [Terminal screenshot](evidence/oracle.png) · [Per-query results and checksums](evidence/oracle.json)

## Usage: Create Task Directories

```sh
cd src/stabletoolbench
uv sync --locked
uv run stabletoolbench --output-dir datasets/stabletoolbench
uv run stabletoolbench --output-dir datasets/single --task-ids 1073
```

The first generator command produces all five tasks using public inputs. It fetches the pinned StableToolBench code and queries, the five tool definitions, and the official baseline ZIP (about 38 MB). It verifies the tool documents and reference traces before writing the task directories. To reuse local copies, provide any of the following.

| Option | Environment variable | Local input |
|---|---|---|
| `--upstream-dir` | `STB_UPSTREAM_DIR` | Git checkout containing the pinned StableToolBench commit |
| `--toolenv-dir` | `STB_TOOLENV_DIR` | ToolBench tool directory containing the `Data`, `Entertainment` and `Travel` subdirectories |
| `--baseline-archive` | `STB_BASELINE_ARCHIVE` | Official `data_baselines.zip`, required for 23982, 66018 and 79620 |

`--task-ids` selects individual query IDs or task directory names. `--limit` takes the first N in the selected order and `--overwrite` regenerates existing tasks.

## Comparison with Original Benchmark (Parity)

Formal parity is pending team confirmation. The proposed comparison uses SoPR alone. SoWR and FAC are outside the current evaluation scope.

| Agent | Model | Metric | Number of Runs | Dataset Size | Original Performance | Harbor Performance |
|---|---|---|---:|---|---|---|
| Pending team confirmation | Pending | SoPR | 0 | 5 proposed | — | — |

The original side uses the upstream `qa_pipeline_multithread.py` with `--backbone_model chatgpt_function --method CoT@1`, the approved agent model and base URL, the selected queries and test tool root. Its `SERVICE_URL` must point to the same MirrorAPI service as the Harbor broker. Both sides need the same Judge model, three SoPR evaluations, and the same tool-use and decoding budgets. On both sides, CoT@1 uses a chain-depth limit of 50 and keeps the first 1,024 characters of each tool response. The proposed paired check covers all five queries, followed by three full runs per side and a separate Codex CLI check. The models, protocol and paired runner are pending confirmation before formal parity. After team confirmation, run the native side from the upstream checkout with the same frozen query list used for Harbor.

```sh
PYTHONPATH=. python toolbench/inference/qa_pipeline_multithread.py \
  --backbone_model chatgpt_function --method CoT@1 \
  --chatgpt_model "$STB_AGENT_MODEL" --base_url "$STB_AGENT_BASE_URL" --openai_key "$STB_AGENT_KEY" \
  --tool_root_dir "$STB_TOOLENV_DIR" --input_query_file "$STB_PARITY_QUERIES" \
  --output_answer_file "$STB_NATIVE_ANSWERS" --toolbench_key "" \
  --max_observation_length 1024 --num_thread 1
```

## Notes & Caveats

This subset retains two previously validated queries (608 and 1073). The three additional IDs were fixed before their Harbor oracle runs. Each has an archived CoT@1 tool-call sequence ending in `Finish` and tool definitions that match the split. The actions for 608 and 1073 come from the repository's `data_example/answer/virtual_chatgpt_cot` sample. Those for 23982, 66018 and 79620 come from `data_baselines/chatgpt_cot/G1_instruction` in the official baseline archive. The source manifest records the exact paths and SHA-256 digests. [Five-task source manifest](src/stabletoolbench/sources.json)

| Input | Fixed public revision |
|---|---|
| StableToolBench code, queries and sample traces | [`aa4ed9f4737ad98bd706663f01d63623c3427812`](https://github.com/THUNLP-MT/StableToolBench/tree/aa4ed9f4737ad98bd706663f01d63623c3427812) |
| StableToolBench baseline archive | [`ea70398f955e7b58f8807187936818e794ce5140`](https://huggingface.co/datasets/stabletoolbench/baselines/tree/ea70398f955e7b58f8807187936818e794ce5140) |
| ToolBench test tool definitions | [`0c25df5e6937aae854c2cc1bb203297600afce8b`](https://huggingface.co/datasets/Adorg/ToolBench/tree/0c25df5e6937aae854c2cc1bb203297600afce8b/toolenv/tools) |
| MirrorAPI-Cache weights | [`df8f586d0f8d17bcbbaee11f1e8f9dd178a28c68`](https://huggingface.co/stabletoolbench/MirrorAPI-Cache/tree/df8f586d0f8d17bcbbaee11f1e8f9dd178a28c68) |

The WIP reports SoPR for these five queries. A no-action control receives zero under the upstream missing-`Finish` rule and requires no model call. A missing trusted trace is an infrastructure error.

## Installation / Prerequisites

Python 3.11+, uv, Git, Harbor 0.26.0 and a Compose-capable environment are required for this workflow. The tool and Judge services must be reachable from their respective containers. The benchmark was developed against MirrorAPI-Cache revision `df8f586d0f8d17bcbbaee11f1e8f9dd178a28c68`. [MirrorAPI-Cache](https://huggingface.co/stabletoolbench/MirrorAPI-Cache/tree/df8f586d0f8d17bcbbaee11f1e8f9dd178a28c68)

## Troubleshooting

If the API response indicates a missing tool, verify that the simulator has the same five tool definitions as the generated tasks. Check broker reachability independently of the model’s OpenAI-compatible inference endpoint. A finished trial without all three Judge settings stops before the Judge call. Each scored trial writes the three native labels, converted execution graph and reward to the verifier logs.

## Citation

Zhicheng Guo, Sijie Cheng, Hao Wang, Shihao Liang, Yujia Qin, Peng Li, Zhiyuan Liu, Maosong Sun and Yang Liu. *StableToolBench: Towards Stable Large-Scale Benchmarking on Tool Learning of Large Language Models* (2024). [Paper](https://arxiv.org/abs/2403.07714)

```bibtex
@misc{guo2024stabletoolbench,
  title = {StableToolBench: Towards Stable Large-Scale Benchmarking on Tool Learning of Large Language Models},
  author = {Guo, Zhicheng and Cheng, Sijie and Wang, Hao and Liang, Shihao and Qin, Yujia and Li, Peng and Liu, Zhiyuan and Sun, Maosong and Liu, Yang},
  year = {2024},
  eprint = {2403.07714},
  archivePrefix = {arXiv}
}
```

## Authors & Contributions

StableToolBench was created by the benchmark authors listed above. Haoyue Bai is responsible for the Harbor adaptation.
