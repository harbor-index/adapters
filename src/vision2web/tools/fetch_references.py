"""Fetch three pinned public candidates, not certified oracle solutions."""

import argparse
import concurrent.futures
import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

REVISION = "dd464e60b02f836e768fd9d21baa49c97a53edf6"
CHOICES = [
    ("webpage", "recipecloud", "ClaudeCode+Claude-Opus-5"),
    ("frontend", "anomali", "ClaudeCode+Claude-Opus-5"),
    ("website", "filmmovement", "Codex+GPT-5.6-Sol"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    receipts = []
    for level, name, submission in CHOICES:
        prefix = f"{submission}/{level}/{name}"
        url = (
            "https://huggingface.co/api/datasets/zai-org/Vision2Web-Leaderboard/tree/"
            + REVISION
            + "/"
            + urllib.parse.quote(prefix, safe="/")
            + "?recursive=true&limit=1000"
        )
        with urllib.request.urlopen(url, timeout=60) as response:
            entries = json.load(response)
            if response.headers.get("Link"):
                raise RuntimeError("Unexpected pagination; do not truncate a candidate")
        files = []
        for item in entries:
            if item["type"] != "file":
                continue
            relative = PurePosixPath(item["path"][len(prefix) + 1 :])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(relative)
            if any(
                x in {"results", "logs", ".playwright-cli", "node_modules", ".git"}
                for x in relative.parts
            ):
                continue
            if relative.name.startswith(".env"):
                continue
            files.append((item, relative))

        def save(pair, level=level, name=name):
            item, relative = pair
            target = args.output_dir / level / name / str(relative)
            raw = (
                "https://huggingface.co/datasets/zai-org/Vision2Web-Leaderboard/resolve/"
                + REVISION
                + "/"
                + urllib.parse.quote(item["path"], safe="/")
            )
            with urllib.request.urlopen(raw, timeout=60) as response:
                data = response.read()
            if len(data) != item["size"]:
                raise ValueError(f"Size mismatch: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return {"path": str(relative), "sha256": hashlib.sha256(data).hexdigest()}

        receipt = {
            "task": f"{level}/{name}",
            "submission": submission,
            "revision": REVISION,
            "status": "Unverified candidate; no score promised.",
            "files": list(concurrent.futures.ThreadPoolExecutor(max_workers=4).map(save, files)),
        }
        receipts.append(receipt)
    (args.output_dir / "candidate-sources.json").write_text(json.dumps(receipts, indent=2) + "\n")


if __name__ == "__main__":
    main()
