"""Download the public HuggingFace dataset of this task into <out>/<dataset>/<config>.

Mirrors AIRS-Bench's datasets/prepare_hf_datasets_text.py (datasets==3.6.0).
"""
import argparse
import json
import os
import time
from pathlib import Path

from datasets import load_dataset


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    cfg = json.loads((Path(__file__).parent / "dataset.json").read_text())
    name, config = cfg["dataset"], cfg["config"]
    for attempt in range(3):
        try:
            ds = load_dataset(name, name=config, trust_remote_code=True)
            ds.save_to_disk(os.path.join(args.out, name, config))
            print(f"saved {name}/{config} -> {args.out}")
            return
        except Exception as e:  # noqa: BLE001
            print(f"attempt {attempt + 1} failed: {e}")
            time.sleep(10)
    raise SystemExit(f"could not download {name}/{config}")


if __name__ == "__main__":
    main()
