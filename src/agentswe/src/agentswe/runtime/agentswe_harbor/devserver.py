from __future__ import annotations

import importlib
import json
import os
import secrets
import socketserver
import threading
from pathlib import Path

from . import settings

# Fixed by upstream one_stop.py (CONTROLLER_CONTAINER_SOCKET); shared with the Builder through a volume.
CONTROLLER_DIR = Path("/run/agentswe-controller")
CONTROL_SOCKET = settings.STATE_DIR / "control.sock"
WORKSPACE_SUBMISSION = Path("/workspace/submission")


def max_dev_rounds(one_stop) -> int:
    if os.environ.get("AGENTSWE_PROTOCOL_MODE") == "smoke":
        return int(settings.TASK["smoke"]["max_dev_rounds"])
    return int(one_stop.MAX_DEV_ROUNDS)


def build_controller():
    from . import pipeline

    pipeline.upstream_adapter()
    one_stop = importlib.import_module("one_stop")
    dev_cases = list(settings.PROTOCOL["dev_cases"])

    def evaluate(round_index: int, attempt_index: int, candidate: Path):
        phase = f"dev-r{round_index:03d}-a{attempt_index:03d}"
        return pipeline.run_phase(phase, candidate, dev_cases)

    token = secrets.token_urlsafe(32)
    controller = one_stop.DevController(
        run_dir=settings.STATE_DIR / "run",
        run_id="harbor",
        workspace_submission=WORKSPACE_SUBMISSION,
        workspace_environment=settings.CANDIDATE_ENV_PREFIX,
        max_rounds=max_dev_rounds(one_stop),
        evaluation_runner=evaluate,
        token=token,
    )
    controller.socket_path = Path(one_stop.CONTROLLER_CONTAINER_SOCKET)
    return controller, token


def serve_control(controller) -> None:
    """Private channel for the [[verifier.collect]] hook (not reachable from the Builder)."""

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            request = json.loads(self.rfile.readline().decode("utf-8") or "{}")
            if request.get("action") != "freeze":
                response = {"error": "unknown_action"}
            else:
                frozen = controller.freeze_after_builder_exit()
                response = {"frozen": frozen}
            self.wfile.write(json.dumps(response).encode("utf-8") + b"\n")

    CONTROL_SOCKET.unlink(missing_ok=True)
    server = socketserver.ThreadingUnixStreamServer(str(CONTROL_SOCKET), Handler)
    CONTROL_SOCKET.chmod(0o600)
    server.serve_forever()


def start_builder_broker() -> None:
    """Upstream builder role broker: pins model/effort, resends on replay and turns 429 into 503."""
    from . import pipeline

    if "builder" not in settings.PROVIDERS:
        return
    token = secrets.token_urlsafe(32)
    log_dir = settings.STATE_DIR / "builder-broker"
    log_dir.mkdir(parents=True, exist_ok=True)
    pipeline.start_role_broker(
        "builder", settings.PROVIDERS["builder"], token, bind="0.0.0.0",
        port=settings.BUILDER_BROKER_PORT, log_dir=log_dir,
        extra_args=["--replay-policy", "resend", "--rate-limit-policy", "as_503"],
    )
    (CONTROLLER_DIR / "builder-token").write_text(token, encoding="utf-8")


def main() -> None:
    settings.apply_process_environment()
    (settings.STATE_DIR / "run").mkdir(parents=True, exist_ok=True)
    controller, token = build_controller()
    controller.start()
    (CONTROLLER_DIR / "token").write_text(token, encoding="utf-8")
    start_builder_broker()
    (CONTROLLER_DIR / "ready").write_text("ready\n", encoding="utf-8")
    control = threading.Thread(target=serve_control, args=(controller,), daemon=True)
    control.start()
    print(f"AgentSWE dev controller listening on {controller.socket_path}", flush=True)
    control.join()


if __name__ == "__main__":
    main()
