"""Run the unchanged evaluator in the isolated verifier environment."""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/tests")
from evaluation_pipeline import evaluate_single_case

logs = Path("/logs/verifier")
logs.mkdir(parents=True, exist_ok=True)
reward = logs / "reward.txt"
reward.unlink(missing_ok=True)
try:
    with tempfile.TemporaryDirectory(prefix="submission-") as temp:
        case = Path(temp) / "case"
        case.mkdir()
        shutil.copyfile("/tests/reference.json", case / "validation_cases.json")
        for name in ("cart.json", "messages.json"):
            source = Path("/workspace/state") / name
            if source.is_file():
                shutil.copyfile(source, case / name)
        result = evaluate_single_case(case)
    if not result.get("success"):
        raise RuntimeError(result.get("error", "Upstream evaluator failed"))
    (logs / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    reward.write_text(f"{float(result['case_score']):g}\n")
except Exception as error:
    (logs / "error.txt").write_text(f"{type(error).__name__}: {error}\n")
    raise
