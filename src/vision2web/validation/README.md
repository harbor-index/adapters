# Local validation of the Vision2Web draft

These checks use **no model calls** and do not establish oracle pass rate or parity.
The receipts are compact summaries; generated data, public website code and screenshots are not committed here.

The full catalog contains 193 projects. Generation and runtime checks below exercise three real projects, one per level. Full archive/full-generation validation remains pending.

## Generation and local controls

`uv run pytest -q` runs catalog, selection, path-safety, score aggregation and API opt-in controls.
Set `VISION2WEB_TEST_UPSTREAM` and `VISION2WEB_TEST_DATA` to enable the real three-project generation check. It checks byte-identical coding prompts, hidden workflows, stable names, repeat generation, resource settings and upstream-source hashes.

The task data and source pins are in `src/vision2web/pins.py` and `data/`.

## Original public-candidate deployment pilot

See [pilot.json](pilot.json). Three tasks were attempted using four public implementations:
the Claude Film Movement submission lacks `src/data` and fails database seeding;
the alternative Codex submission deployed. The three successful sites returned HTTP 200 over 16 direct page/viewport captures.

These direct page checks did not execute the L2/L3 GUI-agent workflow.
RecipeCloud adds CAPTCHA/cookie UI absent from the prototype. Anomali initially hides 14 reveal blocks until scrolling. No numerical visual quality score is assigned.

## Generated-task capture check

See [generated-captures.json](generated-captures.json).
The generated verifier images deploy each reference as UID 1000 without judge credentials.
The capture-only entrypoint captures L1's three viewports and L2/L3's initial homepage nodes.
It checks no later GUI node, writes no reward and returns `benchmark_reward: null`.

Reproduction from `src/vision2web/`:

```bash
uv run vision2web --download --output-dir /tmp/v2w-all
uv run python tools/fetch_references.py --output-dir /tmp/v2w-references
uv run vision2web --split pilot --reference-dir /tmp/v2w-references --output-dir /tmp/v2w-pilot
uv run python tools/capture.py /tmp/v2w-pilot/vision2web-webpage-recipecloud --output-dir /tmp/v2w-capture-l1
uv run python tools/capture.py /tmp/v2w-pilot/vision2web-frontend-anomali --output-dir /tmp/v2w-capture-l2
uv run python tools/capture.py /tmp/v2w-pilot/vision2web-website-filmmovement --output-dir /tmp/v2w-capture-l3
```

Build the runtime as documented in the main README first. The download step is the complete approximately 4.11 GB archive transfer; existing extracted inputs can be supplied with `--dataset-dir` instead.
The reference fetcher uses the fixed leaderboard revision and does not infer oracle quality.

## Remaining work

- Confirm continuous-score oracle acceptance, judge models/endpoints and API budget.
- Validate the live GUI/VLM code paths and error accounting with team-provided access.
- Finish original-side matching of the verifier UID and protected-output path changes.
- Verify full task generation, complete references, then matched model parity and registry publication.

The repository validator's missing `parity_experiment.json` finding is expected for this WIP; no measured parity records have been fabricated.
