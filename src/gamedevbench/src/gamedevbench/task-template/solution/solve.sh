#!/usr/bin/env bash
set -euo pipefail
# Harbor uploads this material only when the oracle agent is selected.
python3 - <<'PY'
import shutil
from pathlib import Path
workspace = Path('/workspace')
for entry in workspace.iterdir():
    if entry.is_dir() and not entry.is_symlink():
        shutil.rmtree(entry)
    else:
        entry.unlink()
shutil.copytree('/solution/project', workspace, dirs_exist_ok=True)
PY
