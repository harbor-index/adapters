# GPT-5 Mini parity reproduction

Status: historical provisional cohort; **not accepted frozen-configuration parity**. See [acceptance findings](acceptance.md). All three paired 24-task runs were recorded. Overall three-run score ranges overlap. Pilot excluded; model solve timeouts are judged without model reruns. Recovered transport errors occurred. 4 unusable attempts were archived and replaced under the original session-completion policy; their usage remains included in cost estimates. The repaired Harbor task and round 3 use the documented stdin-prompt driver patch; earlier valid attempts are retained. A separate sensitivity check counts the oversized-tool-argument attempt as its original zero: Harbor overall 18.0556 ± 3.6747%; overall ranges overlap. The contributor reports maintainer approval for GPT-5 Mini and supplied the 2077 key.

## Frozen inputs and runtime

- Harness: `NJU-LINK/GameLogicBench@10287322040bc72574f144cb40eccfd6ccb66936`.
- Tasks: `NJU-LINK/GameLogicBench-Tasks@82f51ab34860c0ab3fe63a0ce9ae394ad38234a6`.
- Adapter implementation: `600d5e0f8b6861bc5e92cff2429602205ac7c503`; subsequent reproduction documents do not change task generation or grading.
- Harbor `0.23.0`, Codex `0.144.1`, Godot `4.4.stable.official.4c311cbee`, Linux ARM64 on both sides. The repaired Harbor attempt and round 3 use the documented stdin-prompt driver patch below; earlier valid attempts retain the unpatched driver.
- API: `https://app.ppapi.ai/v1`, Responses protocol, actual model ID `gpt-5-mini` (Harbor's agent argument is `openai/gpt-5-mini`). The earlier proposed date-suffixed ID is not used.
- High reasoning effort, 400,000-token context window, original per-task CPU/memory settings and 3,600-second solve budget on both sides. Request retries: 4; stream retries: 8; stream idle timeout: 120,000 ms. Automatic framework-level whole-trial retries are disabled; documented infrastructure-invalid attempts are archived and replaced separately, including their usage in cost estimates.
- [24 fixed task IDs](../parity_task_ids.txt): within each kind, take the first eight tasks in ascending SHA-256 order of `gamelogicbench-parity-v1:<task_id>`. Selection precedes model evaluation and is independent of scores. All 472 upstream scenario/seed cells per run are frozen privately and matched between sides.
- Local Colima Linux backend, 6 vCPU / 16 GiB RAM; two model solves per side per round and two original judge workers per round. Daytona was not used. At `2026-11-20T09:52:10Z`, round 2 was started alongside round 1 after observing low CPU/memory use and API wait. At most two paired rounds overlap, for a total ceiling of eight model solves. Round 3 starts when either paired round completes. No active solve was interrupted, and the target remains 144 scored task runs with unchanged inputs, model settings, budgets and scoring; excluded infrastructure attempts are additional.

Use the pinned checkouts and base-image build from the adapter README, then build the [fixed Codex runtime](parity-runtime/README.md). On macOS, run the original harness on the Linux Docker host: its egress policy requires Linux `iptables`, root access and the host Docker daemon. Harbor can be scheduled from macOS against that same daemon. Set `DOCKER_HOST` explicitly when using a non-default Docker backend.

For the recorded overlap schedule, launch rounds 1 and 2 in separate terminals on both sides, then start round 3 after either paired round finishes. Each round has separate output directories. The loops below instead show sequential invocation for a smaller resource footprint; each round's settings are identical.

Rounds 1 and 2 exposed three invalid attempts: original `combo_formation_commit` (round 1) and `combo_push_commit` (round 2) exhausted response-stream reconnections; Harbor `atom_target_selection` (round 1) exited 143 when the agent's Godot `pkill -f` cleanup also matched the prompt in its launcher argv. The upstream Codex scaffold explicitly avoids this by feeding the prompt through a file/stdin. Harbor's [minimal driver patch](harbor-codex-stdin.patch) applies the same delivery method without changing prompt bytes, model settings or grading. Original invalid attempts and their usage are retained privately; valid losses and model-budget timeouts are not rerun. Third-round execution and these repairs overlap while remaining below eight model solves.

The final completeness gate stopped at 143/144 valid verdicts because Harbor round 3 `atom_light_occlusion` ended without `turn.completed`: a response was truncated with `max_output_tokens`, then the API rejected the returned tool arguments (1,115,572 characters, exceeding its 1,048,576-character field limit). The original scaffold also excludes sessions without a completion event. One replacement was declared under that policy, with all other 143 verdicts preserved. This is reported separately from transient transport failures: the discarded workspace's verifier reward was zero, and the final report includes a sensitivity calculation counting that original zero instead of the replacement. The excluded attempt remains in usage and cost estimates.

Before Harbor model runs, apply the patch to a Harbor 0.23.0 environment and check its real command builder without calling the API. Use that environment's Python interpreter, from the adapter directory:

```bash
python - <<'PY'
from pathlib import Path
import harbor
import hashlib
import json
import subprocess

site_packages = Path(harbor.__file__).resolve().parent.parent
patch = Path("validation/harbor-codex-stdin.patch").resolve()
record = json.loads(Path("validation/harbor-codex-stdin-provenance.json").read_text())
driver = site_packages / "harbor/agents/installed/codex.py"
assert hashlib.sha256(driver.read_bytes()).hexdigest() == record["baseline_sha256"]
subprocess.run(["patch", "-p1", "-d", str(site_packages), "-i", str(patch)], check=True)
assert hashlib.sha256(driver.read_bytes()).hexdigest() == record["patched_sha256"]
PY
python validation/check_codex_stdin.py
```

The [patch provenance](harbor-codex-stdin-provenance.json) records the unmodified and patched driver hashes. The adapter generator, runtime image, Codex executable, frozen inputs and verifier are unchanged. Oracle does not use this driver and does not need to be rerun.

## Harbor side

From `src/gamelogicbench`, with `GLB_HARNESS` and `GLB_TASKS` pointing at the pinned clean checkouts:

```bash
uv run gamelogicbench --harness-dir "$GLB_HARNESS" --tasks-dir "$GLB_TASKS" \
  --output-dir ../../datasets/gamelogicbench-parity --split parity \
  --parity-task-file "$PWD/parity_task_ids.txt" --allow-host app.ppapi.ai \
  --runtime-image gamelogicbench-parity:codex-0.144.1

# Supply OPENAI_API_KEY privately; do not put it in shell history or commit it.
unset CODEX_FORCE_AUTH_JSON CODEX_AUTH_JSON_PATH
export OPENAI_BASE_URL=https://app.ppapi.ai/v1
for run in 1 2 3; do
  harbor run -c run_gamelogicbench_parity.yaml --job-name "run-$run" || break
done
```

Use a new job directory for a new experiment; do not overwrite existing results. The [Codex config](parity-codex.toml) reads `OPENAI_API_KEY` and does not use a personal ChatGPT login. Agent egress is restricted to the model endpoint; verification remains offline.

## Original side

Install the pinned upstream harness dependencies on the Linux Docker host. Set `GLB_PARITY_TASK_IDS` to an absolute copy of the same 24-ID file, and supply `OPENAI_API_KEY` privately to the setup process. From the harness root, write its ignored private registry without printing the credential:

```python
import os
from pathlib import Path
import yaml

path = Path("conf/models/default.yaml")
assert not path.exists(), "Keep an existing private registry; add this entry manually instead"
entry = {"api_type": "openai-responses", "id": "gpt-5-mini",
         "base_url": "https://app.ppapi.ai/v1", "api_key": os.environ["OPENAI_API_KEY"],
         "wire_api": "responses", "context_window": 400000,
         "defaults": {"reasoning_effort": "high"}}
with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
    yaml.safe_dump({"models": {"gpt-5-mini": {"scaffolds": {"codex": entry}}}}, output)
```

The pinned loader uses `yaml.safe_load`; it does **not** interpolate `${oc.env:...}` placeholders. Keep the registry private. Run with Linux-host privileges sufficient to install the upstream egress allowlist:

```bash
for run in 1 2 3; do
  python -m geb.cli command=run \
    "task=[$(paste -sd, "$GLB_PARITY_TASK_IDS")]" \
    solution=gpt-5-mini scaffold=codex effort=high \
    image=gamelogicbench-parity:codex-0.144.1 \
    solve.concurrency=2 judge.concurrency=2 sample_num=1 \
    "result_dir=$PWD/parity-original-run-$run" || break
done
```

## Validation and reporting

The independent pilot used `atom_move_navigation`, `atom_dodge_hold`, `combo_jump_reach_graph` and `repo_slayrobot_deck_ledger`. Both sides passed 1/4 whole tasks, with zero final runtime exceptions. Three actual model submissions were also cross-graded in the opposite harness: 66/66 cell verdicts agreed. Pilot outputs are excluded from the three formal runs.

Each task scores 1 only if every frozen scenario/seed cell passes. Missing, duplicate or unusable judge cells are unresolved infrastructure results, never model zeros. A model solve timeout is retained and its remaining workspace is judged, following the original harness; it is not rerun to improve the score. Publish three raw task-pass rates per side and their mean ± sample SEM overall and per kind. Range overlap uses the minimum and maximum of the three run scores. Explicitly flag categories where both sides score zero.

Both CLIs emit an unknown-model metadata warning; the context window is explicitly set on both sides. Formal logs also contain transient stream disconnections, idle timeouts and a `Service Unavailable` response handled by the pinned CLI retry policy. Report final failures separately from recovered transport errors. Exact 2077 billing is unavailable; token-based cost estimates are not an invoice. Detailed logs, credentials, generated games and hidden judge inputs remain private.
