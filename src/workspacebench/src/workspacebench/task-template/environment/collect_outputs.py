"""Collect the agent's outputs inside the agent container (a Harbor collect hook).

Runs after the agent phase and before artifact collection, so it still sees the
agent's /app. It is upstream's own collector (agent_runner @ 3fbd0f1, in
/opt/wsb):
  - files written outside model_output are mirrored into it,
  - then the expected names, the agent's final path list and model_output
    decide which files are graded.
The result goes to /logs/wsb_collected/, which task.toml declares as an
artifact. The separate verifier container gets only that folder, so the judge
never runs inside the agent's container.

The expected output names come from the hook's command line (base64 JSON of the
task metadata's output_files / output_file / output_manifests fields only).
They are never stored in the image, so the agent cannot read them.
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/opt/wsb/evaluation/src")
import agent_runner as ar  # noqa: E402  (upstream)

WORK_DIR = "/app"
OUTPUT_DIR = "model_output"
AGENT_LOG = Path("/logs/agent/claude-code.txt")
OUT = Path("/logs/wsb_collected")


def stream_events(text: str):
    for line in text.splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


def final_text(text: str) -> str:
    result = ""
    for e in stream_events(text):
        if e.get("type") == "result":
            result = e.get("result") or ""
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--meta-b64", required=True, help="base64 JSON with the metadata's output fields")
    a = p.parse_args()
    meta = json.loads(base64.b64decode(a.meta_b64).decode("utf-8"))

    snap = json.loads(Path("/opt/wsb/before.json").read_text())
    before = {k: (int(v[0]), float(v[1])) for k, v in snap["files"].items()}
    min_mtime = float(snap["created"])
    stream = AGENT_LOG.read_text(errors="replace") if AGENT_LOG.exists() else ""

    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "raw").mkdir(parents=True)
    (Path(WORK_DIR) / OUTPUT_DIR).mkdir(exist_ok=True)

    expected = ar._expected_output_files(meta)
    ar._copy_misplaced_outputs_to_target(
        work_dir=WORK_DIR, task_target_output_dir=OUTPUT_DIR, before=before,
        min_mtime=min_mtime, raw_dir=str(OUT / "raw"), expected_files=expected,
    )
    paths, method = ar._collect_output_paths(
        task_target_output_dir=OUTPUT_DIR, work_dir=WORK_DIR, expected_files=expected,
        returned_paths=[], last_text=final_text(stream), min_mtime=min_mtime,
    )
    manifest = ar._copy_outputs(output_paths=paths, out_dir=str(OUT / "output"), preserve_root=f"{WORK_DIR}/{OUTPUT_DIR}")
    if AGENT_LOG.exists():
        shutil.copy(AGENT_LOG, OUT / "claude-code.txt")
    (OUT / "collect.json").write_text(json.dumps({"outputs": manifest, "retrievalMethod": method}, ensure_ascii=False, indent=2))
    print(f"collected {len(manifest)} output file(s) via {method}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
