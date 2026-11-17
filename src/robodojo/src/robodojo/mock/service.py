"""Entry point of the mock simulator container: a CPU picture-generating service.

It takes the place of ``direct/supervisor.py`` and of the native simulator for
the mock task only. It hosts the real broker and HTTP bridge, runs the real
episode loop against ``MockEnv``, and writes results in the native layout, so
the agent, the bridge client and the verifier behave as they do with Isaac Sim.

What it does not exercise: Isaac Sim, the pinned image, the native policy
WebSocket, the robot's inverse kinematics, and native recovery from invalid
layouts. Its scores are about an invented task and say nothing about RoboDojo.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any
import uuid

from ..catalog import MOCK_LABEL, MOCK_POLICY_NAME
from ..direct.broker import Broker, make_server
from ..direct.episode import DirectAbort, run_episode
from ..direct.policy.model import Model
from ..runtime import EvaluationError, load_spec, write_json
from .scene import MockEnv, MockKinematics

BRIDGE_PORT = 19100


def run(spec_path: Path, results_dir: Path, *, host: str = "0.0.0.0", port: int = BRIDGE_PORT) -> dict[str, Any]:
    """Serve the bridge and play every mock episode. Returns the final verdict."""
    results_dir = results_dir.resolve()
    if results_dir.exists() and any(results_dir.iterdir()):
        raise EvaluationError("Results directory must be empty at startup")
    results_dir.mkdir(parents=True, exist_ok=True)
    spec = load_spec(spec_path)
    if not spec.mock:
        raise EvaluationError("The mock service only runs the mock task")
    # The policy model is the real one; here it is called in-process instead
    # of through the native policy WebSocket.
    model = Model({}, broker=Broker(results_dir), serve=False)
    server = make_server(model.broker, host, port)
    threading.Thread(target=server.serve_forever, name="harbor-bridge", daemon=True).start()

    def call(func_name: str, obs: Any = None) -> Any:
        method = getattr(model, func_name)
        return method() if obs is None else method(obs)

    variants = []
    for variant in spec.variants:
        details = {}
        for index in range(variant.episodes):
            env = MockEnv(variant.name, layout_id=index, eval_seed=spec.seed)
            run_episode(env, call, kinematics_factory=MockKinematics)
            details[str(index)] = {
                "layout_id": index, "success": bool(env.success[0]), "score": float(env.score()),
            }
        scores = [detail["score"] for detail in details.values()]
        destination = results_dir / "native" / variant.name / "_result.json"
        destination.parent.mkdir(parents=True)
        # Same layout as the native result file, including its 0-100 total score.
        write_json(destination, {
            "eval_time": variant.episodes,
            "success_rate": sum(detail["success"] for detail in details.values()) / variant.episodes,
            "score": sum(scores) / variant.episodes * 100,
            "details": details,
        })
        variants.append({
            "name": variant.name, "episodes": variant.episodes, "run_id": f"mock_{uuid.uuid4().hex}",
        })
    return {
        "status": "completed", "mock": True, "task_id": spec.task_id, "dimension": spec.dimension,
        "seed": spec.seed, "upstream_revision": spec.upstream_revision,
        "xpolicylab_revision": MOCK_LABEL, "policy_name": MOCK_POLICY_NAME, "variants": variants,
    }


def evaluate(spec_path: Path, results_dir: Path, *, host: str = "0.0.0.0", port: int = BRIDGE_PORT) -> dict[str, Any]:
    """Run the mock evaluation and always leave a verdict for the bridge to report."""
    try:
        verdict = run(spec_path, results_dir, host=host, port=port)
    except DirectAbort as exc:  # The agent side stopped the run.
        verdict = {"status": "failed", "mock": True, "error": f"aborted: {exc}"}
    except (EvaluationError, OSError) as exc:
        verdict = {"status": "failed", "mock": True, "error": str(exc)}
    if verdict["status"] != "completed":
        print(f"RoboDojo mock evaluation failed: {verdict['error']}", file=sys.stderr, flush=True)
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
        write_json(results_dir / "supervisor.json", verdict)
    except OSError as exc:
        print(f"Cannot write the mock verdict: {exc}", file=sys.stderr, flush=True)
    return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    home = Path(os.environ.get("HARBOR_ROBODOJO_HOME", "/opt/harbor-robodojo"))
    parser.add_argument("--spec", type=Path, default=home / "task_spec.json")
    parser.add_argument("--results-dir", type=Path, default=Path("/harbor-robodojo/results"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=BRIDGE_PORT)
    parser.add_argument("--exit-when-done", action="store_true",
                        help="Exit instead of idling for artifact collection")
    args = parser.parse_args(argv)
    verdict = evaluate(args.spec, args.results_dir, host=args.host, port=args.port)
    if not args.exit_when_done:
        while True:  # Keep the bridge up so the agent reads the final status.
            time.sleep(3600)
    return 0 if verdict["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
