# DeceptionBench source attribution and notices

This adapter incorporates material from **DeceptionBench: A Comprehensive Benchmark for AI Deception Behaviors in Real-world Scenarios**, by **Yao Huang, Yitong Sun, Yichi Zhang, Ruochen Zhang, Yinpeng Dong, and Xingxing Wei**.

- Official source: <https://github.com/Aries-iai/DeceptionBench>
- Source commit: `8f3175f66fdff89ded3ebdf0ee2055fa47c955db`
- Dataset: <https://huggingface.co/datasets/skyai798/DeceptionBench>, revision `38b412da565073483c0a68ce9e04eb15859beedc`; `dataset.py` records each file's SHA-256 hash.
- Paper: <https://arxiv.org/abs/2510.15501>

The upstream README declares the codebase licensed under [Creative Commons Attribution-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-sa/4.0/) and separately states that DeceptionBench is for academic research and that commercial use in any form is prohibited. Both notices are retained here. The adapter does not resolve the tension between them or confer rights beyond those granted by the original authors.

Borrowed material:

- Original scenario text and fields from the five dataset JSON files, preserved in generated tasks.
- The evaluation prompt from `processor/eval_single_turn.py`, copied verbatim into `protocol.py`.
- The response cleanup and exact response suffix from `utils.py`:

  ```text
  (Please say your thought and final response in a json format: {"thought":"","response":""})
  ```

- Single-turn generation/evaluation record structure and `honest`/`decept` label interpretation. The reproduction bridge executes checksum-verified original functions.

Changes include Harbor packaging, deterministic task selection, a public prompt file, an optional CLI file-delivery instruction, a native API agent, isolated verification, artifact handling, and explicit infrastructure-error reporting. Original prompt content and valid-label scoring semantics are preserved. Upstream aggregate filename parsing is bypassed by retaining raw per-condition labels. The generated groundtruth-based oracle candidate is an adapter addition and remains unvalidated.

Adapter author: YuCheng (Tom) Yuan <tomyuanyucheng@gmail.com>.
