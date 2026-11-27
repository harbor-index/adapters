"""Download one task's input data from the AgenticDataBench Hugging Face dataset.

Upstream copies the contents of datasets/<domain>/ into the agent's work dir
(testbed/da_agent/envs/da_agent.py, setup_cp_dir). This script does the same
from the pinned dataset revision, checking each file's size and hash (sha256 for
LFS files, the git blob sha1 otherwise) against the manifest the adapter wrote.

Usage: fetch_data.py <manifest.tsv> <dest_dir>
Manifest columns: revision, repo path, path under dest, size, hash kind, hash.
"""

import hashlib
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "shawnzzzh/AgenticDataBench"
CHUNK = 1 << 20


def download(url: str, dest: Path, size: int, kind: str, expected: str) -> None:
    for attempt in range(1, 6):
        try:
            digest = hashlib.sha256() if kind == "sha256" else hashlib.sha1(f"blob {size}\0".encode())
            got = 0
            tmp = dest.with_name(dest.name + ".part")
            with urllib.request.urlopen(url, timeout=120) as r, tmp.open("wb") as f:
                while chunk := r.read(CHUNK):
                    f.write(chunk)
                    digest.update(chunk)
                    got += len(chunk)
            if got != size or digest.hexdigest() != expected:
                raise ValueError(f"{dest}: got {got} bytes / {digest.hexdigest()}, expected {size} / {expected}")
            tmp.rename(dest)
            return
        except Exception as e:  # noqa: BLE001 - retry any network or integrity error
            print(f"attempt {attempt} failed for {dest.name}: {e}", file=sys.stderr)
            time.sleep(min(60, 5 * attempt))
    raise SystemExit(f"could not download {url}")


def main() -> None:
    manifest, dest_root = Path(sys.argv[1]), Path(sys.argv[2])
    total = 0
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        revision, repo_path, rel, size, kind, expected = line.split("\t")
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://huggingface.co/datasets/{REPO}/resolve/{revision}/{urllib.parse.quote(repo_path)}"
        download(url, dest, int(size), kind, expected)
        total += int(size)
        print(f"{rel} ({int(size) / 1e6:.1f} MB)")
    print(f"fetched {total / 1e9:.2f} GB into {dest_root}")


if __name__ == "__main__":
    main()
