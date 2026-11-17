"""Run the mock task on this machine, without Docker, a GPU or the simulator.

    python -m robodojo.mock.local                       # scripted solver, no API key
    python -m robodojo.mock.local --model provider/name # a real vision model

The mock service, the bridge client, the player and the verifier run as they
do in the generated task, but in one process. With ``--model`` the real
``DirectAgent`` calls the model, which costs money; the API key is read from
the environment exactly as in a Harbor run.

The result describes an invented task. It is not a RoboDojo score.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import shlex
import shutil
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any

from ..catalog import mock_task_spec
from ..direct import bridge_client
from ..runtime import EvaluationError, read_json
from ..verifier import verify
from . import service, solver


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LocalEnvironment:
    """Stand-in for the agent container: runs the bridge client in this process."""

    def __init__(self, url: str, out: Path, keep: Path | None = None) -> None:
        self.url, self.out, self.keep = url, out, keep

    def call(self, *command: str) -> dict[str, Any]:
        lines: list[str] = []
        bridge_client.main(["--url", self.url, "--out", str(self.out), *command], lines.append)
        result = json.loads(lines[-1])
        if self.keep is not None and result.get("status") == "observation":
            # The bridge client keeps only the open observation; save every one.
            source = Path(result["observation_dir"])
            shutil.copytree(source, self.keep / f"{len(list(self.keep.iterdir())):04d}_{source.name}")
        return result

    async def exec(self, command: str, timeout_sec: int | None = None, **_: Any) -> SimpleNamespace:
        arguments = shlex.split(command)[2:]  # Drop "python3 /opt/robodojo/bridge_client.py".
        result = await asyncio.to_thread(self.call, *arguments)
        return SimpleNamespace(return_code=0, stdout=json.dumps(result), stderr="")

    async def download_dir(self, source_dir: str, target_dir: Path) -> None:
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)


def _play_with_model(environment: LocalEnvironment, output: Path, args: argparse.Namespace) -> dict[str, Any]:
    try:
        from harbor.models.agent.context import AgentContext

        from ..direct_agent import DirectAgent
    except ImportError as error:
        raise SystemExit(f"--model needs Harbor: run with `uv run --extra harbor` ({error})") from error
    options = {name: getattr(args, name) for name in ("api_base", "max_cost_usd") if getattr(args, name) is not None}
    agent = DirectAgent(logs_dir=output / "agent", model_name=args.model, **options)
    context = AgentContext()

    async def play() -> None:
        await agent.setup(environment)
        await agent.run("unused", environment, context)

    asyncio.run(play())
    return context.metadata["robodojo_direct"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", type=Path, help="New or empty directory for all files of this run")
    parser.add_argument("--model", help="Vision model for DirectAgent; the scripted solver plays if omitted")
    parser.add_argument("--api-base", help="Model endpoint, recorded without credentials")
    parser.add_argument("--max-cost-usd", type=float, help="Abort once this much has been spent")
    args = parser.parse_args(argv)
    output = (args.output or Path(tempfile.mkdtemp(prefix="robodojo-mock-"))).resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"Output directory is not empty: {output}")
    pictures = output / "observations"
    pictures.mkdir(parents=True)
    spec_path = output / "task_spec.json"
    try:
        spec_path.write_text(json.dumps(mock_task_spec(args.seed, args.episodes), indent=2) + "\n")
    except ValueError as error:
        parser.error(str(error))

    port = _free_port()
    results = output / "results"
    threading.Thread(
        target=service.evaluate, args=(spec_path, results),
        kwargs={"host": "127.0.0.1", "port": port}, name="mock-simulator", daemon=True,
    ).start()
    environment = LocalEnvironment(f"http://127.0.0.1:{port}", output / "bridge", pictures)
    deadline = time.monotonic() + 30
    while environment.call("status").get("status") == "transport_error":
        if time.monotonic() > deadline:
            raise SystemExit("The mock service did not start")
        time.sleep(0.1)

    if args.model:
        summary = _play_with_model(environment, output, args)
        print(json.dumps({"model": summary["model"], "served": summary["served"], **summary["stats"]}, indent=2))
    else:
        solver.solve(environment.call)
    try:
        reward = verify(spec_path, results, output / "verifier")
    except EvaluationError as error:
        print(f"Mock run produced no reward: {error}", file=sys.stderr)
        return 1
    ledger = read_json(output / "verifier" / "evaluation.json")["variants"][0]
    print(f"Mock reward (not a RoboDojo score): {json.dumps(reward)}")
    print(f"Episodes: {ledger['episodes']}, decisions: {ledger['decisions']}, control steps: {ledger['control_steps']}")
    print(f"Pictures of every observation: {pictures}")
    print(f"All files of this run: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
