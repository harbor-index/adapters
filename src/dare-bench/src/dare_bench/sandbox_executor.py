"""In-container stand-in for the SandboxFusion /run_code call DARE-Bench's python_executor makes.

Usage: python3 sandbox_executor.py <request.json>
request: {"code_path", "files_to_load", "files_to_save", "work_dir", "timeout", "run_as"}

Like SandboxFusion, each call runs in a fresh, empty directory holding only the uploaded files,
so nothing persists between calls except files explicitly saved back. The code runs as the
unprivileged user named in request["run_as"], which cannot read work_dir (/workspace, mode 700),
so, as in SandboxFusion's separate container, it only sees the files it asked to load. Prints the run_result
JSON that upstream's tool returns to the model: SandboxFusion's run_result without
execution_time, plus a "File X saved successfully." line per retrieved file.

Saving mirrors upstream: the sandbox returns each requested file found under its basename, keyed
by the requested name, and upstream then raises on the first name that is not a direct child of
work_dir (files before it are already saved). That raise is reported as {"error": "..."}.
"""

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    request = json.loads(Path(sys.argv[1]).read_text())
    work_dir = Path(request["work_dir"])
    user = pwd.getpwnam(request["run_as"])
    run_dir = Path(tempfile.mkdtemp(prefix="dare_sandbox_"))
    for name in request["files_to_load"]:
        shutil.copy(work_dir / name, run_dir / name)
    shutil.copy(request["code_path"], run_dir / "main.py")
    for path in [run_dir, *run_dir.iterdir()]:
        os.chown(path, user.pw_uid, user.pw_gid)
    env = {**os.environ, "HOME": user.pw_dir, "USER": user.pw_name, "LOGNAME": user.pw_name}
    try:
        proc = subprocess.run(
            [sys.executable, "main.py"],
            cwd=run_dir,
            capture_output=True,
            text=True,
            timeout=request["timeout"],
            user=user.pw_uid,
            group=user.pw_gid,
            extra_groups=[],
            env=env,
        )
        run_result = {"status": "Finished", "return_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired as exc:
        decode = lambda b: b.decode(errors="replace") if isinstance(b, bytes) else (b or "")
        run_result = {"status": "TimeLimitExceeded", "return_code": None, "stdout": decode(exc.stdout), "stderr": decode(exc.stderr)}
    result = run_result
    for name in dict.fromkeys(request["files_to_save"]):
        produced = run_dir / os.path.basename(name)
        if not produced.is_file():
            continue
        if os.path.basename(name) != name:
            result = {"error": f"Invalid file path: {name!r}. Must be a direct child of work_dir."}
            break
        shutil.copy(produced, work_dir / name)
        run_result["stdout"] += f"\nFile {name} saved successfully.\n"
    shutil.rmtree(run_dir, ignore_errors=True)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
