"""In-container runner for the cavebench-llm parity agent.

Answers one CaveBench question with the authors' own published protocol:
upstream `run.payload` builds the request and
`inference.protocol.request_published` runs the primary request and, when
needed, the single answer-completion request (`run.py --paper-settings`).
Nothing in the request or protocol is changed. The only addition is a
transport wrapper that records per-stage metadata for the trace.

If the protocol yields no legal answer (or raises on an HTTP error or an
identity hold), no answer file is written, so the question scores 0. This is
how the paper treats unanswered questions and how the parity fork of
`run.py` records them.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path


def stage_summary(out: dict) -> dict:
    transport = out.get("transport") or {}
    response = out.get("response") or {}
    usage = response.get("usage") if isinstance(response, dict) else None
    keep = (
        "stage", "elapsed_seconds", "close_reason", "error_type", "reported_cost_usd",
        "actual_model_id", "actual_provider", "model_identity_verified",
        "provider_identity_verified", "selected_content", "raw_accumulated_content",
        "effective_wall_seconds",
    )
    return {
        "http_status": out.get("http_status"),
        "decision": out.get("decision"),
        "usage": usage,
        **{key: transport.get(key) for key in keep},
    }


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--upstream", type=Path, required=True)
    cli.add_argument("--question", type=Path, required=True)
    cli.add_argument("--frames", type=Path, required=True)
    cli.add_argument("--model", required=True)
    cli.add_argument("--endpoint", required=True)
    cli.add_argument("--expect-prompt-sha256", default=None,
                     help="SHA-256 of the instruction's archived prompt; must match the question")
    cli.add_argument("--answer", type=Path, required=True)
    cli.add_argument("--trace", type=Path, required=True)
    cli.add_argument("--workdir", type=Path, default=Path("/tmp/cavebench-data"))
    args = cli.parse_args()

    sys.path.insert(0, str(args.upstream))
    from inference.protocol import PaperSettings, request_published
    from inference.transport import run_stream_stage
    from prompt_templates import evaluation_question
    from run import payload

    row = json.loads(args.question.read_text(encoding="utf-8"))
    row.pop("frames", None)
    question = evaluation_question(row, "original")
    if args.expect_prompt_sha256 is not None:
        actual = hashlib.sha256(question["prompt"].encode("utf-8")).hexdigest()
        if actual != args.expect_prompt_sha256:
            print("instruction prompt differs from the upstream question prompt", file=sys.stderr)
            return 3

    # Recreate the upstream dataset layout (media/<scene>/<sample>/F*.png) so
    # run.payload reads the frames exactly as it does upstream.
    data = args.workdir
    for i, relative in enumerate(question["images"]):
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.frames / f"F{i}.png", target)

    key = os.environ.get("CAVEBENCH_API_KEY")
    if not key:
        print("CAVEBENCH_API_KEY is not set", file=sys.stderr)
        return 2

    stages: list[dict] = []

    def traced_transport(*a, **kw):
        out = run_stream_stage(*a, **kw)
        stages.append(stage_summary(out))
        return out

    trace = {"id": question["id"], "model": args.model, "endpoint": args.endpoint, "stages": stages}
    started = time.monotonic()
    try:
        protocol = PaperSettings(args.model)
        request = payload(question, data, args.model, 8192)
        trace["settings"] = protocol.for_question(question)
        answer = request_published(question, request, protocol, args.endpoint, key,
                                   transport=traced_transport)
        args.answer.write_text(answer, encoding="utf-8")
        trace.update(status="answered", response=answer)
    except Exception as error:  # recorded as unanswered (scores 0), like the parity fork
        trace.update(status="unanswered", error_type=type(error).__name__,
                     error=str(error), traceback=traceback.format_exc())
    trace["elapsed_seconds"] = time.monotonic() - started
    costs = [s["reported_cost_usd"] for s in stages if s.get("reported_cost_usd") is not None]
    trace["cost_usd"] = sum(costs) if costs else None
    args.trace.parent.mkdir(parents=True, exist_ok=True)
    args.trace.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: trace.get(k) for k in ("id", "status", "error_type", "response", "cost_usd",
                                                "elapsed_seconds")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
