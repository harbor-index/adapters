"""Aggregate metrics.json files; incomplete/full task coverage is always explicit."""

import argparse
import json
from pathlib import Path

from vision2web.metrics import aggregate

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("files", type=Path, nargs="+")
args = parser.parse_args()
records = [json.loads(p.read_text()) for p in args.files]
print(json.dumps(aggregate(records), indent=2))
