<!-- adapter-validation-bot -->
Adapter contract v1 (`docs/adapters.mdx`).

## ✅ Adapter Validation: `gamelogicbench`

✅ 30 passed | ❌ 0 errors | ⚠️ 4 warnings

### Warnings (recommended)

- ⚠️ **Parity PR link null**: Entry 0: `adapter_pr` is null — fill in the PR URL(s). — `src/gamelogicbench/parity_experiment.json`:11
- ⚠️ **Parity PR link null**: Entry 0: `dataset_pr` is null — fill in the PR URL(s). — `src/gamelogicbench/parity_experiment.json`:12
- ⚠️ **Parity PR link null**: Entry 0: `parity_pr` is null — fill in the PR URL(s). — `src/gamelogicbench/parity_experiment.json`:13
- ⚠️ **Metadata: parity_costs**: `parity_costs` is missing or null — fill in a USD cost estimate when available (canonical format: a string such as `$150`). — `src/gamelogicbench/adapter_metadata.json`

<details>
<summary>Passed checks (30)</summary>

- ✅ `README.md` exists
- ✅ `parity_experiment.json` exists
- ✅ `adapter_metadata.json` exists
- ✅ `src/gamelogicbench/` package exists
- ✅ `src/gamelogicbench/adapter.py` exists
- ✅ `src/gamelogicbench/main.py` exists
- ✅ `src/gamelogicbench/task-template/` directory exists
- ✅ `src/gamelogicbench/task-template/task.toml` exists
- ✅ `src/gamelogicbench/task-template/instruction.md` exists
- ✅ `src/gamelogicbench/task-template/environment/Dockerfile` exists
- ✅ `src/gamelogicbench/task-template/tests/test.sh` exists
- ✅ `src/gamelogicbench/task-template/solution/solve.sh` exists
- ✅ Template `[task].name` present
- ✅ Template `[task].authors` present
- ✅ `parity_experiment.json` is valid JSON array
- ✅ `adapter_metadata.json` is valid JSON array
- ✅ README section `Overview` present
- ✅ README section `What is` present
- ✅ README section `Adapter Features` present
- ✅ README section `Generated Task Structure` present
- ✅ README section `Run Evaluation` present
- ✅ README section `Usage` present
- ✅ README section `Parity` present
- ✅ README section `Notes & Caveats` present
- ✅ README section `Installation / Prerequisites` present
- ✅ README section `Citation` present
- ✅ README section `Authors & Contributions` present
- ✅ Parity table column count correct
- ✅ `test.sh` writes to reward path
- ✅ No canary strings found

</details>
