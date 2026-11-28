from __future__ import annotations

import json
import socketserver
from pathlib import Path

from . import brokers, harbor, settings, trial


class TrialHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request = json.loads(self.rfile.readline())
        trial.run_trial(Path(request["task_dir"]), Path(request["trial_dir"]))
        self.wfile.write(b"done\n")


def main() -> None:
    """The dev evaluator's second job slot: same image, its own filesystem for the fixed paths a per-case job
    uses, the evaluator's brokers reached through the relayed ports."""
    settings.prepare()
    brokers.serve_relays(expose=False)
    harbor.WORKER_SOCKET.unlink(missing_ok=True)
    with socketserver.UnixStreamServer(str(harbor.WORKER_SOCKET), TrialHandler) as server:
        harbor.WORKER_SOCKET.chmod(0o600)
        print(f"AgentSWE job worker listening on {harbor.WORKER_SOCKET}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
