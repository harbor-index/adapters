"""Verifier for the Harbor task: score the sealed evidence collected from the simulation sidecar.

Runs in the separate verifier container (the only place, besides the simulator, that holds the instance truth).
Writes /logs/verifier/reward.txt (1 if the task is completed, else 0) and, alongside, details.json (with the process
score, 0..1, the six process levels of the paper), evaluation.json and process_score.json.
"""
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

sys.path.insert(0, "/opt/el")
# Reference optima computed when the dataset was generated (optimization experiments only); the evaluator reads them
# from its cache instead of recomputing them here.
for variable, name in (("EMBODIEDLAB_FILTER_ASTAR_CACHE", "filter_astar.json"),
                       ("EMBODIEDLAB_TRUSS_PSTAR_CACHE", "truss_pstar.json"),
                       ("EMBODIEDLAB_ABSORBER_ASTAR_CACHE", "absorber_astar.json")):
    if Path(f"/opt/el/reference_cache/{name}").is_file():
        os.environ[variable] = f"/opt/el/reference_cache/{name}"
from benchmark.evaluation.evaluate import evaluate  # noqa: E402
from benchmark.evaluation.process import score_trial  # noqa: E402
from benchmark.instances import snapshot as instance_snapshot  # noqa: E402

CAPTURE = Path("/opt/embodiedlab/capture")
INSTANCE = Path("/opt/el/instance.json")
OUT = Path("/logs/verifier")
OUT.mkdir(parents=True, exist_ok=True)

trial = Path("/tmp/trial")
shutil.rmtree(trial, ignore_errors=True)
(trial / "captures").mkdir(parents=True)
if CAPTURE.is_dir():
    shutil.copytree(CAPTURE, trial / "captures" / "env")
shutil.copy(INSTANCE, trial / "instance.json")
instance_id = json.loads(INSTANCE.read_text())["instance_id"]

summary = {"reward": 0, "process_score": 0.0}
details = {"capture_found": CAPTURE.is_dir()}
sealed = [p for p in (trial / "captures").glob("*/sealed") if (p / "manifest.json").is_file()]
details["sealed"] = bool(sealed)
if sealed:
    try:
        report = evaluate(sealed[0], instance_snapshot.for_evaluation(trial, None, instance_id))
        (trial / "evaluation.json").write_text(json.dumps(report, default=str))
        (OUT / "evaluation.json").write_text(json.dumps(report, indent=2, default=str))
        details.update({k: report.get(k) for k in ("G", "e", "e_rel", "task_completed")})
        details["failed_gates"] = [k for k, v in (report.get("gates") or {}).items() if not v]
        summary["reward"] = 1 if report.get("task_completed") else 0
    except Exception as exc:
        details["evaluation_error"] = f"{type(exc).__name__}: {exc}"
        (OUT / "evaluation_traceback.txt").write_text(traceback.format_exc())
try:
    score = score_trial(trial, None)
    (OUT / "process_score.json").write_text(json.dumps(score, indent=2, default=str))
    summary["process_score"] = round(float(score["process_score"]), 4)
    details["process_level"] = score.get("level")
except Exception as exc:
    details["process_error"] = f"{type(exc).__name__}: {exc}"

details["process_score"] = summary["process_score"]
(OUT / "reward.txt").write_text(f"{summary['reward']}\n")
(OUT / "details.json").write_text(json.dumps(details, indent=2, default=str))
print(json.dumps({**summary, **details}, default=str))
