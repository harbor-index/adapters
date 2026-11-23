# Third-Party Licenses and Data Provenance

## Upstream Code License (MIT)

Parts of this adapter—specifically prompt formatting, ranking parsing,
Spearman correlation scoring, UniProt-nested macro aggregation, and release
bundle verification logic in
`src/pg_llm/adapter.py`, `src/pg_llm/score.py`, and
`src/pg_llm/task-template/tests/grade.py`—are adapted from
[`rohitarorayyc/proteingym-llm`](https://github.com/rohitarorayyc/proteingym-llm)
under the MIT License:

```text
MIT License

Copyright (c) 2026 Rohit Arora, Leo Tianlai Chen, Melissa Du, Debora S. Marks, and George M. Church

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Evaluation Data Provenance and Use Terms

- **Frozen release bundle**: `eval-data-v1.2` (`proteingym-llm-eval-v1.2.tar.gz`)
  from `rohitarorayyc/proteingym-llm`.
- **Upstream ProteinGym assays**: ProteinGym is maintained by the Marks Lab
  and OATML (`OATML-Markslab/ProteinGym`). Individual ProteinGym v1.3
  experimental DMS assays remain attributable to their original authors and
  retain their original dataset terms; the upstream repository MIT code license
  does not supersede those assay terms.
- **Evaluation-only use**: In accordance with upstream `DATA.md` and
  `BENCHMARK_USE_POLICY.md`, the released `eval-data-v1.2` candidate splits and
  held-out DMS labels are strictly for evaluation. They must not be used for
  pretraining, continued pretraining, fine-tuning, distillation, retrieval
  augmentation (RAG), prompt optimization, or benchmark-specific model/agent
  tuning. Any model or system exposed to these splits during training or tuning
  must disclose the exposure and is ineligible for clean PG-LLM comparisons.
