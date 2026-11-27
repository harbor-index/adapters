"""Download and unpack one pinned CaveBench test-frame archive (image build stage only).

Usage: fetch_frames.py <paper_scene> <sha256> <bytes>
Unpacks media/<paper_scene>/<sample>/F*.png under /opt/cavebench.
"""

import hashlib
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

HF_REPO = "linguanxin/CaveBench"
HF_REVISION = "df3c19c4cf2fe0ff08aef1d5e47f7ac7a8f22733"
DEST = Path("/opt/cavebench")


def download(url: str, target: Path, attempts: int = 4) -> None:
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=120) as src, target.open("wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            return
        except OSError as exc:
            if attempt == attempts:
                raise
            print(f"download failed ({exc}); retry {attempt}/{attempts - 1}", file=sys.stderr)
            time.sleep(5 * attempt)


def main() -> None:
    scene, expected_sha, expected_bytes = sys.argv[1], sys.argv[2], int(sys.argv[3])
    url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/{HF_REVISION}/test-frames/{scene}.zip"
    archive = Path(f"/tmp/{scene}.zip")
    download(url, archive)

    size = archive.stat().st_size
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if size != expected_bytes or digest != expected_sha:
        sys.exit(f"{scene}.zip mismatch: {size} bytes, sha256 {digest}")

    root = DEST.resolve()
    prefix = ("media", scene)
    with zipfile.ZipFile(archive) as zf:
        for entry in zf.infolist():
            rel = Path(entry.filename)
            target = (root / rel).resolve()
            if entry.is_dir():
                continue
            if rel.parts[:2] != prefix or rel.suffix != ".png" or not target.is_relative_to(root):
                sys.exit(f"unexpected archive entry: {entry.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            # ZipFile.read checks each member's CRC-32.
            target.write_bytes(zf.read(entry))
    archive.unlink()
    print(f"{scene}: unpacked {sum(1 for _ in root.rglob('*.png'))} frames")


if __name__ == "__main__":
    main()
