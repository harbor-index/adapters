# Train a predictor for $dataset_name

Fit a supervised predictor using only `/workspace/data/train.json` and create a frozen Python prediction bundle in `/workspace/submission/`.
The authoritative JSON has `columns` and `data`; JSON null is missing. Preserve literal categorical strings such as `NA`, `nan`, and empty strings. `train.csv` is a convenience export. `/workspace/data/metadata.json` describes features, declared categorical levels, target `$target`, classes and metric.

Problem: $problem_type. Native error is 1-AUROC for binary, multiclass log loss, or RMSE for regression. Lower is better; there is no additional normalization. Use training-only validation and preprocessing. Your system owns its fitting procedure; no prescribed HPO/bagging pipeline is added.

Required output: `/workspace/submission/predict.py` and any fitted artifacts it needs. Complete all fitting now. The test feature batch is withheld during fitting; the frozen bundle is later run in an isolated prediction process as:

```
python predict.py QUERY_DIRECTORY OUTPUT_JSON
```

`QUERY_DIRECTORY/test.json` has the same `columns`/`data` format, with feature columns only and native-shuffled, reset-indexed test rows. `QUERY_DIRECTORY/metadata.json` repeats public metadata. Predict in exactly the supplied row order. `OUTPUT_JSON` must contain exactly `classes` and `predictions`: `classes` must equal $classes_json in that order; `predictions` is N by K probabilities for classification (binary includes both class columns) or N by 1 numeric predictions for regression. Values must be finite; probability rows must sum to one. Return JSON numbers, not labels. Do not change the fitted model or learn from withheld labels. The grader restores native row order and scores using protected labels.

Any Python predictor supported by the installed libraries is permitted; ONNX export is optional. The environment includes numpy, pandas, scikit-learn, scipy, joblib and AutoGluon core. No extra packages or public dataset downloads are available. Training has 600 seconds, two CPUs and 4 GiB. Prediction has a 180-second wall limit, bounded memory, no network, a read-only bundle and a writable scratch directory. Resolve artifacts relative to `__file__` or the bundle working directory, not training-time absolute paths. Keep the bundle at most 256 MiB and use only ordinary files/directories.

For a trained system, include a reproducible `train.py` in the bundle and execute it during this session. Before finishing, exercise `predict.py` on a small training-derived validation batch in the documented JSON format. The final grader does not import your Python into its own process. It reports native error and a separate exact native-optimum diagnostic; an imperfect trained model is still a valid scored result.
