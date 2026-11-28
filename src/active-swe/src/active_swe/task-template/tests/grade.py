"""Score one Active-SWE Recorded-track instance from the eval log.

Mirrors the scoring in upstream active_swe/run_recorded.py (process_one):
parse the after-patch test log with the instance's log parser, compare it with
the ground-truth FAIL_TO_PASS / PASS_TO_PASS lists, and mark the instance
resolved only if every GT F2P and every GT P2P test passed ("strict_resolved").
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from active_swe_grading.grading import (  # noqa: E402
    evaluate_patch_against_gt,
    evaluate_patch_against_gt_parsed,
    parse_and_prepare_after_only,
)


def main() -> int:
    config_path, log_path, out_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    cfg = json.loads(Path(config_path).read_text())
    log_file = Path(log_path)
    after_log = log_file.read_text(errors="replace") if log_file.exists() else ""

    gt_f2p = list(cfg["FAIL_TO_PASS"])
    gt_p2p = list(cfg["PASS_TO_PASS"])
    language = cfg.get("language", "")
    test_framework = cfg.get("test_framework", "")
    log_parser = cfg.get("log_parser", "")

    after_real = {}
    if after_log:
        try:
            after_real, _ = parse_and_prepare_after_only(
                after_log,
                language=language,
                test_framework=test_framework,
                log_parser=log_parser,
            )
        except Exception as exc:  # same fallback as upstream
            print(f"[PARSE_ERROR] {cfg['instance_id']}: {exc}")

    try:
        if after_real:
            result = evaluate_patch_against_gt_parsed(gt_f2p, gt_p2p, after_real)
        else:
            result = evaluate_patch_against_gt(
                gt_f2p,
                gt_p2p,
                after_log,
                language=language,
                test_framework=test_framework,
                log_parser=log_parser,
            )
    except Exception:
        result = {
            "f2p_passed": [],
            "f2p_failed": gt_f2p,
            "p2p_passed": [],
            "p2p_failed": gt_p2p,
            "p2p_flaky": [],
        }

    resolved = int(
        set(result["f2p_passed"]) >= set(gt_f2p)
        and set(result["p2p_passed"]) >= set(gt_p2p)
    )

    details = {
        "instance_id": cfg["instance_id"],
        "set": cfg.get("set", ""),
        "resolved": resolved,
        "f2p_passed": result["f2p_passed"],
        "f2p_failed": result["f2p_failed"],
        "p2p_passed_count": len(result["p2p_passed"]),
        "p2p_failed": result["p2p_failed"],
        "p2p_flaky": result.get("p2p_flaky", []),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(details, indent=2))
    (out_dir / "reward.txt").write_text(f"{resolved}\n")
    print(json.dumps({k: details[k] for k in ("instance_id", "resolved", "f2p_failed", "p2p_failed")}))
    return 0 if resolved else 1


if __name__ == "__main__":
    sys.exit(main())
