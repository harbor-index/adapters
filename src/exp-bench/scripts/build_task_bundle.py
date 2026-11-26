"""Build the EXP-Bench task bundle consumed by the Harbor adapter.

The public EXP-Bench CSV on Hugging Face is a flattened view that omits fields the
original judge needs (``method``, ``design_complexity``, the paper's code URL and the
masked-source lists). This script assembles the full per-task records from the
upstream construction outputs:

* ``<tasks-dir>/<conference>_<paper_id>.json``: the upstream
  ``outputs/logs/<conference>/<paper_id>/<paper_id>_complete_final.json`` files.
  ``task_index`` is the position of a question in that file, exactly as the upstream
  evaluation harness enumerates it.
* ``<details-dir>/<conference>.json``: the upstream paper-details JSONL files
  (``logs/<conference>/<...>_withcode_popularity_stars-100.json``) used to resolve
  each paper's GitHub URL (``code_url`` or ``reproduce_eval.code``).
* ``--pins``: JSON mapping ``<conference>/<paper_id>`` to the pinned repository
  commit (the last default-branch commit before the paper's tasks were constructed).

For every task it also downloads, at the pinned commit, the ground-truth source files
the upstream setup judge receives as ``setup_ground_truth_scripts`` and records their
sha256 (``None`` when the file does not exist in the repository). The adapter downloads
those files again at generation time and checks the hashes, so the bundle stays small.

Usage:
    uv run python scripts/build_task_bundle.py --tasks-dir ... --details-dir ... \
        --pins pins.json --out src/exp_bench/data/exp_bench_tasks.json.gz
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFERENCES = ("iclr2024", "neurips2024")


def strip_workspace_prefix(path: str) -> str:
    """Upstream ``strip_workspace_prefix`` from evaluation/eval.py."""
    if path.startswith("/workspace/"):
        return path[len("/workspace/") :]
    if path.startswith("/tmp/"):
        return path[len("/tmp/") :]
    if path.startswith("/"):
        return path[1:]
    return path


def github_raw_path(path: str) -> str:
    """Path handling of upstream ``get_file_from_github`` (helper/utils.py)."""
    if path.startswith("/workspace"):
        path = path[len("/workspace") :]
    return path.lstrip("/")


def fetch_raw(repo: str, commit: str, path: str, retries: int = 4) -> str | None:
    url = f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}")


def load_details(details_dir: Path) -> dict[tuple[str, str], dict]:
    details: dict[tuple[str, str], dict] = {}
    for conference in CONFERENCES:
        for line in (details_dir / f"{conference}.json").read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            # Same precedence as upstream evaluation/parallel_eval.py:get_paper_details.
            url = record["code_url"] if record.get("code_url") else record["reproduce_eval"]["code"]
            details[(conference, str(record["id"]))] = {
                "github_url": url,
                "paper_title": record.get("title"),
            }
    return details


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--details-dir", type=Path, required=True)
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--construction-commit",
        default=None,
        help="Commit of the upstream construction outputs the tasks were read from.",
    )
    args = parser.parse_args()

    details = load_details(args.details_dir)
    pins = json.loads(args.pins.read_text())
    records = []
    missing_sources = []

    for task_file in sorted(args.tasks_dir.glob("*.json")):
        conference, paper_id = task_file.stem.split("_", 1)
        pin = pins[f"{conference}/{paper_id}"]
        if not pin.get("commit"):
            raise SystemExit(f"No pinned commit for {conference}/{paper_id}: {pin}")
        detail = details[(conference, paper_id)]
        questions = json.loads(task_file.read_text())["questions"]
        for task_index, task_data in enumerate(questions):
            if "masked_source" in task_data:
                task_type, sources = 1, task_data["masked_source"]
            elif "source" in task_data:
                task_type, sources = 2, task_data["source"]
            else:
                task_type, sources = 3, []
            setup_sha256 = {}
            for source in sources:
                content = fetch_raw(pin["repo"], pin["commit"], github_raw_path(source))
                if content is None:
                    missing_sources.append(f"{conference}/{paper_id}/{task_index}: {source}")
                    setup_sha256[source] = None
                else:
                    setup_sha256[source] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            records.append(
                {
                    "task_id": f"{conference}-{paper_id}-{task_index}",
                    "conference": conference,
                    "paper_id": paper_id,
                    "paper_title": detail["paper_title"],
                    "task_index": task_index,
                    "task_type": task_type,
                    "github_url": detail["github_url"],
                    "repo": pin["repo"],
                    "commit": pin["commit"],
                    "masked_files": [strip_workspace_prefix(s) for s in sources],
                    "task_data": task_data,
                    "setup_script_paths": list(sources),
                    "setup_script_sha256": setup_sha256,
                }
            )
            print(f"{conference}-{paper_id}-{task_index} type={task_type} files={len(sources)}", flush=True)

    bundle = {
        "source": "EXP-Bench (arXiv:2505.24785) construction outputs",
        "construction_commit": args.construction_commit,
        "num_tasks": len(records),
        "missing_setup_scripts": missing_sources,
        "tasks": records,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(bundle, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    if args.out.suffix == ".gz":
        with gzip.GzipFile(args.out, "wb", compresslevel=9, mtime=0) as f:
            f.write(data)
    else:
        args.out.write_bytes(data)
    print(f"Wrote {len(records)} tasks to {args.out}; {len(missing_sources)} missing setup scripts")


if __name__ == "__main__":
    main()
