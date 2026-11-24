# GameLogicBench parity evidence

The 72-task submission targets a 24-task comparison. Each side uses Codex 0.144.1 with GPT-5 mini, high reasoning effort and a 3,600-second active solve budget. The model name is the reviewer gateway alias.

The comparison pairs three rounds. The earlier 20 tasks reuse the three 51-task release rounds after checking the saved Harbor submissions against the expansion verifier. The recheck covers 60 submissions and 1,314 judge cases. The four new tasks cover rendered lighting, live character physics, single-module replacement and multi-file module replacement. Each ran on both systems three times through the reviewer API.

The original-side expansion submissions were graded in the pinned GameLogicBench harness. The earlier original-side scores come from the prior paired runs, with the native adjudication checks recorded in the earlier evidence. A task passes when all of its cases pass. The table reports the mean and sample standard error across the three rounds.

The round-two root-motion result was independently checked by grading each saved submission with both verifiers. All 22 case verdicts agreed with their original scores. The saved original-side submission passes both verifiers, and the saved Harbor submission fails both.

NJU-LINK approved public Harbor hosting at the source revisions in `config.yaml`. The Harbor-side scores use the adapter's independent judge. The `original_parity` and `harbor_parity` directories contain the task-level records for each round. The `oracle` directory records the full Harbor run with 72/72 tasks at reward 1.0 and 1,474/1,474 passing cases, a replay of the saved terminal output, and a one-task smoke test against the license-inclusive package. The publication digest report in `results_collection` maps each evaluated package to its final manifest digest after adding the licensing README. [Adapter PR](https://github.com/harbor-index/adapters/pull/19) · [Dataset PR](https://github.com/harbor-framework/harbor-datasets/pull/302)

The full Oracle rerun on the exact GitHub submission also passed all 72 tasks and all 1,474 cases at 16-way concurrency. The audited trial digests match the committed dataset manifest. [Oracle rerun report](oracle/github-pr302-exact-oracle.json) · [Dataset commit](https://github.com/harbor-framework/harbor-datasets/pull/302/commits/b4fa685c0618e3fb34cd155730208a3f0ffd295e)

| Agent | Model | Metric | Runs | Dataset size | Original (mean ± SEM) | Harbor (mean ± SEM) |
| --- | --- | --- | --- | --- | --- | --- |
| codex@0.144.1 | gpt-5-mini | Task pass rate (%) | 3 | 24 of 72 | 11.1111 ± 1.3889 | 6.9444 ± 1.3889 |

The overall run-score ranges intersect at 8.33%. The overlap calculation uses the three individual scores on each side. [Harbor matching criterion](https://harborframework.com/docs/datasets/adapters#matching-criterion)
