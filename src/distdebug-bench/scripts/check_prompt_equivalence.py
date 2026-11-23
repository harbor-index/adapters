"""Check that every generated instruction.md equals the upstream CLI-harness prompt.

Compares instruction.md with distdebug_bench.harnesses.cli.cli_prompt(build_task(bug, stage_workspace(bug, ws)))
and the staged attachments with environment/artifacts/.

Usage: python scripts/check_prompt_equivalence.py <upstream checkout> <generated tasks dir>
"""
import sys, tempfile
from pathlib import Path
up = Path(sys.argv[1]).resolve(); ds = Path(sys.argv[2])
sys.path.insert(0, str(up))
from distdebug_bench import load_benchmark
from distdebug_bench.agent import build_task, stage_workspace
from distdebug_bench.harnesses.cli import cli_prompt
bench = load_benchmark(up); bad = 0
for bug in bench.iter_bugs("lite"):
    with tempfile.TemporaryDirectory() as ws:
        expected = cli_prompt(build_task(bug, stage_workspace(bug, Path(ws))))
        staged = sorted(p.name for p in (Path(ws) / ".artifacts").iterdir()) if (Path(ws) / ".artifacts").exists() else []
    got = (ds / bug.name / "instruction.md").read_text()
    arts = sorted(p.name for p in (ds / bug.name / "environment" / "artifacts").iterdir()) if (ds / bug.name / "environment" / "artifacts").exists() else []
    ok = got == expected and arts == staged
    bad += not ok
    print(bug.name, "OK" if ok else f"MISMATCH (prompt {got == expected}, artifacts {arts} vs {staged})")
print("mismatches:", bad); sys.exit(bad > 0)
