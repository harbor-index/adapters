"""Grade one AgenticDataBench task the way upstream does.

Mirrors testbed/evaluate.py and testbed/da_agent/evaluators/evaluation.py
(AgenticDataBench @ e81dda8): every eval_func string from the task config is
run with its gold and output file names replaced by full paths, each check
scores 0.0-1.0, and the task score is their mean. The whole task has upstream's
default 120 s evaluation timeout. Upstream's trajectory bookkeeping is left out:
it does not affect the score.

Runs in its own container (task.toml: environment_mode = "separate"). Reads the
agent's files from /workspace, where Harbor puts the artifacts it copied out of
the agent's container (upstream reads them from the mounted work dir by base
name), and the gold files from /tests/gold. Writes the score to
/logs/verifier/reward.txt and the per-check details to /logs/verifier/details.json.
"""

import json
import logging
import re
import sys
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from da_utils import timeout  # noqa: E402
from metrics import *  # noqa: E402,F401,F403  (eval_func strings call these by name)

WORK_DIR = Path("/workspace")
GOLD_DIR = TESTS_DIR / "gold"
LOG_DIR = Path("/logs/verifier")
EVAL_TIMEOUT_SEC = 120  # testbed/evaluate.py --timeout_seconds default


def as_list(value):
    return value if isinstance(value, list) else [value]


def bind_paths(func: str, names: dict[str, str]) -> str:
    """Replace each quoted file name in an eval_func string with its full path."""
    for name, path in names.items():
        pattern = rf"(['\"]){re.escape(name)}\1"
        if len(re.findall(pattern, func)) > 1:
            raise ValueError(f"Found multiple matches for {name}")
        func = re.sub(pattern, lambda m, p=path: m.group(1) + p + m.group(1), func)
    return func


def main() -> None:
    config = json.loads((TESTS_DIR / "task_config.json").read_text())
    gold = {n: (GOLD_DIR / Path(n).name).as_posix() for n in as_list(config["gold_file_name"])}
    output = {n: (WORK_DIR / Path(n).name).as_posix() for n in as_list(config["output_file_name"])}
    funcs = []
    for func in as_list(config["eval_func"]):
        funcs.append(bind_paths(bind_paths(func, gold), output))

    scores, info = [], []
    try:
        with timeout(EVAL_TIMEOUT_SEC, "Action execution time exceeded!"):
            for func in funcs:
                try:
                    result = eval(func)
                except FileNotFoundError as e:
                    logging.error(f"File not found! Error: {e}")
                    scores.append(0.0)
                    info.append({"score": 0.0, "errors": [f"File not found: {e}"]})
                    continue
                if isinstance(result, dict):
                    scores.append(result.get("score", 0.0))
                    info.append(result)
                else:
                    scores.append(result)
                    info.append({"score": result})
    except Exception as e:
        logging.error(f"Error in task {config['id']}: {e}")
        traceback.print_exc()
        scores.append(0.0)
        info.append({"score": 0.0, "errors": [str(e)]})

    scores = [s if isinstance(s, (int, float)) else 0.0 for s in scores]
    total = sum(scores) / len(scores)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    details = {"id": config["id"], "total_score": total, "scores": scores, "info": info}
    (LOG_DIR / "details.json").write_text(json.dumps(details, indent=2, default=str))
    (LOG_DIR / "reward.txt").write_text(f"{total}\n")
    print(f"{config['id']}: score {total:.4f} from checks {[round(float(s), 4) for s in scores]}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
