# PhySciBench → Harbor Adapter

[Paper](https://arxiv.org/abs/2606.18648) · [Upstream repository](https://github.com/yigengjiang/physci-deepresearch) · [Dataset](https://huggingface.co/datasets/littletreee/PhySciBench)

## Overview

PhySciBench evaluates deep-research capabilities in physics and chemistry through **200 expert-curated questions** across six task types. Its tasks follow the scientific research workflow: interpreting figures, synthesizing evidence from papers, extracting structured information, reasoning about scientific mechanisms, designing experiments, and implementing scientific computations. The accompanying paper introduces DelveAgent, a multi-agent research framework; this adapter brings the benchmark's tasks and evaluation pipeline into Harbor.

This adaptation includes **162 tasks**, one per selected upstream question. It excludes the 20 code-generation questions and 18 additional questions identified during oracle validation. Running this adapter does not require E2B.

| Task type | Tasks | What the agent does |
| --- | ---: | --- |
| `multimodal-qa` | 38 | Interpret scientific figures, extract values, and explain trends. |
| `long-context-qa` | 29 | Synthesize evidence from papers and supplementary materials. |
| `structured-information-extraction` | 40 | Extract requested fields into JSON or CSV. |
| `scientific-reasoning` | 27 | Develop scientific explanations and multi-step derivations. |
| `experimental-design` | 28 | Complete or correct experimental plans under stated constraints. |
| **Total** | **162** | |

<sub>Note: The 18 additional questions were excluded because their ground-truth answers did not fully cover the rubric requirements under LLM judging, so even oracle responses did not consistently receive full credit. The original questions, reference answers, and rubrics were left unchanged.</sub>

### Task framework

Each question becomes an isolated Docker task. The adapter preserves upstream IDs, question text, reference answers, rubrics, and supporting files. Agents read the question in `instruction.md`, access attachments under `/workspace/files/`, and write their complete answer to `/workspace/response.txt`.

```text
datasets/physcibench/physci-NNN/
├── task.toml                    # Task metadata, resources, and verifier environment
├── instruction.md               # Question and answer-file instructions
├── environment/
│   ├── Dockerfile
│   ├── docker-compose.yaml      # Build and runtime proxy forwarding
│   ├── requirements.txt
│   └── files/                   # This question's figures and documents
├── solution/
│   ├── solve.sh                 # Oracle: copy the upstream reference answer
│   └── reference.txt
└── tests/
    ├── test.sh                  # Verifier entry point
    ├── verify.py                # Adapt the upstream result to Harbor's reward format
    ├── sample.json              # This question's evaluation record
    ├── UPSTREAM-LICENSE
    └── eval/                    # Upstream scorer and judge templates
```

Harbor supplies the solution and verifier files during their respective execution phases. The agent image contains the question's supporting files; the reference answer is supplied through the oracle solution.

### Evaluation

The verifier uses the upstream scoring paths: exact matching with LLM fallback for atomic answers, rule-based JSON/CSV checks for structured extraction, and rubric-guided LLM grading for long-form responses. Harbor receives the **raw score from 0 to 1** as its reward. The verifier also records the upstream `correct` flag, judgment, and reasoning.

Average Score is the mean of raw rewards. Accuracy is computed from the `correct` flags; a correct answer can still receive less than full credit. For rubric and structured-extraction paths, the upstream correctness threshold is `score >= 0.8`.

## Running the Adapter

### 1. Prepare the environment and data

Start with Conda installed and available in your shell. Create a Python 3.12 environment, activate it, then install `uv` and the Harbor CLI into that environment:

```bash
conda create -n harbor-physcibench-adaptor python=3.12 -y
conda activate harbor-physcibench-adaptor

python -m pip install uv
python -m pip install harbor

# Verify the installation.
python --version
uv --version
harbor --help

# Enter the adapter directory.
cd /path/to/adapters/src/physcibench
```

If the Conda environment already exists, skip its creation and activate it directly. Replace the checkout path above when using another machine. Docker and Docker Compose must also be installed on the host; the Docker checks and startup commands are covered in step 3.

**Prepare the upstream source and base dataset.** The [original repository](https://github.com/yigengjiang/physci-deepresearch) provides the evaluation code and downloader; the [Hugging Face dataset](https://huggingface.co/datasets/littletreee/PhySciBench) provides the questions, reference answers, rubrics, and attachments. Both are needed to generate Harbor tasks. With the Conda environment above active, clone the source and download the data:

```bash
git clone https://github.com/yigengjiang/physci-deepresearch.git /path/to/physci-deepresearch
cd /path/to/physci-deepresearch
python -m pip install huggingface_hub
python scripts/download.py --local-dir PhySciBench --with-files
```

The downloader fetches `physcibench.json` and the `files/` directory from Hugging Face. Keep `--with-files`: metadata alone does not include the figures and PDFs required by some tasks. Obtain any required dataset access before downloading; the upstream data terms are linked below.

| Location | Purpose |
| --- | --- |
| `/path/to/physci-deepresearch/` | Upstream source, including `eval/`, `requirements.txt`, and `LICENSE`. |
| `/path/to/physci-deepresearch/PhySciBench/` | Base dataset: `physcibench.json` and its referenced attachments under `files/`. |
| `/path/to/adapters/src/physcibench/datasets/physcibench/` | Generated Harbor task directories used for evaluation. |

**Generate Harbor tasks.** Return to this adapter and pass the source and data paths explicitly:

```bash
cd /path/to/adapters/src/physcibench
uv run physcibench \
  --upstream-dir /path/to/physci-deepresearch \
  --data-dir /path/to/physci-deepresearch/PhySciBench \
  --output-dir datasets/physcibench
```

Replace `/path/to/...` with your local paths. If you already have the source and an authorized dataset snapshot, reuse them and point `--data-dir` to the directory containing `physcibench.json` and `files/`. The generator can package all 180 non-code-generation questions; `config/run_physcibench_162.yaml` selects the 162-question adaptation at runtime. Skip generation if the Harbor task directories are already present. Use `--overwrite` to regenerate existing tasks or `--task-ids physci-001 physci-002` to generate a small subset.

### 2. Configure the judge

Even oracle runs need a judge API for questions that use LLM grading. In the same terminal, set the endpoint and judge model, then enter the API key without displaying it. Replace the model placeholder with an identifier available to your account:

```bash
export OPENAI_BASE_URL=""
export JUDGE_LLM_MODEL="model_name"

read -rsp 'API key: ' OPENAI_API_KEY
echo
export OPENAI_API_KEY
```

Generated `task.toml` files map `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `JUDGE_LLM_MODEL` into the verifier environment. Rubric grading requires Chat Completions structured output with nested JSON Schema support, including `$defs` and `$ref`. A model appearing in an endpoint's model list does not establish that this scoring path works; validate it with a rubric-based trial before a full run.

Next, enable the shared host's proxy and export its settings for Docker builds and task containers:

```bash
proxy_on
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
export NO_PROXY="$no_proxy"
```

`proxy_on` is a helper provided by the shared host's shell configuration. On another host, use its proxy setup instead. Keep these exports and the Harbor command in the same terminal; use `sudo -E` when running Harbor with sudo so the judge and proxy variables are preserved.

### 3. Check Docker and run

On a Linux host managed by systemd, start Docker if needed and verify connectivity:

```bash
sudo systemctl start docker
docker info
docker compose version
```


All supplied configurations use the oracle agent, four concurrent Docker trials, and one attempt per question:

| Configuration | Tasks | Selection |
| --- | ---: | --- |
| [`config/run_physcibench.yaml`](config/run_physcibench.yaml) | 180 | All supported questions. |
| [`config/run_physcibench_162.yaml`](config/run_physcibench_162.yaml) | 162 | Also excludes `physci-010` and `physci-181`, which lost credit in run `2026-11-21__19-01-13`. |

The filters select tasks at runtime and leave the generated task directories intact. YAML paths are relative to the working directory: run these commands from `adapters/src/physcibench/`, even though the configurations live under `config/`.

```bash
# Validate the rubric judge with one question.
harbor trial start -p datasets/physcibench/physci-010 -a oracle

# Run the current 162-question selection.
harbor run -c config/run_physcibench_162.yaml -y
```

Harbor builds the images, starts the task containers, runs the oracle and verifier, and removes the containers after each trial. To evaluate another agent, replace the `agents` entry in the chosen configuration and configure that agent's credentials separately from the judge.

### 4. Inspect results

Results are written under `jobs/<timestamp>/`:

| Artifact | Contents |
| --- | --- |
| `result.json` | Aggregate job statistics, mean reward, and exceptions. |
| `physci-NNN__*/result.json` | Per-trial reward, timing, and exception details. |
| `physci-NNN__*/verifier/result.json` | Upstream score, correctness flag, and criterion-level reasoning. |
| `physci-NNN__*/verifier/reward.txt` | Raw reward used by Harbor. |

The script creates `jobs/<timestamp>/results.json` before launching Harbor and
updates its finish time and exit status when Harbor exits. It records
`JUDGE_LLM_MODEL`, the configured agent names and models, and the configuration
path, without recording API keys. For `oracle`, the agent model is `null` because
the oracle copies a reference answer; `judge_model` identifies the configured
LLM-as-judge model. Harbor's original `result.json` still contains the scores and
exceptions. Direct `harbor run` commands do not create this additional metadata
file. The model name records the requested identifier, not a provider's internal
model routing. Use a new job directory for each run; the script refuses to
overwrite an existing directory.

Use `harbor view jobs` to inspect runs. For failures, check the trial log and verifier output to distinguish environment or API errors from valid low scores. Generated datasets, jobs, and the local virtual environment are excluded from Git by this adapter's `.gitignore`.

## Notes, Citation & Attribution

### Evaluation scope and reproducibility

- **Filtered selections.** The 164- and 162-question configurations are local subsets selected using observed oracle scores. Report the configuration, task IDs, and denominator with results. Scores on these subsets are not directly comparable to the original 200-question benchmark or the full 180-question adaptation.
- **Oracle behavior.** The oracle submits the upstream reference answer verbatim. Some references omit explanations, evidence links, or formatting explicitly required by their rubrics, so oracle execution does not guarantee full credit. Judge decisions can also vary between calls.
- **Judge settings.** The current upstream scorer defaults to `temperature=0.0` and `top_p=1.0`; `verify.py` does not override them. These settings do not guarantee identical judgments across runs. Values such as `0.9999999999999999` can result from floating-point arithmetic and should be treated as full credit when analyzing outcomes.
- **Prompt adaptation.** The adapter appends a JSON-only output instruction to the long-form judge template to match the upstream `JudgmentResponse` schema. Rubric criteria, weights, and scoring logic retain their upstream definitions.
- **Parity status.** Original-versus-Harbor agent parity has not yet been recorded in `parity_experiment.json`. For a comparison, use identical task IDs, attachments, answer instructions, judge prompts, and judge settings on both sides. Record the data and code revisions, agent/model versions, endpoint, and scoring parameters, and report both Average Score and Accuracy.

### Data use and licensing

The upstream evaluation code is licensed under [Apache License 2.0](https://github.com/yigengjiang/physci-deepresearch/blob/main/LICENSE); generated tasks retain its license in `tests/UPSTREAM-LICENSE`.

The dataset has separate [usage terms](https://github.com/yigengjiang/physci-deepresearch/blob/main/DATA_LICENSE.md). The checked-out upstream terms restrict PhySciBench to academic research, prohibit commercial use, and require prior approval for distribution, publication, copying, dissemination, or modification. Third-party papers, figures, and supplementary materials retain their owners' copyrights and licenses. This adapter does not grant additional rights to the dataset or its attachments.

### Citation

Please cite the original benchmark when using this adapter:

```bibtex
@article{jiang2026physcidr,
  title   = {Deep Research in Physical Sciences: A Multi-Agent Framework and Comprehensive Benchmark},
  author  = {Jiang, Yigeng and Yang, Tengchao and others},
  journal = {arXiv preprint arXiv:2606.18648},
  year    = {2026}
}
```

### Authors & Contributions

PhySciBench was created by Yigeng Jiang, Tengchao Yang, and coauthors. This Harbor adapter was developed by Jiaxing Zhang. See the [upstream repository](https://github.com/yigengjiang/physci-deepresearch) for the benchmark, original evaluation code, and full author list.
