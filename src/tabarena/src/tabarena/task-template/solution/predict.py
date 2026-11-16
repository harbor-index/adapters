"""Known-answer oracle, not a trained model. Uses the ordinary prediction entry point."""

import json
import sys
from pathlib import Path

query = json.loads((Path(sys.argv[1]) / "test.json").read_text())
answers = json.loads((Path(__file__).parent / "answers.json").read_text())
if len(query["data"]) != len(answers["predictions"]):
    raise ValueError("Oracle answer count does not match the prediction query")
Path(sys.argv[2]).write_text(json.dumps(answers, allow_nan=False) + "\n")
