#!/usr/bin/env python3
"""Assemble the parity/oracle upload for harborframework/parity-experiments.

Layout (docs/adapters.mdx, Step 7):
  adapters/sweeper-bench/
    README.md  config.yaml
    oracle/<trial>/...            final oracle run (result, verdict, reward, verifier log)
    original_parity/r<k>/<case>/  upstream status, prediction patch, evaluation cells
    harbor_parity/r<k>/<trial>/   Harbor result, verdict, collected patch, agent trajectory
    results_collection/result_{original,harbor}_trial<k>.json

Only small text artifacts are copied. Every copied file is scanned for API-key,
GitHub-token and JWT patterns, which are replaced with ***REDACTED***.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import shutil
from pathlib import Path

SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|AQ\.[A-Za-z0-9_.-]{20,}|AIza[0-9A-Za-z_-]{30,}|ya29\.[A-Za-z0-9_.-]{20,}|1//[0-9A-Za-z_-]{30,})"
)
MAX_BYTES = 8 * 1024 * 1024


def copy(src: Path, dst: Path) -> None:
    if not src.is_file() or src.stat().st_size > MAX_BYTES:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = src.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        shutil.copy2(src, dst)
        return
    dst.write_text(SECRET.sub("***REDACTED***", text), encoding="utf-8")


def harbor_trial(src: Path, dst: Path) -> None:
    for rel in ("result.json", "config.json", "trial.log", "verifier/reward.txt", "verifier/sweeper-verdict.json",
                "verifier/test.log", "verifier/prediction.patch", "agent/trajectory.json", "agent/oracle.txt"):
        copy(src / rel, dst / rel)
    for f in (src / "verifier" / "sweeper" / "logs").glob("*-result.json"):
        copy(f, dst / "verifier" / "sweeper" / "logs" / f.name)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--summary", required=True, help="output of summarize_parity.py")
    p.add_argument("--original", nargs="+", required=True)
    p.add_argument("--harbor", nargs="+", required=True, help="comma-separated job dirs per run")
    p.add_argument("--oracle-jobs", required=True, help="glob of oracle job dirs")
    p.add_argument("--readme", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args()

    root = Path(a.out) / "adapters" / "sweeper-bench"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    summary = json.loads(Path(a.summary).read_text())
    copy(Path(a.readme), root / "README.md")
    copy(Path(a.config), root / "config.yaml")

    for job in sorted(glob.glob(a.oracle_jobs)):
        for t in Path(job).glob("sweeper-*__*/"):
            harbor_trial(t, root / "oracle" / t.name)
    for k, run in enumerate(a.original, 1):
        for case_dir in Path(run).glob("sweeper-*/"):
            out = root / "original_parity" / f"r{k}" / case_dir.name
            copy(case_dir / "status.json", out / "status.json")
            copy(case_dir / "prediction" / "artifacts" / "prediction-scoped.patch", out / "prediction-scoped.patch")
            copy(case_dir / "prediction" / "artifacts" / "logs-scoped" / "harness.stdout.txt", out / "harness.stdout.txt")
            for f in (case_dir / "evaluation" / "artifacts" / "logs").glob("*-result.json"):
                copy(f, out / f.name)
    for k, spec in enumerate(a.harbor, 1):
        for job in spec.split(","):
            for t in Path(job).glob("sweeper-*__*/"):
                harbor_trial(t, root / "harbor_parity" / f"r{k}" / Path(job).name / t.name)

    rc = root / "results_collection"
    rc.mkdir()
    for side in ("original", "harbor"):
        for k, score in enumerate(summary[f"{side}_runs"], 1):
            per_task = {t: v[side][k - 1] for t, v in summary["per_task"].items()}
            (rc / f"result_{side}_trial{k}.json").write_text(
                json.dumps({"run": k, "side": side, "solved_rate_percent": score, "per_task": per_task}, indent=2) + "\n"
            )
    (rc / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    n = sum(1 for _ in root.rglob("*") if _.is_file())
    print(f"bundle: {root} ({n} files)")


if __name__ == "__main__":
    main()
