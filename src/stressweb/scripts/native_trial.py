#!/usr/bin/env python3
"""Run one generated trial with Node.js and the installed Playwright Chromium."""
import argparse
import ctypes
import filecmp
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

def unused_ports(n):
    sockets = [socket.socket() for _ in range(n)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--deps", type=Path, help="Reuse frontend/backend npm installs with matching lockfiles")
    parser.add_argument("--nop", action="store_true")
    args = parser.parse_args()
    if sys.platform == "linux":
        # Adopt orphaned Chromium/webpack descendants so an HPC pod without
        # a reaping PID 1 does not retain one zombie per helper on shutdown.
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot set child subreaper")
    task, out = args.task.resolve(), args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    browser = task / "environment/browser"
    site = out / "site"
    shutil.copytree(browser / "site", site)
    for part in ("backend", "frontend"):
        folder = site / part
        if args.deps:
            cached = args.deps.resolve() / part
            if not filecmp.cmp(cached / "package-lock.json", folder / "package-lock.json", shallow=False):
                raise ValueError(f"Mismatched lockfile for {part}")
            if not (cached / "node_modules").is_dir():
                raise FileNotFoundError(f"Missing npm install for {part}")
            (folder / "node_modules").symlink_to(cached / "node_modules", target_is_directory=True)
        else:
            subprocess.run(["npm","ci","--legacy-peer-deps","--no-audit","--no-fund"],cwd=folder,check=True)
    control, frontend, backend = unused_ports(3)
    base = f"http://127.0.0.1:{control}"
    env = dict(os.environ, STRESSWEB_BROWSER_URL=base)
    with (out / "browser.log").open("w") as log:
        proc = subprocess.Popen([sys.executable, "-u", str(browser/"service.py"), "--site", str(site),
            "--evidence", str(out/"evidence"), "--bind", "127.0.0.1","--control-port",str(control),
            "--frontend-port",str(frontend),"--backend-port",str(backend)], stdout=log, stderr=subprocess.STDOUT,
            env=env, start_new_session=True)
        try:
            for retry in range(240):
                if proc.poll() is not None:
                    raise RuntimeError(f"Browser service exited ({proc.returncode}); see {log.name}")
                try:
                    with urlopen(base+"/ready",timeout=1) as response:
                        if json.load(response)["ready"]:
                            break
                except OSError:
                    time.sleep(1)
            else:
                raise TimeoutError(f"Browser startup timed out; see {log.name}")
            if not args.nop:
                with (out/"oracle.log").open("w") as oracle_log:
                    subprocess.run(["bash", str(task/"solution/solve.sh")], env=env, stdout=oracle_log,
                                   stderr=subprocess.STDOUT, check=True, timeout=1800)
            subprocess.run([sys.executable, str(task/"tests/score.py"), str(out/"evidence"),
                            str(out/"verifier")],check=True)
            print((out/"verifier/checkpoints.json").read_text())
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=15)
            if sys.platform == "linux":
                while True:
                    try:
                        os.waitpid(-1, 0)
                    except ChildProcessError:
                        break

if __name__ == "__main__":
    main()
