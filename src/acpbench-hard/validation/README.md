# Offline validation

These checks invoke the unchanged upstream evaluator against generated task records. They complement, and do not replace, a full Harbor oracle run.

From the adapters repository root, after generating `datasets/acpbench-hard`:

```bash
docker build -t acpbench-hard-verifier-check \
  datasets/acpbench-hard/acpbench-hard-app-1044594258043400716/tests
mkdir -p validation/acpbench-hard
docker run --rm \
  -v "$PWD/datasets/acpbench-hard:/dataset:ro" \
  -v "$PWD/validation/acpbench-hard:/results" \
  -v "$PWD/src/acpbench-hard/validation/check_oracles.py:/check.py:ro" \
  acpbench-hard-verifier-check python /check.py
docker run --rm \
  -v "$PWD/datasets/acpbench-hard:/dataset:ro" \
  -v "$PWD/validation/acpbench-hard:/results" \
  -v "$PWD/src/acpbench-hard/validation/check_controls.py:/check.py:ro" \
  acpbench-hard-verifier-check python /check.py
```

`check_oracles.py` records all 1,040 task-level results, including exceptions. `check_controls.py` checks a reference, a wrong answer, and an empty answer in each of eight categories. The verifier must use `kstar-planner==1.6.0`: the older 1.4.1 return schema is incompatible with the upstream fallback scorer.

The initial Harbor run using 1.4.1 was stopped after identifying that incompatibility; it is not final evidence. The replacement job is `acpbench-hard-oracle-full-v2`.

The independent original-side preparation matches all 1,040 prompts and evaluator records. Its standalone scoring entry point also passes all 1,040 saved reference answers and scores all 1,040 missing answers as zero. `original-input-audit.json` records this audit and the scorer source hash. Model parity remains pending.
