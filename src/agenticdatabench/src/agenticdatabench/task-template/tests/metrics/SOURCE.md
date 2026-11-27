# Source of these files

Upstream's grader, copied from AgenticDataBench
(https://github.com/AgenticDataBench/AgenticDataBench, Apache-2.0, see LICENSE).

- `__init__.py`, `table.py`, `text.py`, `ml.py`, `script/`: unchanged from
  `testbed/da_agent/evaluators/metrics/` at commit `e81dda8`.
- `myjson.py`, `image.py`: **modified**. They are the files from `e81dda8` with three
  grader fixes (commit `9ced93d` on the adapter's fork branch `harbor-adapter`):
  1. `compare_json`: an empty gold list or dict is matched by an empty output
     (it scored 0 before). Affects loan_model_18 and loan_model_20.
  2. `compare_json`: with no thresholds, every top-level gold field is compared with
     the default rule (no field was compared, so the score was always 0). Affects
     real_estate_29 and ecommerce_35.
  3. `compare_image`: a subplot that neither the result nor the gold `.npy` holds data
     for (Plotprocess saves none for KDE plots) is judged on its figure properties
     alone (it always failed before). Affects ecommerce_21 and healthcare_26.

`../da_utils.py` is `testbed/da_agent/envs/utils.py` at `e81dda8`, unchanged (for
`timeout`). `../../environment/image.py` is `testbed/da_agent/configs/scripts/image.py`
at `e81dda8`, unchanged.
