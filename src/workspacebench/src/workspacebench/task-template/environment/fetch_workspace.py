"""Unpack one persona workspace from the pinned Workspace-Bench zip into --dest.

The zips are about 18.7 GB each, so only the persona's part is read. Each
persona's entries sit next to each other in the zip, so they are fetched as a
few large HTTP range requests (at most CHUNK_BYTES each) and unpacked while
streaming. One request per file would be about 14,500 requests for the
Researcher, over Hugging Face's limit of 3,000 per 5 minutes for anonymous
downloads. Every file is checked against the CRC-32 and size in the zip's
central directory. Afterwards a snapshot of every file's size and mtime is
written to /opt/wsb/before.json; the verifier uses it, like upstream's runner,
to find outputs written outside model_output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

REPO = "Workspace-Bench/Workspace-Bench-Workspaces"
REVISION = "e245d63bfa20cfdb708cd8e78145ffb087155857"
SNAPSHOT = "/opt/wsb/before.json"
SKIPPED_DIRS = {"model_output", ".workspace_data", ".git", ".cache", "__pycache__", "node_modules"}
CHUNK_BYTES = 512 << 20
ATTEMPTS = 8
LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")


class RateLimited(Exception):
    def __init__(self, wait: float):
        super().__init__(f"rate limited, retry in {wait:.0f}s")
        self.wait = wait


class Entry:
    """One zip member: where its local record starts and ends, and what the central directory says about it."""

    def __init__(self, info, end: int, out: str | None):
        self.name = info.filename
        self.start = info.header_offset
        self.end = end
        self.csize = info.compress_size
        self.size = info.file_size
        self.crc = info.CRC
        self.method = info.compress_type
        self.out = out  # None: directory or the persona folder itself, nothing to write


class Reader:
    """Exact-size reads from an iterator of byte blocks."""

    def __init__(self, blocks):
        self.blocks = iter(blocks)
        self.buf = bytearray()

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            b = next(self.blocks, b"")
            if not b:
                raise EOFError("stream ended early")
            self.buf += b
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def skip(self, n: int) -> None:
        while n:
            n -= len(self.read(min(n, 1 << 20)))


def unpack_entry(r: Reader, e: Entry) -> int:
    """Read one local record from r (positioned at its start), write the file, return bytes written."""
    sig, _, flags, method, *_, name_len, extra_len = LOCAL_HEADER.unpack(r.read(LOCAL_HEADER.size))
    if sig != 0x04034B50:
        raise ValueError(f"no local header at {e.start} for {e.name}")
    if flags & 1:
        raise ValueError(f"encrypted entry: {e.name}")
    if method != e.method or method not in (0, 8):
        raise ValueError(f"unsupported compression {method} for {e.name}")
    r.skip(name_len + extra_len)
    tail = e.end - e.start - LOCAL_HEADER.size - name_len - extra_len - e.csize  # data descriptor, if any
    if tail < 0:
        raise ValueError(f"entry overruns the next one: {e.name}")
    if e.out is None:
        r.skip(e.csize + tail)
        return 0
    inflate = zlib.decompressobj(-15) if method == 8 else None
    crc = wrote = 0
    left = e.csize
    with open(e.out, "wb") as f:
        while left:
            b = r.read(min(left, 1 << 20))
            left -= len(b)
            if inflate:
                b = inflate.decompress(b)
            crc = zlib.crc32(b, crc)
            wrote += len(b)
            f.write(b)
        if inflate:
            b = inflate.flush()
            crc = zlib.crc32(b, crc)
            wrote += len(b)
            f.write(b)
    if wrote != e.size or crc != e.crc:
        raise ValueError(f"corrupt data for {e.name}: {wrote} bytes, crc {crc:08x}, zip lists {e.size} / {e.crc:08x}")
    r.skip(tail)
    return wrote


def http_range(url: str, start: int, end: int):
    """Yield bytes [start, end) of url. A 429 raises RateLimited with Hugging Face's reset time."""
    import requests

    resp = requests.get(url, headers={"Range": f"bytes={start}-{end - 1}"}, stream=True, timeout=(30, 120))
    with resp:
        if resp.status_code == 429:
            # Hugging Face sends `RateLimit: "resolvers";r=0;t=<seconds until reset>`.
            m = re.search(r"\bt=(\d+)", resp.headers.get("RateLimit", ""))
            raise RateLimited(min(int(m.group(1)) + 1, 330) if m else 60)
        resp.raise_for_status()
        if resp.status_code != 206:
            raise ValueError(f"server ignored the range request (HTTP {resp.status_code})")
        yield from resp.iter_content(1 << 20)


def unpack_chunk(fetch, chunk: list[Entry], log=print) -> int:
    """Stream one run of adjacent entries; after a failure, resume at the entry that was in progress."""
    done = total = 0
    for attempt in range(ATTEMPTS):
        try:
            r = Reader(fetch(chunk[done].start, chunk[-1].end))
            while done < len(chunk):
                total += unpack_entry(r, chunk[done])
                done += 1
            return total
        except Exception as exc:
            if attempt == ATTEMPTS - 1:
                raise
            wait = exc.wait if isinstance(exc, RateLimited) else min(2 ** attempt, 60)
            log(f"retrying from {chunk[done].name} in {wait:.0f}s: {exc}", file=sys.stderr)
            time.sleep(wait)
    return total


def plan_chunks(entries: list[Entry], limit: int = CHUNK_BYTES) -> list[list[Entry]]:
    """Group adjacent entries into runs of at most `limit` bytes (a bigger entry gets a run of its own)."""
    chunks: list[list[Entry]] = []
    for e in entries:
        cur = chunks[-1] if chunks else None
        if cur and cur[-1].end == e.start and e.end - cur[0].start <= limit:
            cur.append(e)
        else:
            chunks.append([e])
    return chunks


def snapshot(work_dir: str) -> dict:
    """Same walk as upstream agent_runner._snapshot_work_dir_files."""
    out = {}
    target = os.path.join(work_dir, "model_output")
    for dirpath, dirnames, filenames in os.walk(work_dir):
        if os.path.abspath(dirpath).startswith(target):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS and not d.startswith(".")]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            try:
                st = os.stat(path)
            except OSError:
                continue
            out[os.path.relpath(path, work_dir)] = [int(st.st_size), float(st.st_mtime)]
    return out


def main() -> None:
    from remotezip import RemoteZip

    p = argparse.ArgumentParser()
    p.add_argument("--language", choices=["en", "cn"], required=True)
    p.add_argument("--persona", required=True, help="Folder inside the zip, e.g. Research_Workdir")
    p.add_argument("--dest", required=True)
    p.add_argument("--workers", type=int, default=4, help="parallel range requests")
    a = p.parse_args()

    url = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/filesys_{a.language}.zip"
    prefix = a.persona.rstrip("/") + "/"
    dest = os.path.realpath(a.dest)
    with RemoteZip(url) as z:  # reads only the central directory
        infos = sorted(z.infolist(), key=lambda i: i.header_offset)
        dir_start = z.start_dir
    ends = [i.header_offset for i in infos[1:]] + [dir_start]

    entries = []
    for i, end in zip(infos, ends):
        if not i.filename.startswith(prefix):
            continue
        rel = i.filename[len(prefix):]
        out = os.path.realpath(os.path.join(dest, rel)) if rel else None
        if out is not None and not out.startswith(dest + os.sep):
            raise SystemExit(f"unsafe path in zip: {i.filename}")
        if out is not None and i.is_dir():
            os.makedirs(out, exist_ok=True)
            out = None
        elif out is not None:
            os.makedirs(os.path.dirname(out), exist_ok=True)
        entries.append(Entry(i, end, out))
    if not entries:
        raise SystemExit(f"{prefix} not found in {url}")

    chunks = plan_chunks(entries)
    files = [e for e in entries if e.out is not None]
    started = time.time()
    fetch = lambda s, e: http_range(url, s, e)  # noqa: E731
    with ThreadPoolExecutor(a.workers) as pool:
        total = sum(pool.map(lambda c: unpack_chunk(fetch, c), chunks))
    expected = sum(e.size for e in files)
    if total != expected:
        raise SystemExit(f"size mismatch: wrote {total} bytes, zip lists {expected}")
    print(f"{a.persona} ({a.language}): {len(files)} files, {total / 1e9:.2f} GB "
          f"in {len(chunks)} requests, {time.time() - started:.0f}s")

    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    with open(SNAPSHOT, "w") as f:
        json.dump({"created": time.time(), "files": snapshot(dest)}, f)


if __name__ == "__main__":
    main()
