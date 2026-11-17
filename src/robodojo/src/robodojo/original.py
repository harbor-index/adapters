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
import json
from pathlib import Path
import sys
from typing import Any

from .direct.player import DirectPlayer, DirectRunError, LocalBridge, SETUP_TIMEOUT_SEC
from .runtime import EvaluationError, load_spec, read_json, write_json
from .verifier import verify

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
    model: str, bridge_url: str, output: Path, *, setup_timeout_sec: float = SETUP_TIMEOUT_SEC,
    **options: Any,
) -> dict[str, Any]:
    """Run the player against a bridge and return its provenance and counters."""
    player = DirectPlayer(model, output / "robodojo_direct", **options)
    bridge = LocalBridge(bridge_url, output / "bridge")

    async def run() -> None:
        await player.setup(bridge, setup_timeout_sec)
        await player.run(bridge)

    asyncio.run(run())
    return player.summary()


def grade(spec_path: Path, results_dir: Path, output: Path) -> dict[str, Any]:
    """Validate the native results and return them next to the comparable reward."""
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
        direct = play(
            args.model, args.bridge_url, output, setup_timeout_sec=args.setup_timeout_sec, **options,
        )
    except ValueError as error:
        parser.error(str(error))
    except DirectRunError as error:
        print(f"Original-side run failed and has no result: {error}", file=sys.stderr)
        return 1
    report: dict[str, Any] = {"runner": "original", "direct": direct}
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
