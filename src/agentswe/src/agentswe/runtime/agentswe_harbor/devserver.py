from __future__ import annotations

import contextlib
import functools
import json
import secrets
import socketserver
from pathlib import Path

from . import brokers, settings

# Shared with the Builder through a volume: upstream's controller socket, its token and the builder broker token.
CONTROLLER_DIR = Path("/run/agentswe-controller")
# Reached only by the [[verifier.collect]] hook inside this container.
FREEZE_SOCKET = settings.STATE_DIR / "freeze.sock"


class FreezeHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        frozen = self.server.controller.freeze_after_builder_exit()
        self.wfile.write(json.dumps({"frozen": frozen}).encode() + b"\n")


def main() -> None:
    """Upstream's host-side dev controller (one_stop.DevController) inside the task: dev rounds run upstream's
    run_adapter_phase, which drives the task's own adapter.py; the delivery is frozen when the Builder exits."""
    # Before settings.prepare() points the judge variables at the judge broker.
    providers = brokers.take_providers("runtime", "judge", "builder")
    settings.prepare()
    adapter, one_stop = settings.import_upstream()
    log_dir = settings.RUN_DIR / "brokers"
    with contextlib.ExitStack() as stack:
        builder_token = secrets.token_urlsafe(32)
        brokers.role_broker(stack, "builder", providers["builder"], builder_token, bind="0.0.0.0",
                            port=settings.BUILDER_BROKER_PORT, log_dir=log_dir)
        (CONTROLLER_DIR / "builder-token").write_text(builder_token, encoding="utf-8")
        judge_credential = brokers.evaluation_brokers(stack, providers, log_dir)
        brokers.serve_relays(expose=True)
        trusted_digest = functools.cache(lambda: one_stop.environment_tree_digest(settings.TRUSTED_ENV_PREFIX))

        def evaluate(round_index: int, attempt_index: int, candidate: Path):
            return one_stop.run_adapter_phase(
                run_dir=settings.RUN_DIR, phase_id=f"harbor-dev-r{round_index:03d}-a{attempt_index:03d}",
                candidate=candidate, cases=adapter.DEV_CASES, benchmark=settings.TASK_DIR / "benchmark",
                env_prefix=settings.CANDIDATE_ENV_PREFIX, trusted_env_prefix=settings.TRUSTED_ENV_PREFIX,
                trusted_env_digest=trusted_digest(), credential_file=judge_credential,
                jobs_dir=settings.JOBS_DIR, n_concurrent=settings.N_CONCURRENT,
            )

        controller = one_stop.DevController(
            run_dir=settings.RUN_DIR, run_id="harbor", workspace_submission=Path("/workspace/submission"),
            workspace_environment=settings.CANDIDATE_ENV_PREFIX, max_rounds=int(settings.PROTOCOL["max_dev_rounds"]),
            evaluation_runner=evaluate,
        )
        controller.socket_path = Path(one_stop.CONTROLLER_CONTAINER_SOCKET)
        controller.start()
        (CONTROLLER_DIR / "token").write_text(controller.token, encoding="utf-8")
        FREEZE_SOCKET.unlink(missing_ok=True)
        with socketserver.UnixStreamServer(str(FREEZE_SOCKET), FreezeHandler) as server:
            FREEZE_SOCKET.chmod(0o600)
            server.controller = controller
            print(f"AgentSWE dev controller listening on {controller.socket_path}", flush=True)
            server.serve_forever()


if __name__ == "__main__":
    main()
