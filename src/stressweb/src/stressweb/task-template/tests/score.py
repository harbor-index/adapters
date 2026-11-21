import hashlib
import json
from pathlib import Path
import sys

from evaluator import Evaluator


def verified_capture(evidence):
    status = json.loads((evidence / "capture.json").read_text())
    if status.get("phase") != "complete" or (evidence / "capture.tmp").exists():
        raise ValueError("Browser capture did not complete")
    files = status["files"]
    if not {"result.json", "traj.jsonl"} <= files.keys():
        raise ValueError("Missing browser summary or trajectory")
    for name, digest in files.items():
        path = evidence / name
        if Path(name).name != name or path.is_symlink() or not path.is_file():
            raise ValueError("Invalid browser artifact")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"Incomplete browser artifact: {name}")
        if name.endswith("_raw.html") and not data.rstrip().lower().endswith(b"</html>"):
            raise ValueError(f"Incomplete browser DOM: {name}")
        if name.endswith(".png") and not (data.startswith(b"\x89PNG\r\n\x1a\n") and
                                          data.endswith(b"IEND\xaeB\x60\x82")):
            raise ValueError(f"Incomplete screenshot: {name}")
    actual = {p.name for p in evidence.iterdir()}
    if actual != set(files) | {"capture.json"}:
        raise ValueError("Browser file inventory differs from its completion record")
    summary = json.loads((evidence / "result.json").read_text())
    steps, reason = summary["steps"], summary["reason"]
    if type(steps) is not int or not 0 <= steps <= 100 or steps != status["steps"]:
        raise ValueError("Invalid browser step count")
    if reason not in {"active","done","fail"} or (steps == 0 and reason != "active"):
        raise ValueError("Invalid browser termination state")
    if summary.get("success") is not (reason == "done"):
        raise ValueError("Browser success and termination state differ")
    rows = [json.loads(line) for line in (evidence / "traj.jsonl").read_text().splitlines()]
    # The native FAIL branch returns before writing a terminal row or snapshot.
    if len(rows) != 1 + steps - int(reason == "fail"):
        raise ValueError("Incomplete browser trajectory")
    expected = {"traj.jsonl", "result.json"}
    if steps == 0:
        expected.add("final_0_nop_raw.html")
    for n, row in enumerate(rows):
        if row["step_num"] != n:
            raise ValueError("Nonconsecutive browser trajectory")
        final = reason == "done" and n == steps
        if n == 0:
            prefix = "initial"
            if row.get("state") != "initial" or not row.get("page_info", {}).get("url"):
                raise ValueError("Incomplete initial observation")
        elif final:
            prefix = "final"
            if row.get("state") != "done" or row.get("action", {}).get("action_type") != "DONE":
                raise ValueError("Incomplete DONE observation")
        else:
            prefix = "step"
            if (not row.get("action", {}).get("action_type") or not row.get("page_info", {}).get("url")
                    or "success" not in row.get("result", {})):
                raise ValueError("Incomplete browser action record")
        matches = list(evidence.glob(f"{prefix}_{n}_*_raw.html"))
        if len(matches) != 1:
            raise ValueError(f"Expected one {prefix} snapshot at step {n}")
        raw = matches[0].name
        expected.update((raw, raw.removesuffix("_raw.html") + ".png"))
    if set(files) != expected:
        raise ValueError("Snapshot inventory does not match the browser trajectory")
    return summary


def main():
    evidence, output = map(Path, sys.argv[1:])
    summary = verified_capture(evidence)
    measured = Evaluator(str(evidence)).evaluate()
    # A native immediate FAIL has an initial page only. Q1 reports the lack
    # of any post-action HTML as its ordinary unsuccessful result in that case.
    if measured.get("error") and not (summary["reason"] == "fail" and summary["steps"] == 1
                                      and measured["error"] == "No HTML file found"):
        raise RuntimeError(measured["error"])
    output.mkdir(parents=True, exist_ok=True)
    (output / "checkpoints.json").write_text(json.dumps(measured, indent=2) + "\n")
    (output / "reward.txt").write_text(f"{int(measured['overall_success'])}\n")


if __name__ == "__main__":
    main()
