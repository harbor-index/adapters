"""Play RoboDojo in Direct mode without Harbor: the original side of a parity run.

    python -m robodojo.original --model provider/name --output /new/directory \\
        --bridge-url http://127.0.0.1:19100 --spec task_spec.json --results-dir /results

The simulator half is the native evaluation with the ``HarborDirect`` policy,
started outside Harbor (see the README). This program is the model half. It
runs ``direct.player.DirectPlayer``, the same class that the Harbor agent runs,
so both sides share the prompt, the tool, the validation, the retries and the
limits. Only what Harbor adds differs: there is no container for the agent, no
trial record and no separate verifier container. The bridge client is the same
file, run in this process.

The model is called from this machine and costs money. The API key is read
from the environment, never from the command line.

With ``--spec`` and ``--results-dir`` the finished run is validated by the same
checks as the Harbor verifier, and the totals of every native ``_result.json``
are recorded as the native code wrote them. A run that fails or is incomplete
ends with a nonzero exit code and no result.
"""

from __future__ import annotations

import argparse
import asyncio
from itertools import zip_longest
import json
from pathlib import Path
import sys
from typing import Any

from .direct.player import DirectPlayer, DirectRunError, LocalBridge, SETUP_TIMEOUT_SEC
from .runtime import EvaluationError, load_spec, read_json, write_json
from .verifier import ledger_entries, verify

DEFAULT_BRIDGE_URL = "http://127.0.0.1:19100"
# Player options on the command line. Defaults are the player's own, so the
# two sides of a parity run start from the same values.
OPTIONS: dict[str, type] = {
    "max_invalid_responses": int, "max_decisions_per_episode": int, "max_cost_usd": float,
    "image_history": int, "history_window": int, "temperature": float, "reasoning_effort": str,
    "max_output_tokens": int, "model_seed": int, "tool_choice": str, "api_base": str,
    "api_key_env": str, "api_retries": int, "api_timeout_sec": float, "wait_sec": float,
    "max_idle_sec": float,
}


def play(
    model: str, bridge_url: str, output: Path, *, results_dir: Path | None = None,
    setup_timeout_sec: float = SETUP_TIMEOUT_SEC,
    **options: Any,
) -> dict[str, Any]:
    """Run the player against a bridge and return its provenance and counters."""
    player = DirectPlayer(model, output / "robodojo_direct", **options)
    player.work_dir.mkdir(parents=True)  # Exclusively reserve this invocation's player log.
    bridge = LocalBridge(bridge_url, output / "bridge")

    async def run() -> None:
        status = await player.setup(bridge, setup_timeout_sec)
        if status.get("run") != "running" or status.get("aborted"):
            raise DirectRunError("The bridge does not have a live attempt")
        attempt_id = status.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise DirectRunError("Missing bridge attempt ID. Regenerate the simulator task.")
        if results_dir is not None:
            check_attempt(results_dir, attempt_id)
        write_json(player.work_dir / "attempt.json", {"attempt_id": attempt_id})
        await player.run(bridge)

    asyncio.run(run())
    return player.summary()


def check_attempt(results_dir: Path, attempt_id: str) -> None:
    if read_json(results_dir / "broker" / "attempt.json").get("attempt_id") != attempt_id:
        raise EvaluationError("Bridge and results directory belong to different attempts")


def check_player_ledger(results_dir: Path, player_log: Path) -> None:
    """Match accepted decisions and episode ends in order, including scaffold holds."""
    def player_events():
        if player_log.exists():
            for row in ledger_entries(player_log):
                if "episode_end" in row:
                    end = row["episode_end"]
                    yield end["episode_index"], "end", end["episode_id"]
                elif row.get("error") is None:
                    yield row["episode_index"], "act", row["request_id"]

    def broker_events():
        directory = results_dir / "broker"
        for episode in ledger_entries(directory / "episodes.jsonl"):
            if episode["decisions"] == 0:
                continue  # No player action or act response for this native episode.
            index = episode["episode_index"]
            count = 0
            for row in ledger_entries(directory / "decisions" / f"{index:04d}.jsonl"):
                if row["event"] == "accepted":
                    count += 1
                    yield index, "act", row["request_id"]
            if count != episode["decisions"]:
                raise EvaluationError("Incomplete broker decision ledger")
            yield index, "end", episode["episode_id"]

    if any(player != broker for player, broker in zip_longest(player_events(), broker_events())):
        raise EvaluationError("Player actions or episode IDs do not match the broker ledger")


def grade(spec_path: Path, results_dir: Path, output: Path) -> dict[str, Any]:
    """Validate the native results and return them next to the comparable reward."""
    attempt_id = read_json(output / "robodojo_direct" / "attempt.json")["attempt_id"]
    check_attempt(results_dir, attempt_id)
    if read_json(results_dir / "supervisor.json").get("attempt_id") != attempt_id:
        raise EvaluationError("Supervisor verdict belongs to a different attempt")
    check_player_ledger(results_dir, output / "robodojo_direct" / "decisions.jsonl")
    verify(spec_path, results_dir, output / "verifier")
    evaluation = read_json(output / "verifier" / "evaluation.json")
    native = {}
    for variant in load_spec(spec_path).variants:
        result = read_json(results_dir / "native" / variant.name / "_result.json")
        # Verbatim native totals. The native score is on a 0-100 scale.
        native[variant.name] = {key: result[key] for key in ("eval_time", "success_rate", "score")}
    return {"native": native, "evaluation": evaluation}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="Vision model, for example provider/model-name")
    parser.add_argument("--output", type=Path, required=True, help="New or empty directory for this run")
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL, help="Address of the simulator's bridge")
    parser.add_argument("--spec", type=Path, help="Task specification of the native run, to validate it")
    parser.add_argument("--results-dir", type=Path, help="Results directory of the native run")
    parser.add_argument("--setup-timeout-sec", type=float, default=SETUP_TIMEOUT_SEC,
                        help="Seconds to wait for the bridge to answer")
    settings = parser.add_argument_group("player options", "Defaults are those of the Harbor agent.")
    for name, kind in OPTIONS.items():
        settings.add_argument(f"--{name.replace('_', '-')}", type=kind, default=None)
    settings.add_argument("--save-images", action="store_true", default=None,
                          help="Keep every downloaded observation")
    args = parser.parse_args(argv)
    if (args.spec is None) != (args.results_dir is None):
        parser.error("--spec and --results-dir belong together")
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"Output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    options = {
        name: getattr(args, name) for name in (*OPTIONS, "save_images") if getattr(args, name) is not None
    }
    try:
        if args.spec is not None:
            load_spec(args.spec)
        direct = play(
            args.model, args.bridge_url, output, results_dir=args.results_dir,
            setup_timeout_sec=args.setup_timeout_sec, **options,
        )
    except ValueError as error:
        parser.error(str(error))
    except (DirectRunError, EvaluationError, OSError) as error:
        print(f"Original-side run failed and has no result: {error}", file=sys.stderr)
        return 1
    attempt = read_json(output / "robodojo_direct" / "attempt.json")
    report: dict[str, Any] = {"runner": "original", "attempt_id": attempt["attempt_id"], "direct": direct}
    if args.spec is not None:
        try:
            report.update(grade(args.spec, args.results_dir, output))
        except (EvaluationError, OSError, KeyError) as error:
            print(f"Original-side run has no valid result: {error}", file=sys.stderr)
            return 1
    write_json(output / "original_side.json", report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
