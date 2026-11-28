# Validation evidence

`oracle-result.json` records the full 110-task Harbor oracle run. `nop-result.json`
records three missing-answer trials. `blocksworld-controls.json` records the VAL
positive and negative controls. `provenance.json` identifies the runtime, upstream
revision, and scope of these runs. These checks are not model parity experiments.

To rerun source-fidelity and selective-overwrite tests from this adapter directory:

```bash
PLANBENCH_SOURCE=/path/to/pinned/LLMs-Planning uv run python -m unittest discover -s tests
```

The source checkout must be at `fc638a1aff7df3fe7a1a1d289fa2c04cc24dc284`.
Without `PLANBENCH_SOURCE`, the two source tests are skipped; a skipped run is not
validation evidence.

`original-input-audit.json` records a separate 110-task input comparison and
original-side scoring check. The independently implemented scripts and commands
are available in the [original benchmark fork](https://github.com/Chenghao-Zhang/LLMs-Planning/tree/82256f992bf0e426719a2fb14e39090e872effc7/harbor_parity).
Use that fork's parity revision to reproduce these scripts, and the original
revision above when passing `--source-dir` to this adapter.
