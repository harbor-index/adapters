"""Turn evaluate.py output into a Harbor reward using AIRS-Bench's normalized score.

phi(s) = -log10(|s - s_opt|)
NS(s)  = (phi(s) - phi(worst)) / (phi(sota) - phi(worst))     (0 = worst, 1 = SOTA)
reward = clip(NS, 0, 1);  a perfect score (s == s_opt) gives reward 1.0.
"""
import json
import math
import re
import sys
from pathlib import Path


def phi(s: float, opt: float) -> float:
    d = abs(s - opt)
    return math.inf if d == 0 else -math.log10(d)


def normalized_score(s: float, worst: float, sota: float, opt: float) -> float:
    ps, pw, pt = phi(s, opt), phi(worst, opt), phi(sota, opt)
    if math.isinf(ps):
        return 1.0
    return (ps - pw) / (pt - pw)


def parse_metric(stdout: str, metric: str) -> float:
    block = stdout.split("--- EVALUATION RESULT ---", 1)[-1]
    m = re.search(r"\{.*\}", block, re.S)
    result = json.loads(m.group(0))
    for k, v in result.items():
        if k.lower().replace("_", "") == metric.lower().replace("_", ""):
            return float(v)
    return float(next(iter(result.values())))


def main() -> None:
    stdout_path, cfg_path, reward_path = map(Path, sys.argv[1:4])
    cfg = json.loads(cfg_path.read_text())
    try:
        s = parse_metric(stdout_path.read_text(), cfg["metric"])
        ns = normalized_score(s, cfg["worst"], cfg["sota"], cfg["optimal"])
        reward = min(1.0, max(0.0, ns))
        detail = {"metric": cfg["metric"], "score": s, "normalized_score": ns, "reward": reward}
    except Exception as e:  # noqa: BLE001
        reward, detail = 0.0, {"error": repr(e)}
    reward_path.write_text(f"{reward}\n")
    (reward_path.parent / "metrics.json").write_text(json.dumps(detail, indent=2))
    print(json.dumps(detail, indent=2))


if __name__ == "__main__":
    main()
