# Native oracle cross-validation

This utility runs the known-answer oracle through upstream `ExternalSystemModel.fit_custom`, its test permutation and inverse permutation, and `experiment_runner.evaluate` with the native training-derived `LabelCleaner`. It checks oracle execution and scoring, not learned-model performance or formal parity.

Use **`src/tabarena` as the working directory throughout**. Generate tasks as described in the README, then create an isolated native environment and obtain the pinned source:

```bash
uv venv --python 3.12 .venv-native
uv pip install --prerelease=allow --python .venv-native/bin/python \
  'numpy==2.2.6' 'pandas==2.2.3' 'scikit-learn==1.7.2' \
  'autogluon.tabular==1.6.4b20261109' 'loguru==0.8.0' \
  'openml==0.15.1' 'huggingface-hub==2.4.0'
git clone https://github.com/autogluon/tabarena.git .native-source
git -C .native-source checkout bfa8c50c17885e5b722d75ce5bcb5db3822257a3
PYTHONPATH="$PWD/.native-source/packages/tabarena/src:$PWD/.native-source/packages/bencheval/src" \
  .venv-native/bin/python scripts/native_oracle_check.py \
  --task-dir datasets/tabarena/tabarena-v01-363621-r0-f0 \
  --output-dir native-results/binary
```

The upstream and adapter packages share the import name `tabarena`; the explicit `PYTHONPATH` selects upstream. The utility checks SHA256 hashes of the imported lifecycle, permutation and evaluator source files, and the generated oracle predictor against this adapter's template. Each output directory must be new.

Optionally add `--harbor-verifier-dir jobs/JOB/TRIAL/verifier`, replacing `JOB/TRIAL` with an actual trial containing `result.json` and `predictions.json`. This requires identical prediction arrays and native errors within `1e-12`. Outputs retain source hashes, predictions, native timing and comparison results.

The oracle deliberately copies privileged reference outputs from `solution/` during native fit. Test labels are not inferred from training data. Typed JSON preserves missing values and categorical strings; scoring uses the same class ordering and exact native optimum as the Harbor verifier.

This local utility executes only the hash-checked reference predictor, makes no model or Modal calls, and is **not a sandbox for arbitrary submitted code**. Native process-memory inspection requires ordinary process access. Local timing does not establish equivalence to the protected Linux verifier; formal matched-system parity remains separate.
