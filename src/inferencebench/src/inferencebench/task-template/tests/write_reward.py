"""Write /logs/verifier/reward.json with the paper's failure-aware speedup."""

import json
import sys
from pathlib import Path

sys.path.insert(0, "/tests/ib")
from src.eval.inference.hpo_search_baselines import (  # noqa: E402
    FAILURE_SCORE,
    SCENARIO_TO_SHORT,
    primary_metric,
)

# PyTorch baseline in the units of primary_metric, from Table 12 of the paper: 840 ms TTFT,
# 41 ms TPOT, 0.132 req/s, and 1.68 for the scenario D score. These constants reproduce the
# paper's Table 2 from its Table 12.
PYTORCH_BASELINE = {"a": 1 / 0.840, "b": 1 / 0.041, "c": 0.132, "d": 1.68}
VERDICTS = {
    "contamination_detected": (
        "contamination_judgement.txt",
        "contamination detected",
        "no contamination detected",
    ),
    "disallowed_use_detected": (
        "disallowed_model_judgement.txt",
        "disallowed use detected",
        "only allowed use detected",
    ),
}


def read_verdict(path: Path, flagged: str, clean: str) -> bool | None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    return lines == [flagged] if lines in ([flagged], [clean]) else None


def rewards_for(logs: Path, scenario: str, eval_rc: str, judge_rc: str) -> dict[str, float]:
    try:
        metrics = json.loads((logs / "metrics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        metrics = {}
    metrics = metrics if isinstance(metrics, dict) else {}

    def check(name: str) -> bool:
        return isinstance(metrics.get(name), dict) and metrics[name].get("pass") is True

    gates = {
        "eval_valid": eval_rc == "0",
        "speed_pass": check("speed_check"),
        "quality_pass": check("quality_check"),
    }
    primary = 0.0
    if all(gates.values()) and metrics.get("score") != 0:
        value = primary_metric(metrics, scenario)[0]
        primary = value if value > FAILURE_SCORE else 0.0
    verdicts = {
        key: read_verdict(logs / name, flagged, clean)
        for key, (name, flagged, clean) in VERDICTS.items()
    }
    speedup = primary / PYTORCH_BASELINE[SCENARIO_TO_SHORT[scenario]]
    # A run that fails a gate, ships no working server, or is flagged by the judge is worth
    # the PyTorch baseline (1x), as in the paper's Table 2.
    valid = primary > 0 and not any(verdicts.values())
    return {
        "reward": speedup if valid else 1.0,
        "valid": int(valid),
        "speedup": speedup,
        "primary_metric": primary,
        **{key: int(value) for key, value in gates.items()},
        "judge_valid": int(judge_rc == "0" and None not in verdicts.values()),
        **{key: int(value is True) for key, value in verdicts.items()},
    }


def main() -> None:
    logs, scenario, eval_rc, judge_rc = Path(sys.argv[1]), *sys.argv[2:5]
    rewards = rewards_for(logs, scenario, eval_rc, judge_rc)
    (logs / "reward.json").write_text(json.dumps(rewards, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(rewards))


if __name__ == "__main__":
    main()
