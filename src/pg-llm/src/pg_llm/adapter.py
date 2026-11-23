"""Harbor adapter for PG-LLM (ProteinGym-LLM protein variant ranking benchmark).

Adapted from ``rohitarorayyc/proteingym-llm`` (MIT License).
Copyright (c) 2026 Rohit Arora, Leo Tianlai Chen, Melissa Du, Debora S. Marks,
and George M. Church.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import http.client
import json
import os
import random
import re
import shutil
import socket
import stat
import string
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunparse

EVAL_BUNDLE_VERSION = "v1.2"
EVAL_BUNDLE_FILENAME = f"proteingym-llm-eval-{EVAL_BUNDLE_VERSION}.tar.gz"
EVAL_BUNDLE_URL = (
    "https://api.github.com/repos/rohitarorayyc/proteingym-llm/releases/tags/eval-data-v1.2"
)
EVAL_BUNDLE_SHA256 = "b40ca6bb30741a652243e90e08a485ef48a52691bff8a075ec0f396200cf6f8a"
EVAL_BUNDLE_COMPRESSED_BYTES = 2_882_774
EVAL_BUNDLE_MANIFEST = "eval_bundle_manifest.json"
EVAL_BUNDLE_MANIFEST_SHA256 = "cdb7073cee6a651538c9db042be5fc84ca486d3164e646a686615f95b75cb908"
EVAL_BUNDLE_SCHEMA_VERSION = 2

EVAL_SIZES = (10, 50, 100)
EVAL_SEEDS = (1, 2, 3)
NETWORK_MODES = ("public", "no-network")
SELECTION_TYPES = ("Activity", "Binding", "Expression", "OrganismalFitness", "Stability")
PARITY_PER_CATEGORY = 10
PARITY_RANDOM_SEED = 42

SYSTEM_PROMPT = (
    "You will be given a wild-type protein sequence and a set of mutant sequences. "
    "Rank the mutants by their predicted effect on the assayed property, then output "
    "the ranking in the requested JSON format."
)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DOWNLOAD_CHUNK_SIZE = 1 << 20
_MAX_RELEASE_METADATA_BYTES = 1 << 20
_MAX_MANIFEST_BYTES = 64 << 20
_GITHUB_API_HOST = "api.github.com"
_GITHUB_REPO_API_PREFIX = "/repos/rohitarorayyc/proteingym-llm/"
_UPSTREAM_OUTPUT_DIRECTIVE = (
    "Reason through the ordering, then on the last line output ONLY the JSON object:\n"
)
_HARBOR_OUTPUT_DIRECTIVE = (
    "Reason through the ordering, then write ONLY the JSON object to /app/answer.json:\n"
)


class BundleError(RuntimeError):
    """Raised when the frozen evaluation bundle is missing, corrupt, or unsafe."""


def default_cache_root() -> Path:
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / "harbor-pg-llm"


def sanitize_task_id(dms_id: str, size: int, seed: int) -> str:
    """Convert an upstream DMS assay ID into a deterministic, registry-safe task ID."""
    slug = re.sub(r"[^a-z0-9-]+", "-", dms_id.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise ValueError(f"Unable to sanitize empty DMS ID: {dms_id!r}")
    return f"{slug}-n{size}-b{seed}"


def _adapt_prompt_for_file_submission(user_prompt: str) -> str:
    if _UPSTREAM_OUTPUT_DIRECTIVE not in user_prompt:
        raise ValueError("Upstream prompt missing expected output directive")
    return user_prompt.replace(_UPSTREAM_OUTPUT_DIRECTIVE, _HARBOR_OUTPUT_DIRECTIVE, 1)


def build_user_prompt(
    meta: dict,
    wt_seq: str,
    mutants: list[tuple[str, str, float]],
) -> tuple[str, list[str]]:
    """Construct the exact upstream PG-LLM ranking-v1 prompt and variant ID list."""
    assay = (meta.get("fitness_description") or "").strip()
    ids = [f"M{i + 1:02d}" for i in range(len(mutants))]
    lines = [
        f"**Protein:** {meta.get('target_name', '')}  ({meta.get('organism', '')})",
    ]
    if assay:
        lines.append(f"**Assay (what is measured):** {assay}")
    lines += [
        "**Higher experimental fitness = HIGHER value of the measured property.**",
        "",
        f"**Wild-type sequence ({len(wt_seq)} aa):**\n{wt_seq}",
        "",
        f"**{len(mutants)} candidate mutant sequences to rank:**",
    ]
    for mid, (_vid, seq, _score) in zip(ids, mutants):
        lines.append(f"{mid}: {seq}")
    lines += [
        "",
        f"Rank all {len(mutants)} mutants from the one you predict has the MOST "
        f"favorable effect on the assayed property (highest fitness) to the LEAST. "
        f"Reason through the ordering, then on the last line output ONLY the JSON object:\n"
        f'{{"ranking": ["M03", "M27", ... all {len(mutants)} ids, best to worst]}}',
    ]
    return "\n".join(lines), ids


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_DOWNLOAD_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(name: str) -> Path:
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if not name or name.startswith("/") or posix.is_absolute() or windows.is_absolute():
        raise BundleError(f"unsafe absolute or empty bundle path: {name!r}")
    if "\\" in name or windows.drive or any(part in {"", ".", ".."} for part in posix.parts):
        raise BundleError(f"unsafe bundle path: {name!r}")
    return Path(*posix.parts)


def _is_exact_int(val: object, expected: int) -> bool:
    return isinstance(val, int) and not isinstance(val, bool) and val == expected


def _has_user_symlink_component(path: Path) -> Path | None:
    """Return the first user-created symlink component along ``path`` without collapsing '..'."""
    raw_str = os.fspath(path)
    if os.path.isabs(raw_str):
        base_parts: list[str] = []
        rel_parts = [p for p in raw_str.split(os.sep) if p and p != "."]
    else:
        base_parts = [p for p in os.getcwd().split(os.sep) if p and p != "."]
        rel_parts = [p for p in raw_str.split(os.sep) if p and p != "."]

    cur = Path(os.sep)
    for part in (*base_parts, *rel_parts):
        if part == "..":
            cur = cur.parent
            continue
        cand = cur / part
        if cand.is_symlink():
            if (
                sys.platform == "darwin"
                and cand in {Path("/var"), Path("/tmp"), Path("/etc")}
                and cand.resolve() == Path("/private") / cand.name
            ):
                cur = Path("/private") / cand.name
                continue
            return cand
        cur = cand
    return None


def _reject_symlink_path(
    path: Path, label: str, *, exc_type: type[Exception] = BundleError
) -> None:
    sym = _has_user_symlink_component(path)
    if sym is not None:
        raise exc_type(f"{label} cannot be or traverse a symlink: {sym} (via {path})")


def _safe_target(data_root: Path, relative: Path) -> Path:
    _reject_symlink_path(data_root, "Data root")
    root = data_root.resolve()
    cur = root
    for part in relative.parts:
        cur = cur / part
        if cur.is_symlink():
            raise BundleError(f"Links are forbidden in evaluation bundle: {relative.as_posix()!r}")
    target = root.joinpath(relative)
    resolved = target.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise BundleError(f"bundle target escapes data root: {relative.as_posix()!r}")
    return target


def _is_trusted_github_api_url(url: str) -> bool:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == _GITHUB_API_HOST
        and port in {None, 443}
        and parsed.path.startswith(_GITHUB_REPO_API_PREFIX)
    )


def _download_headers(url: str, *, binary: bool) -> dict[str, str]:
    headers = {
        "User-Agent": "harbor-pg-llm-adapter/0.1",
        "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and _is_trusted_github_api_url(url):
        headers["Authorization"] = f"Bearer {token}"
    return headers


class _DeadlineStream:
    """Wrap a buffered stream so every framing/body receive obeys a wall-clock deadline."""

    def __init__(self, inner, deadline: float, stage: str, *, resp=None) -> None:
        self._inner = inner
        self._deadline = deadline
        self._stage = stage
        self._resp = resp

    def _before(self) -> float:
        rem = _remaining_timeout(self._deadline, self._stage)
        if self._resp is not None:
            _apply_resp_timeout(self._resp, rem)
        elif hasattr(self._inner, "raw") and hasattr(self._inner.raw, "_sock"):
            sock = self._inner.raw._sock
            if hasattr(sock, "settimeout"):
                sock.settimeout(rem)
        return rem

    def read(self, *args, **kwargs):
        self._before()
        data = self._inner.read(*args, **kwargs)
        _remaining_timeout(self._deadline, self._stage)
        return data

    def read1(self, *args, **kwargs):
        self._before()
        fn = getattr(self._inner, "read1", self._inner.read)
        data = fn(*args, **kwargs)
        _remaining_timeout(self._deadline, self._stage)
        return data

    def readline(self, *args, **kwargs):
        self._before()
        data = self._inner.readline(*args, **kwargs)
        _remaining_timeout(self._deadline, self._stage)
        return data

    def readinto(self, *args, **kwargs):
        self._before()
        res = self._inner.readinto(*args, **kwargs)
        _remaining_timeout(self._deadline, self._stage)
        return res

    def readinto1(self, *args, **kwargs):
        self._before()
        fn = getattr(self._inner, "readinto1", self._inner.readinto)
        res = fn(*args, **kwargs)
        _remaining_timeout(self._deadline, self._stage)
        return res

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _DeadlineSocket:
    """Wrap a raw/SSL socket to enforce remaining monotonic wall-clock timeout per I/O call."""

    def __init__(self, sock, deadline: float, stage: str) -> None:
        self._sock = sock
        self._deadline = deadline
        self._stage = stage

    def _tick(self, stage: str | None = None) -> float:
        rem = _remaining_timeout(self._deadline, stage or self._stage)
        if hasattr(self._sock, "settimeout"):
            self._sock.settimeout(rem)
        return rem

    def connect(self, *args, **kwargs):
        self._tick("connecting")
        res = self._sock.connect(*args, **kwargs)
        self._tick("connecting")
        return res

    def recv(self, *args, **kwargs):
        self._tick()
        res = self._sock.recv(*args, **kwargs)
        self._tick()
        return res

    def recv_into(self, *args, **kwargs):
        self._tick()
        res = self._sock.recv_into(*args, **kwargs)
        self._tick()
        return res

    def send(self, *args, **kwargs):
        self._tick("sending request")
        res = self._sock.send(*args, **kwargs)
        self._tick("sending request")
        return res

    def sendall(self, *args, **kwargs):
        self._tick("sending request")
        res = self._sock.sendall(*args, **kwargs)
        self._tick("sending request")
        return res

    def makefile(self, *args, **kwargs):
        raw_io = self._sock.makefile(*args, **kwargs)
        for candidate in (
            raw_io,
            getattr(raw_io, "raw", None),
            getattr(getattr(raw_io, "buffer", None), "raw", None),
        ):
            if candidate is not None and getattr(candidate, "_sock", None) is self._sock:
                candidate._sock = self
                break
        return _DeadlineStream(raw_io, self._deadline, self._stage, resp=self)

    def __getattr__(self, name: str):
        return getattr(self._sock, name)


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection enforcing a shared monotonic deadline across connect, TLS, and headers."""

    def __init__(self, host: str, deadline: float, **kwargs) -> None:
        kwargs["timeout"] = _remaining_timeout(deadline, "opening HTTPS connection")
        super().__init__(host, **kwargs)
        self._deadline = deadline

    def _resolve_with_deadline(self, host: str, port: int) -> list[tuple]:
        result: list[tuple] | None = None
        exc_box: list[BaseException] = []

        def _worker() -> None:
            nonlocal result
            try:
                result = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
            except BaseException as exc:  # noqa: BLE001
                exc_box.append(exc)

        rem = _remaining_timeout(self._deadline, "resolving host")
        thread = threading.Thread(target=_worker, name="pg-llm-dns", daemon=True)
        thread.start()
        thread.join(timeout=rem)
        if thread.is_alive():
            raise BundleError("Timed out while resolving host (wall-clock budget exceeded)")
        _remaining_timeout(self._deadline, "resolving host")
        if exc_box:
            raise exc_box[0]
        if not result:
            raise OSError("getaddrinfo returns an empty list")
        return result

    def _connect_with_deadline(
        self,
        address: tuple[str, int],
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        host, port = address
        err: OSError | None = None
        for af, socktype, proto, _canonname, sa in self._resolve_with_deadline(host, port):
            rem = _remaining_timeout(self._deadline, "connecting to host")
            sock: socket.socket | None = None
            try:
                sock = socket.socket(af, socktype, proto)
                sock.settimeout(rem)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sa)
                _remaining_timeout(self._deadline, "connecting to host")
                return sock
            except BaseException as exc:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if isinstance(exc, OSError):
                    err = exc
                    if isinstance(exc, TimeoutError):
                        _remaining_timeout(self._deadline, "connecting to host")
                else:
                    raise
        _remaining_timeout(self._deadline, "connecting to host")
        if err is not None:
            raise err
        raise OSError("getaddrinfo returns an empty list")

    def connect(self) -> None:
        self.timeout = _remaining_timeout(self._deadline, "connecting to host")
        raw_sock = self._connect_with_deadline((self.host, self.port), self.source_address)
        try:
            if self._tunnel_host:
                self.sock = _DeadlineSocket(raw_sock, self._deadline, "HTTP tunnel")
                self._tunnel()
                server_hostname = self._tunnel_host
            else:
                server_hostname = self.host
            rem = _remaining_timeout(self._deadline, "TLS handshake")
            if hasattr(raw_sock, "settimeout"):
                raw_sock.settimeout(rem)
            ssl_sock = self._context.wrap_socket(raw_sock, server_hostname=server_hostname)
            _remaining_timeout(self._deadline, "TLS handshake")
            self.sock = _DeadlineSocket(ssl_sock, self._deadline, "reading HTTPS response")
        except BaseException:
            raw_sock.close()
            self.sock = None
            raise

    def getresponse(self):
        self.timeout = _remaining_timeout(self._deadline, "reading response headers")
        if self.sock is not None and hasattr(self.sock, "settimeout"):
            self.sock.settimeout(self.timeout)
        resp = super().getresponse()
        _remaining_timeout(self._deadline, "reading response headers")
        return resp


class _DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline: float) -> None:
        super().__init__()
        self._deadline = deadline

    def https_open(self, req):
        req.timeout = _remaining_timeout(self._deadline, "opening HTTPS request")
        return self.do_open(
            lambda host, **kwargs: _DeadlineHTTPSConnection(
                host, deadline=self._deadline, **kwargs
            ),
            req,
        )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, deadline: float | None = None) -> None:
        self._deadline = deadline

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected is not None:
            if self._deadline is not None:
                redirected.timeout = _remaining_timeout(self._deadline, "following redirect")
            old = urlsplit(request.full_url)
            new = urlsplit(newurl)
            if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
                redirected.remove_header("Authorization")
        return redirected

    def http_error_302(self, req, fp, code, msg, headers):
        newurl = next(
            (v for k, v in headers.items() if k.lower() == "location"),
            None,
        ) or next(
            (v for k, v in headers.items() if k.lower() == "uri"),
            None,
        )
        if not newurl:
            return None

        urlparts = urlparse(newurl)
        if urlparts.scheme not in ("https", ""):
            raise urllib.error.HTTPError(
                newurl,
                code,
                f"{msg} - Redirection to url {newurl!r} is not allowed",
                headers,
                fp,
            )

        if not urlparts.path and urlparts.netloc:
            urlparts = urlparts._replace(path="/")
        newurl = urlunparse(urlparts)
        newurl = quote(newurl, encoding="iso-8859-1", safe=string.punctuation)
        newurl = urljoin(req.full_url, newurl)
        if urlparse(newurl).scheme != "https":
            raise urllib.error.HTTPError(
                newurl,
                code,
                f"{msg} - Redirection to non-HTTPS url {newurl!r} is not allowed",
                headers,
                fp,
            )

        new = self.redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None

        if hasattr(req, "redirect_dict"):
            visited = new.redirect_dict = req.redirect_dict
            if (
                visited.get(newurl, 0) >= self.max_repeats
                or len(visited) >= self.max_redirections
            ):
                raise urllib.error.HTTPError(req.full_url, code, self.inf_msg + msg, headers, fp)
        else:
            visited = new.redirect_dict = req.redirect_dict = {}
        visited[newurl] = visited.get(newurl, 0) + 1

        # Close redirect response immediately without draining its body so an
        # unresponsive server cannot stall past the wall-clock deadline.
        fp.close()

        remaining = (
            _remaining_timeout(self._deadline, "following redirect")
            if self._deadline is not None
            else getattr(req, "timeout", None)
        )
        if remaining is not None:
            new.timeout = remaining
            return self.parent.open(new, timeout=remaining)
        return self.parent.open(new, timeout=req.timeout)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _open_url(request: urllib.request.Request, deadline: float):
    opener = urllib.request.build_opener(
        _DeadlineHTTPSHandler(deadline=deadline),
        _SafeRedirectHandler(deadline=deadline),
    )
    return opener.open(request, timeout=_remaining_timeout(deadline, "opening URL"))


def _remaining_timeout(deadline: float, stage: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BundleError(f"Timed out while {stage} (wall-clock budget exceeded)")
    return remaining


def _apply_resp_timeout(resp, remaining: float) -> None:
    if hasattr(resp, "settimeout"):
        resp.settimeout(remaining)
        return
    fp = getattr(resp, "fp", None)
    inner_fp = getattr(fp, "_inner", fp)
    raw = getattr(inner_fp, "raw", None)
    for obj in (
        getattr(raw, "_sock", None),
        raw,
        getattr(inner_fp, "_sock", None),
        inner_fp,
    ):
        if obj is not None and hasattr(obj, "settimeout"):
            obj.settimeout(remaining)
            return


def _iter_bounded_chunks(
    resp,
    max_bytes: int,
    deadline: float,
    label: str,
    *,
    overcap_msg: str | None = None,
) -> Iterable[bytes]:
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None)
    raw_sock = getattr(raw, "_sock", None)
    if (
        raw_sock is not None
        and not isinstance(raw_sock, _DeadlineSocket)
        and (hasattr(raw_sock, "recv") or hasattr(raw_sock, "recv_into"))
    ):
        raw._sock = _DeadlineSocket(raw_sock, deadline, f"reading {label}")
    if (
        fp is not None
        and not isinstance(fp, _DeadlineStream)
        and (hasattr(fp, "read") or hasattr(fp, "readline"))
    ):
        resp.fp = _DeadlineStream(fp, deadline, f"reading {label}", resp=resp)
    # Prefer single-recv read1 when available so buffered HTTPResponse cannot
    # loop past remaining wall-clock deadline before returning to the outer loop.
    read_fn = getattr(resp, "read1", resp.read)
    total = 0
    while True:
        remaining = _remaining_timeout(deadline, f"reading {label}")
        _apply_resp_timeout(resp, remaining)
        to_read = min(_DOWNLOAD_CHUNK_SIZE, (max_bytes - total) + 1)
        chunk = read_fn(to_read)
        _remaining_timeout(deadline, f"reading {label}")
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise BundleError(
                overcap_msg or f"{label} exceeds maximum allowed size ({max_bytes} bytes)"
            )
        yield chunk


def _read_bounded_response(resp, max_bytes: int, deadline: float, label: str) -> bytes:
    return b"".join(_iter_bounded_chunks(resp, max_bytes, deadline, label))


def _download_archive(archive_path: Path, timeout: float = 120) -> Path:
    _reject_symlink_path(archive_path, "Archive path")
    if archive_path.is_file() and _sha256_file(archive_path) == EVAL_BUNDLE_SHA256:
        return archive_path

    deadline = time.monotonic() + timeout
    release_req = urllib.request.Request(
        EVAL_BUNDLE_URL, headers=_download_headers(EVAL_BUNDLE_URL, binary=False)
    )
    try:
        with _open_url(release_req, deadline) as resp:
            payload = _read_bounded_response(
                resp, _MAX_RELEASE_METADATA_BYTES, deadline, "PG-LLM release metadata"
            )
            release = json.loads(payload.decode("utf-8"))
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        json.JSONDecodeError,
        UnicodeDecodeError,
        TimeoutError,
        OSError,
    ) as exc:
        raise BundleError(f"Failed to resolve PG-LLM release metadata: {exc}") from exc

    if not isinstance(release, dict):
        raise BundleError("GitHub release metadata must be a JSON object")
    assets = release.get("assets")
    if not isinstance(assets, list) or not all(isinstance(a, dict) for a in assets):
        raise BundleError("GitHub release metadata contains invalid 'assets' list")

    matches = [asset for asset in assets if asset.get("name") == EVAL_BUNDLE_FILENAME]
    if len(matches) != 1:
        raise BundleError(f"Release does not contain unique asset {EVAL_BUNDLE_FILENAME!r}")
    asset_url = matches[0].get("url") or matches[0].get("api_url")
    if not isinstance(asset_url, str) or not asset_url.startswith(
        "https://api.github.com/repos/rohitarorayyc/proteingym-llm/releases/assets/"
    ):
        raise BundleError("GitHub release returned an unexpected asset API URL")

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{archive_path.name}.", suffix=".download", dir=archive_path.parent
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        digest = hashlib.sha256()
        asset_req = urllib.request.Request(
            asset_url, headers=_download_headers(asset_url, binary=True)
        )
        try:
            with _open_url(asset_req, deadline) as resp, tmp_path.open("wb") as out:
                total_bytes = 0
                for chunk in _iter_bounded_chunks(
                    resp,
                    EVAL_BUNDLE_COMPRESSED_BYTES,
                    deadline,
                    "PG-LLM archive",
                    overcap_msg=f"Archive download exceeds expected compressed size ({EVAL_BUNDLE_COMPRESSED_BYTES} bytes)",
                ):
                    total_bytes += len(chunk)
                    out.write(chunk)
                    digest.update(chunk)
                if total_bytes != EVAL_BUNDLE_COMPRESSED_BYTES:
                    raise BundleError(
                        f"Archive size mismatch: expected {EVAL_BUNDLE_COMPRESSED_BYTES} bytes, got {total_bytes}"
                    )
                out.flush()
                os.fsync(out.fileno())
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
        ) as exc:
            raise BundleError(f"Failed to download PG-LLM archive: {exc}") from exc

        actual = digest.hexdigest()
        if actual != EVAL_BUNDLE_SHA256:
            raise BundleError(
                f"Archive SHA-256 mismatch: expected {EVAL_BUNDLE_SHA256}, got {actual}"
            )
        os.replace(tmp_path, archive_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return archive_path


def _parse_manifest(payload: bytes) -> dict:
    manifest = json.loads(payload.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise BundleError("Bundle manifest must be a JSON object")
    if not _is_exact_int(manifest.get("schema_version"), EVAL_BUNDLE_SCHEMA_VERSION):
        raise BundleError(
            f"Unsupported bundle manifest schema: {manifest.get('schema_version')!r}"
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise BundleError("Bundle manifest has no files")
    for name, record in files.items():
        _safe_relative_path(name)
        if not isinstance(record, dict):
            raise BundleError(f"Invalid manifest record for {name!r}")
        sha = str(record.get("sha256", ""))
        if not _SHA256_RE.fullmatch(sha):
            raise BundleError(f"Invalid SHA-256 in manifest for {name!r}")
        record["sha256"] = sha.lower()
        size = record.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BundleError(f"Invalid size in manifest for {name!r}")
    return manifest


def verify_installed_bundle(data_root: Path) -> dict:
    """Verify an installed eval-data-v1.2 directory against the pinned manifest digest."""
    _reject_symlink_path(data_root, "Data root")
    manifest_path = _safe_target(data_root, Path(EVAL_BUNDLE_MANIFEST))
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BundleError(f"Missing bundle manifest: {manifest_path}")
    payload = manifest_path.read_bytes()
    actual_manifest_sha = hashlib.sha256(payload).hexdigest()
    if actual_manifest_sha != EVAL_BUNDLE_MANIFEST_SHA256:
        raise BundleError(
            f"Manifest SHA-256 mismatch: expected {EVAL_BUNDLE_MANIFEST_SHA256}, "
            f"got {actual_manifest_sha}"
        )
    manifest = _parse_manifest(payload)
    for name, record in sorted(manifest["files"].items()):
        path = _safe_target(data_root, _safe_relative_path(name))
        if not path.is_file() or path.is_symlink():
            raise BundleError(f"Missing bundle file: {name!r}")
        if path.stat().st_size != record["size"]:
            raise BundleError(f"File size mismatch for {name!r}")
        if _sha256_file(path) != record["sha256"]:
            raise BundleError(f"File SHA-256 mismatch for {name!r}")
    return manifest


def _extract_bundle(archive_path: Path, data_root: Path) -> None:
    _reject_symlink_path(archive_path, "Archive path")
    _reject_symlink_path(data_root, "Data root")
    if _sha256_file(archive_path) != EVAL_BUNDLE_SHA256:
        raise BundleError(f"Archive checksum mismatch for {archive_path}")

    data_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:*") as handle:
        members: dict[str, tarfile.TarInfo] = {}
        manifest_payload: bytes | None = None
        for member in handle.getmembers():
            rel = _safe_relative_path(member.name)
            name = rel.as_posix()
            if member.issym() or member.islnk():
                raise BundleError(f"Links are forbidden in evaluation bundle: {name!r}")
            if member.isdir():
                continue
            if not member.isfile():
                raise BundleError(f"Special file forbidden in evaluation bundle: {name!r}")
            if name in members or (name == EVAL_BUNDLE_MANIFEST and manifest_payload is not None):
                raise BundleError(f"Duplicate bundle member: {name!r}")
            stream = handle.extractfile(member)
            if stream is None:
                raise BundleError(f"Unable to read bundle member: {name!r}")
            if name == EVAL_BUNDLE_MANIFEST:
                manifest_payload = stream.read(_MAX_MANIFEST_BYTES + 1)
                if len(manifest_payload) > _MAX_MANIFEST_BYTES:
                    raise BundleError("Bundle manifest exceeds size cap")
            else:
                members[name] = member

        if manifest_payload is None:
            raise BundleError(f"Archive is missing {EVAL_BUNDLE_MANIFEST}")
        manifest = _parse_manifest(manifest_payload)
        normalized_manifest = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        actual_manifest_sha = hashlib.sha256(normalized_manifest).hexdigest()
        if actual_manifest_sha != EVAL_BUNDLE_MANIFEST_SHA256:
            raise BundleError(
                f"Manifest SHA-256 mismatch: expected {EVAL_BUNDLE_MANIFEST_SHA256}, "
                f"got {actual_manifest_sha}"
            )
        if set(members) != set(manifest["files"]):
            raise BundleError("Archive member set does not match manifest")

        with tempfile.TemporaryDirectory(prefix=".eval-bundle-", dir=data_root) as stage_dir:
            stage = Path(stage_dir)
            for name, member in sorted(members.items()):
                record = manifest["files"][name]
                stream = handle.extractfile(member)
                if stream is None:
                    raise BundleError(f"Unable to extract {name!r}")
                staged = stage / _safe_relative_path(name)
                staged.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                written = 0
                with staged.open("wb") as out:
                    while chunk := stream.read(_DOWNLOAD_CHUNK_SIZE):
                        written += len(chunk)
                        if written > record["size"]:
                            raise BundleError(f"Bundle member {name!r} exceeds manifest size")
                        out.write(chunk)
                        digest.update(chunk)
                if written != record["size"] or digest.hexdigest() != record["sha256"]:
                    raise BundleError(f"Extracted member failed checksum/size check: {name!r}")

            (stage / EVAL_BUNDLE_MANIFEST).write_bytes(normalized_manifest)
            for name in [*sorted(members), EVAL_BUNDLE_MANIFEST]:
                rel = _safe_relative_path(name)
                dest = _safe_target(data_root, rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage / rel, dest)

    verify_installed_bundle(data_root)


def _resolve_eval_data_paths(
    data_dir: Path | None = None,
) -> tuple[Path, Path | None, Path | None]:
    """Resolve the effective data root and, when cached, cache root and archive path."""
    if data_dir is not None:
        return Path(data_dir), None, None
    cache_root = default_cache_root()
    data_root = cache_root / f"eval-data-{EVAL_BUNDLE_VERSION}"
    archive_path = cache_root / EVAL_BUNDLE_FILENAME
    return data_root, cache_root, archive_path


def ensure_eval_data(
    data_dir: Path | None = None,
    *,
    cache_root: Path | None = None,
    data_root: Path | None = None,
    archive_path: Path | None = None,
) -> Path:
    """Return a verified eval-data-v1.2 directory, downloading and caching if needed."""
    if data_dir is not None:
        resolved_root = Path(data_dir)
        _reject_symlink_path(resolved_root, "Data directory")
        verify_installed_bundle(resolved_root)
        return resolved_root

    _, default_cache, _ = _resolve_eval_data_paths(None)
    assert default_cache is not None
    use_cache = Path(cache_root) if cache_root is not None else default_cache
    use_data = Path(data_root) if data_root is not None else (use_cache / f"eval-data-{EVAL_BUNDLE_VERSION}")
    use_archive = Path(archive_path) if archive_path is not None else (use_cache / EVAL_BUNDLE_FILENAME)

    _reject_symlink_path(use_cache, "Cache root")
    _reject_symlink_path(use_data, "Cached data directory")
    _reject_symlink_path(use_archive, "Cached archive")

    manifest_file = use_data / EVAL_BUNDLE_MANIFEST
    if manifest_file.is_file() and not manifest_file.is_symlink():
        try:
            verify_installed_bundle(use_data)
            return use_data
        except BundleError:
            pass

    _download_archive(use_archive)
    _extract_bundle(use_archive, use_data)
    return use_data


def _load_assay_meta(data_root: Path) -> dict[str, dict]:
    ref_path = data_root / "reference" / "DMS_substitutions.csv"
    manifest_path = data_root / "splits" / "manifest.csv"

    with manifest_path.open(encoding="utf-8", newline="") as handle:
        runnable = {
            (row.get("assay") or "").strip()
            for row in csv.DictReader(handle)
            if (row.get("assay") or "").strip()
        }

    with ref_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    meta: dict[str, dict] = {}
    for row in rows:
        assay_id = (row.get("DMS_id") or "").strip()
        if not assay_id:
            raise ValueError("Reference metadata contains empty DMS_id")
        wt = (row.get("target_seq") or "").strip()
        desc = (row.get("fitness_description") or "").strip()
        if not desc:
            raise ValueError(f"Missing fitness_description for {assay_id}")
        meta[assay_id] = {
            "dms_id": assay_id,
            "reference_sequence": wt,
            "target_name": (row.get("UniProt_ID") or assay_id).strip(),
            "uniprot_id": (row.get("UniProt_ID") or "").strip(),
            "organism": (row.get("source_organism") or row.get("taxon") or "").strip(),
            "fitness_description": desc,
            "function": (row.get("coarse_selection_type") or "?").strip() or "?",
            "taxon": (row.get("taxon") or "?").strip() or "?",
            "msa_category": (row.get("MSA_Neff_L_category") or "?").strip() or "?",
            "seq_len": int(row.get("seq_len") or len(wt) or 0),
            "multi": str(row.get("includes_multiple_mutants", "")).strip().lower()
            in {"true", "1", "yes"},
        }

    if set(meta) != runnable:
        missing = sorted(runnable - set(meta))
        extra = sorted(set(meta) - runnable)
        raise ValueError(f"Metadata/manifest assay mismatch: missing={missing}, extra={extra}")
    return meta


def _select_parity_assays(meta: dict[str, dict]) -> list[str]:
    """Select a deterministic 50-assay stratified parity split (10 per selection type)."""
    by_category: dict[str, list[str]] = defaultdict(list)
    for assay_id, info in meta.items():
        by_category[info["function"]].append(assay_id)

    rng = random.Random(PARITY_RANDOM_SEED)
    selected: list[str] = []
    for category in SELECTION_TYPES:
        candidates = sorted(by_category.get(category, []))
        if len(candidates) < PARITY_PER_CATEGORY:
            raise ValueError(
                f"Category {category!r} has only {len(candidates)} assays (< {PARITY_PER_CATEGORY})"
            )
        selected.extend(rng.sample(candidates, PARITY_PER_CATEGORY))
    return sorted(selected)


def _load_split_episode(
    data_root: Path, assay_id: str, size: int, seed: int
) -> list[tuple[str, str, float]]:
    split_path = data_root / "splits" / assay_id / f"n{size}_b{seed}.json"
    label_path = data_root / "splits" / assay_id / f"n{size}_b{seed}.labels.json"
    if not split_path.is_file() or not label_path.is_file():
        raise FileNotFoundError(f"Missing frozen split for {assay_id} n{size} b{seed}")
    split = json.loads(split_path.read_text(encoding="utf-8"))
    labels = json.loads(label_path.read_text(encoding="utf-8"))
    return [
        (variant["id"], variant["seq"], float(labels[variant["id"]]))
        for variant in split["variants"]
    ]


def _render_template(template: str, mapping: dict[str, str]) -> str:
    rendered = template
    for key, value in mapping.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


_REQUIRED_TASK_FILES = (
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "solution/solve.sh",
    "tests/test.sh",
    "tests/grade.py",
    "tests/THIRD_PARTY_LICENSES.md",
    "tests/labels.json",
)
_EXECUTABLE_REL_PATHS = frozenset({"solution/solve.sh", "tests/test.sh"})
_ALLOWED_TASK_SUBDIRS = frozenset({"environment", "solution", "tests"})
_PG_LLM_TASK_NAME_PREFIX = "markslab/pg-llm__"
_PG_LLM_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-n(?:10|50|100)-b[123]$")
_MANIFEST_FILENAME = ".pg-llm-manifest.json"
_MANIFEST_SCHEMA_VERSION = 1
_MANIFEST_ADAPTER_ID = "markslab/pg-llm"
_TX_DIR_SUFFIX = ".pg-llm-tx"
_TX_STATE_SUFFIX = ".pg-llm-tx.state"
_TX_STATE_TMP_SUFFIX = ".pg-llm-tx.state.tmp"
_LOCK_FILENAME_SUFFIX = ".pg-llm.lock"
_TX_MARKER_FILENAME = ".pg-llm-tx.json"
_TX_MARKER_TMP_FILENAME = ".pg-llm-tx.json.tmp"
_MANIFEST_TMP_NAME = ".pg-llm-manifest.json.tmp"
_LICENSE_TMP_NAME = "THIRD_PARTY_LICENSES.md.tmp"
_TX_SCHEMA_VERSION = 1
_TX_ALLOWED_CHILDREN = frozenset({_TX_MARKER_FILENAME, "journal", "staging", "backups", "tmp"})
_TX_STATE_ALLOWED_KEYS = frozenset(
    {"schema_version", "adapter", "output_root", "tx_state", "tx_dev", "tx_ino"}
)
_MANIFEST_TMP_ALLOWED_KEYS = frozenset(
    {"schema_version", "adapter", "tasks", "root_license_sha256"}
)
_JOURNAL_TMP_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "adapter",
        "output_root",
        "op",
        "task_id",
        "staging_name",
        "backup_name",
        "previous_files",
        "replacement_files",
    }
)
_ALLOWED_ROOT_FILES = frozenset({"THIRD_PARTY_LICENSES.md", _MANIFEST_FILENAME})


def _paths_overlap(a: Path, b: Path) -> bool:
    la = Path(os.path.abspath(a))
    lb = Path(os.path.abspath(b))
    if la == lb or la in lb.parents or lb in la.parents:
        return True
    ra = a.resolve(strict=False)
    rb = b.resolve(strict=False)
    return ra == rb or ra in rb.parents or rb in ra.parents


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_task_name(task_toml_path: Path) -> str | None:
    if task_toml_path.is_symlink() or not task_toml_path.is_file():
        return None
    try:
        text = task_toml_path.read_text(encoding="utf-8")
        parsed = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    task_tbl = parsed.get("task") if isinstance(parsed, dict) else None
    name = task_tbl.get("name") if isinstance(task_tbl, dict) else None
    return name if isinstance(name, str) else None


def _is_pg_llm_task_dir(path: Path, expected_task_id: str | None = None) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    name = _read_task_name(path / "task.toml")
    if not isinstance(name, str):
        return False
    if expected_task_id is not None:
        return name == f"{_PG_LLM_TASK_NAME_PREFIX}{expected_task_id}"
    return name.startswith(_PG_LLM_TASK_NAME_PREFIX) and len(name) > len(_PG_LLM_TASK_NAME_PREFIX)


def _write_file_atomic_via_tmp(
    tmp_path: Path, path: Path, payload: bytes, mode: int = 0o644
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    if tmp_path.is_symlink() or (tmp_path.exists() and not tmp_path.is_file()):
        raise FileExistsError(f"Invalid temporary file entry at {tmp_path}; preserving untouched")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp_path, flags, mode)
    try:
        mv = memoryview(payload)
        written = 0
        while written < len(payload):
            n = os.write(fd, mv[written:])
            if n <= 0:
                raise OSError(f"short write while writing {tmp_path}")
            written += n
        os.fsync(fd)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)


def _atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        _write_file_atomic_via_tmp(tmp_path, path, payload, mode=mode)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _load_output_manifest_state(
    output_dir: Path,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    manifest_path = output_dir / _MANIFEST_FILENAME
    if manifest_path.is_symlink() or (manifest_path.exists() and not manifest_path.is_file()):
        raise ValueError(f"Invalid PG-LLM manifest entry at {manifest_path}")
    if not manifest_path.exists():
        return {}, None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Corrupt PG-LLM manifest {manifest_path}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or not _is_exact_int(data.get("schema_version"), _MANIFEST_SCHEMA_VERSION)
        or data.get("adapter") != _MANIFEST_ADAPTER_ID
        or not isinstance(data.get("tasks"), dict)
    ):
        raise ValueError(f"Unsupported or invalid PG-LLM manifest schema in {manifest_path}")

    raw_lic_sha = data.get("root_license_sha256")
    lic_sha: str | None = None
    if raw_lic_sha is not None:
        if not isinstance(raw_lic_sha, str) or not _SHA256_RE.fullmatch(raw_lic_sha):
            raise ValueError(f"Invalid root_license_sha256 in {manifest_path}")
        lic_sha = raw_lic_sha.lower()

    required_set = set(_REQUIRED_TASK_FILES)
    tasks: dict[str, dict[str, Any]] = {}
    for tid, record in data["tasks"].items():
        if not isinstance(tid, str) or not _PG_LLM_TASK_ID_RE.fullmatch(tid):
            raise ValueError(f"Invalid task ID in manifest {manifest_path}: {tid!r}")
        if not isinstance(record, dict):
            raise ValueError(f"Invalid manifest record for {tid!r} in {manifest_path}")
        if record.get("task_name") != f"{_PG_LLM_TASK_NAME_PREFIX}{tid}":
            raise ValueError(f"Mismatched task_name for {tid!r} in {manifest_path}")
        files = record.get("files")
        if not isinstance(files, dict) or set(files) != required_set:
            raise ValueError(f"Invalid file map for {tid!r} in {manifest_path}")
        norm_files: dict[str, str] = {}
        for rel_path, digest in files.items():
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise ValueError(f"Invalid digest for {tid!r}:{rel_path!r} in {manifest_path}")
            norm_files[rel_path] = digest.lower()
        tasks[tid] = {"task_name": f"{_PG_LLM_TASK_NAME_PREFIX}{tid}", "files": norm_files}
    return tasks, lic_sha


def _load_output_manifest(output_dir: Path) -> dict[str, dict[str, Any]]:
    tasks, _ = _load_output_manifest_state(output_dir)
    return tasks


def _write_output_manifest(
    output_dir: Path,
    tasks: dict[str, dict[str, Any]],
    *,
    root_license_sha256: str | None = None,
    tmp_root: Path | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "adapter": _MANIFEST_ADAPTER_ID,
        "tasks": dict(sorted(tasks.items())),
    }
    if root_license_sha256 is not None:
        payload["root_license_sha256"] = root_license_sha256
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    dest = output_dir / _MANIFEST_FILENAME
    if tmp_root is not None:
        _write_file_atomic_via_tmp(tmp_root / _MANIFEST_TMP_NAME, dest, encoded, mode=0o644)
    else:
        _atomic_write_bytes(dest, encoded, mode=0o644)


def _verify_regular_task_tree(
    task_dir: Path,
    task_id: str,
    allowed_files: set[str],
    *,
    require_task_toml: bool = True,
) -> tuple[bool, str, set[str]]:
    if task_dir.is_symlink() or not task_dir.is_dir():
        return False, "not_a_real_directory", set()
    actual_files: set[str] = set()
    for entry in sorted(task_dir.rglob("*")):
        rel = entry.relative_to(task_dir).as_posix()
        if entry.is_symlink():
            return False, f"symlink({rel})", actual_files
        if entry.is_dir():
            if rel not in _ALLOWED_TASK_SUBDIRS:
                return False, f"extra_entry({rel})", actual_files
        elif entry.is_file():
            if rel not in allowed_files:
                return False, f"extra_entry({rel})", actual_files
            actual_files.add(rel)
        else:
            return False, f"extra_entry({rel})", actual_files
    task_toml = task_dir / "task.toml"
    if require_task_toml or task_toml.exists() or task_toml.is_symlink():
        if _read_task_name(task_toml) != f"{_PG_LLM_TASK_NAME_PREFIX}{task_id}":
            return False, "unowned_or_wrong_task", actual_files
    return True, "ok", actual_files


def _verify_partial_deletable_tree(
    tree_dir: Path, task_id: str, allowed_digests: dict[str, str]
) -> tuple[bool, str]:
    ok, reason, actual_files = _verify_regular_task_tree(
        tree_dir, task_id, set(allowed_digests), require_task_toml=False
    )
    if not ok:
        return False, reason
    for rel_path in sorted(actual_files):
        if _sha256_file(tree_dir / rel_path) != allowed_digests[rel_path].lower():
            return False, f"digest_mismatch({rel_path})"
    return True, "ok"


def _verify_task_dir_digests(
    task_dir: Path,
    task_id: str,
    expected_digests: dict[str, str],
    *,
    check_exec: bool = True,
) -> tuple[bool, str]:
    ok, reason, actual_files = _verify_regular_task_tree(
        task_dir, task_id, set(expected_digests)
    )
    if not ok:
        return False, reason
    if actual_files != set(expected_digests):
        missing = sorted(set(expected_digests) - actual_files)
        return False, f"missing_files({missing})"
    for rel_path, expected_sha in sorted(expected_digests.items()):
        candidate = task_dir / rel_path
        if _sha256_file(candidate) != expected_sha.lower():
            return False, f"digest_mismatch({rel_path})"
        if check_exec and rel_path in _EXECUTABLE_REL_PATHS and not (
            candidate.stat().st_mode & 0o100
        ):
            return False, f"non_executable({rel_path})"
    return True, "ok"


def _verify_prunable_owned_task(
    entry: Path, manifest_tasks: dict[str, dict[str, Any]]
) -> tuple[bool, str]:
    if entry.is_symlink() or not entry.is_dir():
        return False, "not_a_real_directory"
    if not _PG_LLM_TASK_ID_RE.fullmatch(entry.name):
        return False, "invalid_task_id_dir_name"
    rec = manifest_tasks.get(entry.name)
    if rec is None:
        return False, "unrecorded_in_manifest"
    return _verify_task_dir_digests(entry, entry.name, rec["files"], check_exec=False)


def _validate_digest_map(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    allowed = set(_REQUIRED_TASK_FILES)
    out: dict[str, str] = {}
    for rel, sha in raw.items():
        if (
            not isinstance(rel, str)
            or rel not in allowed
            or not isinstance(sha, str)
            or not _SHA256_RE.fullmatch(sha)
        ):
            return None
        out[rel] = sha.lower()
    return out


def _verify_subset_digests(
    task_dir: Path, task_id: str, expected_digests: dict[str, str]
) -> tuple[bool, str]:
    ok, reason, actual_files = _verify_regular_task_tree(
        task_dir, task_id, set(expected_digests)
    )
    if not ok:
        return False, reason
    if actual_files != set(expected_digests):
        missing = sorted(set(expected_digests) - actual_files)
        return False, f"file_set_mismatch({missing})"
    for rel_path, expected_sha in sorted(expected_digests.items()):
        if _sha256_file(task_dir / rel_path) != expected_sha.lower():
            return False, f"digest_mismatch({rel_path})"
    return True, "ok"


def _tx_lock_path(output_root: Path) -> Path:
    return output_root.parent / f".{output_root.name}{_LOCK_FILENAME_SUFFIX}"


def _tx_state_paths(output_root: Path) -> tuple[Path, Path]:
    return (
        output_root.parent / f".{output_root.name}{_TX_STATE_SUFFIX}",
        output_root.parent / f".{output_root.name}{_TX_STATE_TMP_SUFFIX}",
    )


def _expected_tx_marker_bytes(output_root: Path) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": _TX_SCHEMA_VERSION,
                "adapter": _MANIFEST_ADAPTER_ID,
                "output_root": str(output_root),
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _write_tx_marker(marker_path: Path, output_root: Path) -> None:
    payload = _expected_tx_marker_bytes(output_root)
    tmp_path = marker_path.parent / _TX_MARKER_TMP_FILENAME
    if tmp_path.is_symlink() or (tmp_path.exists() and not tmp_path.is_file()):
        raise FileExistsError(
            f"Invalid temporary transaction marker entry at {tmp_path}; preserving untouched"
        )
    if tmp_path.exists():
        try:
            existing = tmp_path.read_bytes()
        except OSError as exc:
            raise FileExistsError(
                f"Unreadable temporary transaction marker entry at {tmp_path}; preserving untouched"
            ) from exc
        if not payload.startswith(existing):
            raise FileExistsError(
                f"Unrecognized temporary marker file at {tmp_path}; preserving untouched"
            )
    _write_file_atomic_via_tmp(tmp_path, marker_path, payload, mode=0o644)


def _tx_state_prefix(output_root: Path) -> bytes:
    return (
        f'{{"adapter": {json.dumps(_MANIFEST_ADAPTER_ID)}, '
        f'"output_root": {json.dumps(str(output_root))}, '
        f'"schema_version": {_TX_SCHEMA_VERSION}, '
    ).encode("utf-8")


def _validate_tx_state_dict(
    data: object, output_root: Path, path: Path
) -> dict[str, Any]:
    if not isinstance(data, dict) or not (set(data) <= _TX_STATE_ALLOWED_KEYS):
        raise FileExistsError(
            f"Unowned or unrecognized transaction state file at {path}; preserving untouched"
        )
    if (
        not _is_exact_int(data.get("schema_version"), _TX_SCHEMA_VERSION)
        or data.get("adapter") != _MANIFEST_ADAPTER_ID
        or data.get("output_root") != str(output_root)
        or not isinstance(data.get("tx_state"), str)
        or data["tx_state"] not in {"init", "teardown"}
    ):
        raise FileExistsError(
            f"Unowned or unrecognized transaction state file at {path}; preserving untouched"
        )
    has_dev = "tx_dev" in data
    has_ino = "tx_ino" in data
    if has_dev != has_ino:
        raise FileExistsError(
            f"Unowned or unrecognized transaction state file at {path}; preserving untouched"
        )
    if has_dev and (
        isinstance(data["tx_dev"], bool)
        or not isinstance(data["tx_dev"], int)
        or isinstance(data["tx_ino"], bool)
        or not isinstance(data["tx_ino"], int)
    ):
        raise FileExistsError(
            f"Unowned or unrecognized transaction state file at {path}; preserving untouched"
        )
    if data["tx_state"] == "teardown" and not has_dev:
        raise FileExistsError(
            f"Unowned or unrecognized transaction state file at {path}; preserving untouched"
        )
    return data


def _validate_tx_state_tmp(tmp_path: Path, output_root: Path) -> None:
    if tmp_path.is_symlink() or (tmp_path.exists() and not tmp_path.is_file()):
        raise FileExistsError(
            f"Invalid temporary transaction state entry at {tmp_path}; preserving untouched"
        )
    if not tmp_path.exists():
        return
    try:
        with tmp_path.open("rb") as handle:
            raw = handle.read(4097)
    except OSError as exc:
        raise FileExistsError(
            f"Unreadable temporary transaction state file at {tmp_path}; preserving untouched"
        ) from exc
    if len(raw) > 4096:
        raise FileExistsError(
            f"Unowned oversized temporary file at {tmp_path}; preserving untouched"
        )
    try:
        raw_str = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FileExistsError(
            f"Unrecognized temporary transaction state file at {tmp_path}; preserving untouched"
        ) from exc
    if raw_str.endswith("\n"):
        try:
            _validate_tx_state_dict(json.loads(raw_str), output_root, tmp_path)
            return
        except json.JSONDecodeError as exc:
            raise FileExistsError(
                f"Unrecognized temporary transaction state file at {tmp_path}; preserving untouched"
            ) from exc
    prefix = _tx_state_prefix(output_root).decode("ascii")
    if len(raw_str) <= len(prefix):
        if prefix.startswith(raw_str):
            return
        raise FileExistsError(
            f"Unrecognized temporary transaction state file at {tmp_path}; preserving untouched"
        )
    if not raw_str.startswith(prefix):
        raise FileExistsError(
            f"Unrecognized temporary transaction state file at {tmp_path}; preserving untouched"
        )
    rest = raw_str[len(prefix) :]
    norm = re.sub(r"\d+", "<D>", rest)
    templates = (
        '"tx_state": "init"}\n',
        '"tx_dev": <D>, "tx_ino": <D>, "tx_state": "init"}\n',
        '"tx_dev": <D>, "tx_ino": <D>, "tx_state": "teardown"}\n',
    )
    if not any(tmpl.startswith(norm) for tmpl in templates):
        raise FileExistsError(
            f"Unrecognized temporary transaction state file at {tmp_path}; preserving untouched"
        )


def _read_tx_state_record(output_root: Path) -> dict[str, Any] | None:
    state_path, tmp_path = _tx_state_paths(output_root)
    _validate_tx_state_tmp(tmp_path, output_root)
    tmp_path.unlink(missing_ok=True)
    if state_path.is_symlink() or (state_path.exists() and not state_path.is_file()):
        raise FileExistsError(
            f"Invalid transaction state path at {state_path}; preserving untouched"
        )
    if not state_path.exists():
        return None
    try:
        with state_path.open("rb") as handle:
            raw = handle.read(4097)
    except OSError as exc:
        raise FileExistsError(
            f"Unreadable transaction state file at {state_path}; preserving untouched"
        ) from exc
    if len(raw) > 4096 or not raw:
        raise FileExistsError(
            f"Unowned or invalid transaction state file at {state_path}; preserving untouched"
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FileExistsError(
            f"Corrupt transaction state file at {state_path}; preserving untouched"
        ) from exc
    return _validate_tx_state_dict(data, output_root, state_path)


def _write_tx_state_record(
    output_root: Path,
    *,
    tx_state: str,
    tx_dev: int | None = None,
    tx_ino: int | None = None,
) -> None:
    _read_tx_state_record(output_root)
    state_path, tmp_path = _tx_state_paths(output_root)
    record: dict[str, Any] = {
        "schema_version": _TX_SCHEMA_VERSION,
        "adapter": _MANIFEST_ADAPTER_ID,
        "output_root": str(output_root),
        "tx_state": tx_state,
    }
    if tx_dev is not None and tx_ino is not None:
        record["tx_dev"] = tx_dev
        record["tx_ino"] = tx_ino
    payload = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    _write_file_atomic_via_tmp(tmp_path, state_path, payload, mode=0o644)


def _clear_tx_state_record(output_root: Path) -> None:
    _read_tx_state_record(output_root)
    state_path, tmp_path = _tx_state_paths(output_root)
    tmp_path.unlink(missing_ok=True)
    state_path.unlink(missing_ok=True)


def _tx_root_is_recoverable_markerless(tx_root: Path, output_root: Path) -> bool:
    entries = list(tx_root.iterdir())
    if not entries:
        return True
    if (
        len(entries) == 1
        and entries[0].name == _TX_MARKER_TMP_FILENAME
        and not entries[0].is_symlink()
        and entries[0].is_file()
    ):
        try:
            existing = entries[0].read_bytes()
        except OSError:
            return False
        return _expected_tx_marker_bytes(output_root).startswith(existing)
    return False


def _ensure_owned_tx_root(tx_root: Path, output_root: Path) -> None:
    state_record = _read_tx_state_record(output_root)
    if tx_root.is_symlink() or (tx_root.exists() and not tx_root.is_dir()):
        raise FileExistsError(
            f"Transaction path exists and is not a regular directory: {tx_root}"
        )
    marker_path = tx_root / _TX_MARKER_FILENAME
    if tx_root.exists():
        if marker_path.is_symlink() or not marker_path.is_file():
            try:
                st = os.lstat(tx_root)
            except OSError:
                st = None
            state_matches = (
                state_record is not None
                and st is not None
                and stat.S_ISDIR(st.st_mode)
                and (
                    (
                        state_record["tx_state"] in {"init", "teardown"}
                        and "tx_dev" in state_record
                        and _is_exact_int(state_record["tx_dev"], st.st_dev)
                        and _is_exact_int(state_record["tx_ino"], st.st_ino)
                    )
                    or (state_record["tx_state"] == "init" and "tx_dev" not in state_record)
                )
            )
            if (
                not marker_path.is_symlink()
                and state_matches
                and _tx_root_is_recoverable_markerless(tx_root, output_root)
            ):
                assert st is not None and state_record is not None
                _write_tx_state_record(
                    output_root,
                    tx_state=state_record["tx_state"],
                    tx_dev=st.st_dev,
                    tx_ino=st.st_ino,
                )
                _write_tx_marker(marker_path, output_root)
                _clear_tx_state_record(output_root)
                return
            raise FileExistsError(
                f"Unowned directory at transaction path {tx_root} (missing {_TX_MARKER_FILENAME}); preserving untouched"
            )
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FileExistsError(
                f"Corrupt transaction marker at {marker_path}; preserving {tx_root} untouched"
            ) from exc
        if (
            not isinstance(marker, dict)
            or not _is_exact_int(marker.get("schema_version"), _TX_SCHEMA_VERSION)
            or marker.get("adapter") != _MANIFEST_ADAPTER_ID
            or marker.get("output_root") != str(output_root)
        ):
            raise FileExistsError(
                f"Transaction marker mismatch at {marker_path}; preserving {tx_root} untouched"
            )
        for item in sorted(tx_root.iterdir()):
            if item.is_symlink() or item.name not in _TX_ALLOWED_CHILDREN:
                raise FileExistsError(
                    f"Unexpected entry inside transaction directory ({item}); preserving {tx_root} untouched"
                )
            if item.name != _TX_MARKER_FILENAME and not item.is_dir():
                raise FileExistsError(
                    f"Invalid transaction subdirectory ({item}); preserving {tx_root} untouched"
                )
        _clear_tx_state_record(output_root)
    else:
        _write_tx_state_record(output_root, tx_state="init")
        tx_root.mkdir(parents=True, exist_ok=True)
        st = os.lstat(tx_root)
        _write_tx_state_record(
            output_root,
            tx_state="init",
            tx_dev=st.st_dev,
            tx_ino=st.st_ino,
        )
        _write_tx_marker(marker_path, output_root)
        _clear_tx_state_record(output_root)


def _write_tx_journal(
    journal_root: Path,
    output_root: Path,
    *,
    op: str,
    task_id: str,
    previous_files: dict[str, str] | None,
    replacement_files: dict[str, str] | None,
    tmp_root: Path | None = None,
) -> Path:
    journal_path = journal_root / f"{task_id}.json"
    payload = {
        "schema_version": _TX_SCHEMA_VERSION,
        "adapter": _MANIFEST_ADAPTER_ID,
        "output_root": str(output_root),
        "op": op,
        "task_id": task_id,
        "staging_name": task_id,
        "backup_name": task_id,
        "previous_files": previous_files,
        "replacement_files": replacement_files,
    }
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if tmp_root is not None:
        _write_file_atomic_via_tmp(tmp_root / f"{task_id}.journal.tmp", journal_path, encoded, mode=0o644)
    else:
        _atomic_write_bytes(journal_path, encoded, mode=0o644)
    return journal_path


def _cleanup_empty_tx_root(tx_root: Path, output_root: Path) -> None:
    if tx_root.is_symlink() or not tx_root.is_dir():
        return
    marker_path = tx_root / _TX_MARKER_FILENAME
    if marker_path.is_symlink() or not marker_path.is_file():
        return
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if (
        not isinstance(marker, dict)
        or not _is_exact_int(marker.get("schema_version"), _TX_SCHEMA_VERSION)
        or marker.get("adapter") != _MANIFEST_ADAPTER_ID
        or marker.get("output_root") != str(output_root)
    ):
        return
    for name in ("staging", "backups", "journal", "tmp"):
        sub = tx_root / name
        if sub.is_symlink():
            return
        if sub.is_dir():
            try:
                if any(sub.iterdir()):
                    return
            except OSError:
                return
        elif sub.exists():
            return
    for item in tx_root.iterdir():
        if item.name not in _TX_ALLOWED_CHILDREN or item.is_symlink():
            return
    for name in ("staging", "backups", "journal", "tmp"):
        sub = tx_root / name
        if sub.is_dir():
            try:
                sub.rmdir()
            except OSError:
                return
    st = os.lstat(tx_root)
    _write_tx_state_record(
        output_root,
        tx_state="teardown",
        tx_dev=st.st_dev,
        tx_ino=st.st_ino,
    )
    marker_path.unlink(missing_ok=True)
    try:
        tx_root.rmdir()
    except OSError:
        return
    _clear_tx_state_record(output_root)


def _read_bounded_bytes(path: Path, max_bytes: int) -> bytes | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return None
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    return raw


def _is_owned_tmp_json_prefix(
    raw: bytes,
    *,
    required_header: bytes,
    allowed_top_keys: frozenset[str],
    validator: Any,
) -> bool:
    if not raw:
        return True
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return False
    if len(raw) <= len(required_header):
        return required_header.startswith(raw)
    if not raw.startswith(required_header):
        return False

    depth = 0
    in_string = False
    escape = False
    closed_at_end = False
    for ch in text:
        if closed_at_end:
            if ch not in " \t\r\n":
                return False
            continue
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    return False
                if depth == 0:
                    closed_at_end = True

    if depth == 0:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return False
        if not isinstance(data, dict) or not (set(data) <= allowed_top_keys) or not bool(validator(data)):
            return False
        expected_full = json.dumps(data, indent=2, sort_keys=True) + "\n"
        return text in (expected_full, expected_full[:-1])

    candidates: list[tuple[str, bool]] = []
    if in_string:
        base = text[:-1] if escape else text
        base = re.sub(r"\\u[0-9a-fA-F]{0,3}$", "", base)
        candidates.append((base + '"' + ("}" * depth), False))
        candidates.append((base + '": null' + ("}" * depth), depth == 1))
    else:
        trimmed = text.rstrip()
        if trimmed.endswith(","):
            candidates.append((trimmed[:-1] + ("}" * depth), False))
        elif trimmed.endswith(":"):
            candidates.append((trimmed + " null" + ("}" * depth), False))
        elif trimmed.endswith('"'):
            candidates.append((trimmed + ("}" * depth), False))
            candidates.append((trimmed + ": null" + ("}" * depth), depth == 1))
        else:
            m_null = re.search(r":\s*(n|nu|nul)$", trimmed)
            if m_null is not None:
                candidates.append((trimmed[: m_null.start(1)] + "null" + ("}" * depth), False))
            else:
                candidates.append((trimmed + ("}" * depth), False))

    for cand, allow_last_key_prefix in candidates:
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict) or parsed.get("adapter") != _MANIFEST_ADAPTER_ID:
            continue
        keys = list(parsed.keys())
        if set(keys) <= allowed_top_keys:
            return True
        if (
            allow_last_key_prefix
            and keys
            and set(keys[:-1]) <= allowed_top_keys
            and any(ak.startswith(keys[-1]) for ak in allowed_top_keys)
        ):
            return True
    return False


def _is_owned_stage_tmp(
    raw: bytes,
    repl_digests: set[str],
    license_bytes: bytes | None,
) -> bool:
    if not raw:
        return True
    if hashlib.sha256(raw).hexdigest() in repl_digests:
        return True
    prefixes = [
        b'version = "1.0"\n',
        SYSTEM_PROMPT.encode("utf-8"),
        b"FROM python:3.11-slim",
        b"#!/bin/bash\n",
        b'"""Deterministic Spearman',
        b'{\n  "assay": ',
        b'{"assay": ',
    ]
    if license_bytes is not None:
        prefixes.append(license_bytes)
    for pref in prefixes:
        if len(raw) <= len(pref):
            if pref.startswith(raw):
                return True
        elif raw.startswith(pref):
            for trim in range(4):
                chunk = raw[: len(raw) - trim] if trim else raw
                try:
                    chunk.decode("utf-8")
                    return True
                except UnicodeDecodeError:
                    continue
            return False
    return False


def _recover_interrupted_tx(
    output_root: Path,
    tx_root: Path,
    manifest_tasks: dict[str, dict[str, Any]],
    *,
    license_bytes: bytes | None = None,
    root_license_sha256: str | None = None,
) -> None:
    journal_root = tx_root / "journal"
    staging_root = tx_root / "staging"
    backups_root = tx_root / "backups"
    tmp_root = tx_root / "tmp"

    journals: dict[str, dict[str, Any]] = {}
    if journal_root.exists():
        for entry in sorted(journal_root.iterdir()):
            if entry.is_symlink() or not entry.is_file() or not entry.name.endswith(".json"):
                raise RuntimeError(f"Invalid journal entry inside {journal_root}: {entry}")
            tid = entry.name[: -len(".json")]
            if not _PG_LLM_TASK_ID_RE.fullmatch(tid):
                raise RuntimeError(f"Unexpected journal task ID {tid!r} in {journal_root}")
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Corrupt transaction journal {entry}: {exc}") from exc
            if (
                not isinstance(data, dict)
                or not _is_exact_int(data.get("schema_version"), _TX_SCHEMA_VERSION)
                or data.get("adapter") != _MANIFEST_ADAPTER_ID
                or data.get("output_root") != str(output_root)
                or data.get("task_id") != tid
                or not isinstance(data.get("op"), str)
                or data.get("op") not in {"replace", "prune"}
                or data.get("staging_name") != tid
                or data.get("backup_name") != tid
            ):
                raise RuntimeError(f"Invalid transaction journal schema in {entry}")
            journals[tid] = data

    journaled_staging = {
        data["staging_name"] for data in journals.values() if data["op"] == "replace"
    }
    journaled_backups = {data["backup_name"] for data in journals.values()}

    if staging_root.exists():
        for entry in sorted(staging_root.iterdir()):
            if entry.is_symlink() or not entry.is_dir() or entry.name not in journaled_staging:
                raise RuntimeError(
                    f"Unjournaled entry inside staging directory ({entry}); preserving untouched"
                )

    if backups_root.exists():
        for entry in sorted(backups_root.iterdir()):
            if entry.is_symlink() or not entry.is_dir() or entry.name not in journaled_backups:
                raise RuntimeError(
                    f"Unjournaled entry inside backup directory ({entry}); preserving untouched"
                )

    cleanup_tmp_files: list[Path] = []
    pending_stage_tmps: dict[str, tuple[Path, bytes]] = {}
    if tmp_root.exists():
        if tmp_root.is_symlink() or not tmp_root.is_dir():
            raise RuntimeError(f"Invalid transaction temp directory at {tmp_root}")
        manifest_hdr = f'{{\n  "adapter": {json.dumps(_MANIFEST_ADAPTER_ID)},\n'.encode("utf-8")
        for entry in sorted(tmp_root.iterdir()):
            if entry.is_symlink() or not entry.is_file():
                raise RuntimeError(f"Invalid temp entry inside {tmp_root}: {entry}")
            if entry.name == _MANIFEST_TMP_NAME:
                raw_tmp = _read_bounded_bytes(entry, 16 * 1024 * 1024)
                if raw_tmp is None or not _is_owned_tmp_json_prefix(
                    raw_tmp,
                    required_header=manifest_hdr,
                    allowed_top_keys=_MANIFEST_TMP_ALLOWED_KEYS,
                    validator=lambda d: (
                        isinstance(d, dict)
                        and _is_exact_int(d.get("schema_version"), _MANIFEST_SCHEMA_VERSION)
                        and d.get("adapter") == _MANIFEST_ADAPTER_ID
                        and isinstance(d.get("tasks"), dict)
                    ),
                ):
                    raise RuntimeError(
                        f"Unrecognized temp manifest at {entry}; preserving {tx_root}"
                    )
                cleanup_tmp_files.append(entry)
            elif entry.name == _LICENSE_TMP_NAME:
                if license_bytes is None:
                    raise RuntimeError(
                        f"Unrecognized temp license file at {entry}; preserving {tx_root}"
                    )
                raw_tmp = _read_bounded_bytes(entry, len(license_bytes))
                if raw_tmp is None or not license_bytes.startswith(raw_tmp):
                    raise RuntimeError(
                        f"Unrecognized temp license file at {entry}; preserving {tx_root}"
                    )
                cleanup_tmp_files.append(entry)
            elif entry.name.endswith(".journal.tmp"):
                tid = entry.name[: -len(".journal.tmp")]
                if not _PG_LLM_TASK_ID_RE.fullmatch(tid):
                    raise RuntimeError(f"Unexpected journal temp {entry}; preserving {tx_root}")
                raw_tmp = _read_bounded_bytes(entry, 64 * 1024)
                j_hdr = (
                    f'{{\n  "adapter": {json.dumps(_MANIFEST_ADAPTER_ID)},\n'
                    f'  "backup_name": {json.dumps(tid)},\n'
                ).encode("utf-8")
                if raw_tmp is None or not _is_owned_tmp_json_prefix(
                    raw_tmp,
                    required_header=j_hdr,
                    allowed_top_keys=_JOURNAL_TMP_ALLOWED_KEYS,
                    validator=lambda d, _tid=tid: (
                        isinstance(d, dict)
                        and _is_exact_int(d.get("schema_version"), _TX_SCHEMA_VERSION)
                        and d.get("adapter") == _MANIFEST_ADAPTER_ID
                        and d.get("output_root") == str(output_root)
                        and d.get("task_id") == _tid
                    ),
                ):
                    raise RuntimeError(
                        f"Unrecognized temp journal at {entry}; preserving {tx_root}"
                    )
                cleanup_tmp_files.append(entry)
            elif entry.name.endswith(".stage.tmp"):
                tid = entry.name[: -len(".stage.tmp")]
                if not _PG_LLM_TASK_ID_RE.fullmatch(tid) or tid not in journaled_staging:
                    raise RuntimeError(
                        f"Unjournaled staging temp file {entry}; preserving {tx_root}"
                    )
                raw_tmp = _read_bounded_bytes(entry, 4 * 1024 * 1024)
                if raw_tmp is None:
                    raise RuntimeError(
                        f"Oversized or unreadable staging temp file {entry}; preserving {tx_root}"
                    )
                pending_stage_tmps[tid] = (entry, raw_tmp)
            else:
                raise RuntimeError(f"Unexpected entry inside {tmp_root}: {entry}")

    for tmp_file in cleanup_tmp_files:
        tmp_file.unlink(missing_ok=True)

    required_set = set(_REQUIRED_TASK_FILES)
    for tid, journal in sorted(journals.items()):
        journal_path = journal_root / f"{tid}.json"
        staging_dir = staging_root / tid
        backup_dir = backups_root / tid
        stage_tmp_info = pending_stage_tmps.get(tid)
        stage_tmp: Path | None = None
        target = output_root / tid
        op = journal["op"]

        if op == "replace":
            repl_files = _validate_digest_map(journal.get("replacement_files"))
            if repl_files is None or set(repl_files) != required_set:
                raise RuntimeError(
                    f"Invalid replacement digest map in {journal_path}; preserving {tx_root}"
                )
            if stage_tmp_info is not None:
                stage_tmp, stage_raw = stage_tmp_info
                if not _is_owned_stage_tmp(stage_raw, set(repl_files.values()), license_bytes):
                    raise RuntimeError(
                        f"Unrecognized staging temp file {stage_tmp}; preserving {tx_root}"
                    )
            raw_prev = journal.get("previous_files")
            prev_files: dict[str, str] | None = None
            if raw_prev is not None:
                prev_files = _validate_digest_map(raw_prev)
                if prev_files is None or not prev_files:
                    raise RuntimeError(
                        f"Invalid previous digest map in {journal_path}; preserving {tx_root}"
                    )

            if staging_dir.exists():
                ok, reason = _verify_partial_deletable_tree(staging_dir, tid, repl_files)
                if not ok:
                    raise RuntimeError(
                        f"Staging directory {staging_dir} failed integrity check ({reason}); preserving untouched"
                    )

            if backup_dir.exists():
                if not target.exists() and not target.is_symlink():
                    if prev_files is None or not prev_files:
                        raise RuntimeError(
                            f"Journal {journal_path} lacks previous file digests to verify backup {backup_dir}"
                        )
                    ok, reason = _verify_subset_digests(backup_dir, tid, prev_files)
                    if not ok:
                        raise RuntimeError(
                            f"Backup {backup_dir} failed integrity check ({reason}); preserving backup"
                        )
                    if staging_dir.exists():
                        shutil.rmtree(staging_dir)
                    if stage_tmp is not None:
                        stage_tmp.unlink(missing_ok=True)
                    output_root.mkdir(parents=True, exist_ok=True)
                    backup_dir.rename(target)
                    if set(prev_files) == required_set:
                        manifest_tasks[tid] = {
                            "task_name": f"{_PG_LLM_TASK_NAME_PREFIX}{tid}",
                            "files": prev_files,
                        }
                        _write_output_manifest(
                            output_root,
                            manifest_tasks,
                            root_license_sha256=root_license_sha256,
                            tmp_root=tmp_root,
                        )
                    journal_path.unlink(missing_ok=True)
                else:
                    ok, reason = _verify_task_dir_digests(
                        target, tid, repl_files, check_exec=True
                    )
                    if not ok:
                        raise RuntimeError(
                            f"Interrupted replacement at {target} failed verification ({reason}); preserving backup at {backup_dir}"
                        )
                    if prev_files is None:
                        raise RuntimeError(
                            f"Journal {journal_path} lacks previous file digests to verify backup {backup_dir}"
                        )
                    bak_ok, bak_reason = _verify_partial_deletable_tree(
                        backup_dir, tid, prev_files
                    )
                    if not bak_ok:
                        raise RuntimeError(
                            f"Backup {backup_dir} contains unverified contents ({bak_reason}); preserving backup"
                        )
                    manifest_tasks[tid] = {
                        "task_name": f"{_PG_LLM_TASK_NAME_PREFIX}{tid}",
                        "files": repl_files,
                    }
                    _write_output_manifest(
                        output_root,
                        manifest_tasks,
                        root_license_sha256=root_license_sha256,
                        tmp_root=tmp_root,
                    )
                    if staging_dir.exists():
                        shutil.rmtree(staging_dir)
                    if stage_tmp is not None:
                        stage_tmp.unlink(missing_ok=True)
                    shutil.rmtree(backup_dir)
                    journal_path.unlink(missing_ok=True)
            else:
                target_is_repl = False
                if target.exists() or target.is_symlink():
                    if target.is_symlink():
                        raise RuntimeError(
                            f"Destination {target} is a symlink and does not match journal {journal_path}"
                        )
                    target_is_repl = _verify_task_dir_digests(
                        target, tid, repl_files, check_exec=True
                    )[0]
                    if not target_is_repl and not (
                        prev_files is not None
                        and _verify_subset_digests(target, tid, prev_files)[0]
                    ):
                        raise RuntimeError(
                            f"Destination {target} does not match journal {journal_path}"
                        )
                elif prev_files is not None:
                    raise RuntimeError(
                        f"Replace journal {journal_path} records previous files but both {target} and backup {backup_dir} are missing; preserving {tx_root}"
                    )
                elif tid in manifest_tasks:
                    raise RuntimeError(
                        f"Replace journal {journal_path} inconsistent: manifest records {tid} yet destination {target} and backup {backup_dir} are missing; preserving {tx_root}"
                    )
                if target_is_repl:
                    manifest_tasks[tid] = {
                        "task_name": f"{_PG_LLM_TASK_NAME_PREFIX}{tid}",
                        "files": repl_files,
                    }
                    _write_output_manifest(
                        output_root,
                        manifest_tasks,
                        root_license_sha256=root_license_sha256,
                        tmp_root=tmp_root,
                    )
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)
                if stage_tmp is not None:
                    stage_tmp.unlink(missing_ok=True)
                journal_path.unlink(missing_ok=True)
        else:  # op == "prune"
            prev_files = _validate_digest_map(journal.get("previous_files"))
            if prev_files is None or set(prev_files) != required_set:
                raise RuntimeError(
                    f"Invalid prune digest map in {journal_path}; preserving {tx_root}"
                )
            if backup_dir.exists():
                if tid in manifest_tasks:
                    if manifest_tasks[tid].get("files") != prev_files:
                        raise RuntimeError(
                            f"Prune journal {journal_path} disagrees with manifest digests for {tid}; preserving backup {backup_dir}"
                        )
                    ok, reason = _verify_task_dir_digests(
                        backup_dir, tid, prev_files, check_exec=False
                    )
                    if not ok:
                        raise RuntimeError(
                            f"Prune backup {backup_dir} failed integrity check ({reason}); preserving backup"
                        )
                    if target.exists() or target.is_symlink():
                        raise RuntimeError(
                            f"Cannot restore prune backup {backup_dir}; {target} already exists"
                        )
                    backup_dir.rename(target)
                else:
                    if target.exists() or target.is_symlink():
                        raise RuntimeError(
                            f"Prune journal {journal_path} inconsistent: {tid} absent from manifest yet {target} exists; preserving backup {backup_dir}"
                        )
                    ok, reason = _verify_partial_deletable_tree(
                        backup_dir, tid, prev_files
                    )
                    if not ok:
                        raise RuntimeError(
                            f"Prune backup {backup_dir} contains unverified contents ({reason}); preserving backup"
                        )
                    shutil.rmtree(backup_dir)
                journal_path.unlink(missing_ok=True)
            else:
                if tid in manifest_tasks:
                    if manifest_tasks[tid].get("files") != prev_files:
                        raise RuntimeError(
                            f"Prune journal {journal_path} disagrees with manifest digests for {tid}"
                        )
                    if target.is_symlink() or not target.exists():
                        raise RuntimeError(
                            f"Prune journal {journal_path} inconsistent: manifest contains {tid} but directory is missing or symlink"
                        )
                    ok, reason = _verify_task_dir_digests(
                        target, tid, prev_files, check_exec=False
                    )
                    if not ok:
                        raise RuntimeError(
                            f"Prune target {target} failed integrity check ({reason})"
                        )
                elif target.exists() or target.is_symlink():
                    raise RuntimeError(
                        f"Prune journal {journal_path} inconsistent: {tid} absent from manifest yet {target} exists"
                    )
                journal_path.unlink(missing_ok=True)


def _inspect_existing_task(
    task_dir: Path,
    task_id: str,
    expected_files: dict[str, bytes],
    recorded_files: dict[str, str] | None = None,
) -> str:
    if not task_dir.exists() and not task_dir.is_symlink():
        return "missing"
    if task_dir.is_symlink() or not task_dir.is_dir():
        return "foreign:not_a_real_directory"

    ok, reason, _ = _verify_regular_task_tree(task_dir, task_id, set(expected_files))
    if not ok:
        return f"foreign:{reason}"

    content_diffs: list[str] = []
    exec_diffs: list[str] = []
    surviving_matches_recorded = recorded_files is not None
    for rel_path, expected_bytes in expected_files.items():
        candidate = task_dir / rel_path
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink() or not candidate.is_file():
                return f"foreign:extra_entry({rel_path})"
            raw = candidate.read_bytes()
            if recorded_files is None or _sha256_bytes(raw) != recorded_files.get(rel_path):
                surviving_matches_recorded = False
            if raw != expected_bytes:
                content_diffs.append(rel_path)
            elif rel_path in _EXECUTABLE_REL_PATHS and not (
                candidate.stat().st_mode & 0o100
            ):
                exec_diffs.append(rel_path)

    if not content_diffs and not exec_diffs:
        if all((task_dir / rel_path).is_file() for rel_path in _REQUIRED_TASK_FILES):
            return "exact"
        if surviving_matches_recorded:
            return "incomplete"
        return "foreign:unrecorded_incomplete"

    if recorded_files is not None and _verify_task_dir_digests(
        task_dir, task_id, recorded_files, check_exec=False
    )[0]:
        diff_label = (
            content_diffs[0] if content_diffs else f"non_executable({exec_diffs[0]})"
        )
        return f"mismatch:{diff_label}"

    diff_sample = content_diffs[0] if content_diffs else f"non_executable({exec_diffs[0]})"
    return f"foreign:modified_or_unrecorded({diff_sample})"


class PgLlmAdapter:
    """Convert PG-LLM frozen ProteinGym evaluation tasks into Harbor task directories."""

    def __init__(
        self,
        output_dir: Path,
        *,
        data_dir: Path | None = None,
        size: int = 50,
        seeds: Iterable[int] = (1,),
        split: str = "all",
        network_mode: str = "public",
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: Iterable[str] | None = None,
        templates_dir: Path | None = None,
    ) -> None:
        if size not in EVAL_SIZES:
            raise ValueError(f"size must be one of {EVAL_SIZES}, got {size!r}")
        self.seeds = tuple(seeds)
        if not self.seeds or any(seed not in EVAL_SEEDS for seed in self.seeds):
            raise ValueError(f"seeds must be a non-empty subset of {EVAL_SEEDS}, got {self.seeds!r}")
        if split not in {"all", "parity"}:
            raise ValueError(f"split must be 'all' or 'parity', got {split!r}")
        if network_mode not in NETWORK_MODES:
            raise ValueError(
                f"network_mode must be one of {NETWORK_MODES}, got {network_mode!r}"
            )

        self.output_dir = Path(output_dir)
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.size = size
        self.split = split
        self.network_mode = network_mode
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = set(task_ids) if task_ids else None
        self.templates_dir = (
            Path(templates_dir)
            if templates_dir is not None
            else Path(__file__).parent / "task-template"
        )
        if not self.templates_dir.is_dir():
            raise FileNotFoundError(f"Template directory not found: {self.templates_dir}")

    def _validate_non_overlapping_paths(
        self,
        targets: list[Path],
        paths_with_labels: list[tuple[str, Path]],
    ) -> None:
        for base in targets:
            for label, candidate in paths_with_labels:
                if _paths_overlap(base, candidate):
                    raise ValueError(
                        f"Output path {base} overlaps {label} {candidate}"
                    )

    def run(self) -> list[Path]:
        _reject_symlink_path(self.output_dir, "Output path", exc_type=NotADirectoryError)
        if self.output_dir.exists() and not self.output_dir.is_dir():
            raise NotADirectoryError(f"Output path is not a directory: {self.output_dir}")
        if self.data_dir is not None:
            _reject_symlink_path(self.data_dir, "Data directory")
        output_root = self.output_dir.resolve(strict=False)
        if output_root == output_root.parent:
            raise ValueError(f"Output directory cannot be filesystem root: {output_root}")
        tx_root = output_root.parent / f".{output_root.name}{_TX_DIR_SUFFIX}"
        tx_state_path, tx_state_tmp = _tx_state_paths(output_root)
        lock_path = output_root.parent / f".{output_root.name}{_LOCK_FILENAME_SUFFIX}"
        _reject_symlink_path(tx_root, "Transaction path", exc_type=FileExistsError)
        _reject_symlink_path(tx_state_path, "Transaction state path", exc_type=FileExistsError)
        _reject_symlink_path(tx_state_tmp, "Transaction state temp path", exc_type=FileExistsError)
        _reject_symlink_path(lock_path, "Lock path", exc_type=FileExistsError)

        data_root, cache_root, archive_path = _resolve_eval_data_paths(self.data_dir)
        preflight_paths: list[tuple[str, Path]] = [
            ("template directory", self.templates_dir),
            ("data directory", data_root),
            ("resolved data directory", data_root.resolve(strict=False)),
        ]
        if cache_root is not None:
            preflight_paths.append(("cache directory", cache_root))
            preflight_paths.append(("resolved cache directory", cache_root.resolve(strict=False)))
        if archive_path is not None:
            preflight_paths.append(("archive path", archive_path))
            preflight_paths.append(("resolved archive path", archive_path.resolve(strict=False)))
            preflight_paths.append(("archive parent directory", archive_path.parent))
        self._validate_non_overlapping_paths(
            [output_root, tx_root, lock_path, tx_state_path, tx_state_tmp],
            preflight_paths,
        )

        # Carry un-resolved cache paths so archive/cache symlink checks reject symlink referents
        # before any download or os.replace can touch a referent file.
        resolved_data_root = ensure_eval_data(
            self.data_dir,
            cache_root=cache_root,
            data_root=data_root,
            archive_path=archive_path,
        )
        self._validate_non_overlapping_paths(
            [output_root, tx_root, lock_path],
            [("resolved data directory", resolved_data_root)],
        )

        meta = _load_assay_meta(resolved_data_root)

        if self.split == "parity":
            assay_ids = _select_parity_assays(meta)
        else:
            assay_ids = sorted(meta)

        episodes: list[tuple[str, int, str]] = []
        seen_task_ids: set[str] = set()
        matched_task_ids: set[str] = set()
        for seed in self.seeds:
            for assay_id in assay_ids:
                task_id = sanitize_task_id(assay_id, self.size, seed)
                if task_id in seen_task_ids:
                    raise ValueError(f"Sanitized task ID collision: {task_id!r}")
                seen_task_ids.add(task_id)
                if self.task_ids is not None:
                    matched = False
                    if task_id in self.task_ids:
                        matched_task_ids.add(task_id)
                        matched = True
                    if assay_id in self.task_ids:
                        matched_task_ids.add(assay_id)
                        matched = True
                    if not matched:
                        continue
                episodes.append((assay_id, seed, task_id))

        if self.task_ids is not None:
            unmatched = self.task_ids - matched_task_ids
            if unmatched:
                raise ValueError(f"Unknown --task-ids requested: {sorted(unmatched)}")

        if self.limit is not None:
            if self.limit < 0:
                raise ValueError(f"limit must be non-negative, got {self.limit}")
            episodes = episodes[: self.limit]

        expected_task_ids = {task_id for _, _, task_id in episodes}

        task_toml_tpl = (self.templates_dir / "task.toml").read_text(encoding="utf-8")
        instruction_tpl = (self.templates_dir / "instruction.md").read_text(encoding="utf-8")
        solve_tpl = (self.templates_dir / "solution" / "solve.sh").read_text(encoding="utf-8")
        dockerfile_bytes = (self.templates_dir / "environment" / "Dockerfile").read_bytes()
        test_sh_bytes = (self.templates_dir / "tests" / "test.sh").read_bytes()
        grade_py_bytes = (self.templates_dir / "tests" / "grade.py").read_bytes()
        license_bytes = (self.templates_dir / "tests" / "THIRD_PARTY_LICENSES.md").read_bytes()
        expected_license_sha = _sha256_bytes(license_bytes)

        output_root.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
            raise FileExistsError(f"Lock path exists and is not a regular file: {lock_path}")
        lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(lock_path, lock_flags, 0o644)
        except OSError as exc:
            raise FileExistsError(
                f"Lock path exists and is not a regular file: {lock_path}"
            ) from exc
        lock_acquired = False
        tx_owned = False

        try:
            fst = os.fstat(lock_fd)
            if not stat.S_ISREG(fst.st_mode):
                raise FileExistsError(f"Lock path is not a regular file: {lock_path}")
            lst = os.lstat(lock_path)
            if fst.st_dev != lst.st_dev or fst.st_ino != lst.st_ino:
                raise FileExistsError(f"Lock path changed during open: {lock_path}")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(f"Another pg-llm process holds generation lock {lock_path}") from exc
            lock_acquired = True
            if os.fstat(lock_fd).st_size != 0:
                raise FileExistsError(
                    f"Unowned non-empty file at lock path {lock_path}; preserving untouched"
                )

            _ensure_owned_tx_root(tx_root, output_root)
            tx_owned = True
            if output_root.exists():
                manifest_tasks, recorded_license_sha = _load_output_manifest_state(output_root)
            else:
                manifest_tasks, recorded_license_sha = {}, None
            _recover_interrupted_tx(
                output_root,
                tx_root,
                manifest_tasks,
                license_bytes=license_bytes,
                root_license_sha256=recorded_license_sha,
            )

            unexpected_owned: list[Path] = []
            manifest_only_stale = sorted(
                tid
                for tid in manifest_tasks
                if tid not in expected_task_ids
                and not (output_root / tid).exists()
                and not (output_root / tid).is_symlink()
            )
            root_license_needs_write = True
            root_license_owned_stale = False
            effective_license_sha = recorded_license_sha
            manifest_dirty = not (output_root / _MANIFEST_FILENAME).is_file()

            if output_root.exists():
                unexpected_foreign: list[str] = []
                for entry in sorted(output_root.iterdir()):
                    if entry.name in _ALLOWED_ROOT_FILES:
                        if entry.is_symlink() or not entry.is_file():
                            unexpected_foreign.append(f"{entry} (invalid root metadata entry)")
                        elif entry.name == "THIRD_PARTY_LICENSES.md":
                            actual_lic_sha = _sha256_file(entry)
                            if actual_lic_sha == expected_license_sha:
                                root_license_needs_write = False
                                effective_license_sha = expected_license_sha
                                if recorded_license_sha != expected_license_sha:
                                    manifest_dirty = True
                            elif recorded_license_sha is not None and actual_lic_sha == recorded_license_sha:
                                root_license_owned_stale = True
                                root_license_needs_write = True
                            else:
                                unexpected_foreign.append(
                                    f"{entry} (modified or unrecorded root license file)"
                                )
                        continue
                    if entry.is_symlink() or not entry.is_dir():
                        unexpected_foreign.append(f"{entry} (non-directory/symlink root entry)")
                        continue
                    if entry.name in expected_task_ids:
                        continue
                    ok, reason = _verify_prunable_owned_task(entry, manifest_tasks)
                    if ok:
                        unexpected_owned.append(entry)
                    else:
                        unexpected_foreign.append(f"{entry} ({reason})")

                if unexpected_foreign:
                    raise FileExistsError(
                        f"Output directory {output_root} contains {len(unexpected_foreign)} non-PG-LLM or "
                        f"unverified entry(s) ({'; '.join(unexpected_foreign[:3])}); refusing to overwrite or prune."
                    )
                if unexpected_owned and not self.overwrite:
                    raise FileExistsError(
                        f"Output directory {output_root} contains {len(unexpected_owned)} unexpected "
                        f"PG-LLM task directory(s) not in the current selection (e.g. {unexpected_owned[0].name!r}). "
                        "Pass --overwrite to prune stale tasks or choose a fresh --output-dir."
                    )
                if manifest_only_stale and not self.overwrite:
                    raise FileExistsError(
                        f"Output manifest contains {len(manifest_only_stale)} stale record(s) absent from disk "
                        f"(e.g. {manifest_only_stale[0]!r}). Pass --overwrite to prune stale records or choose a fresh --output-dir."
                    )
                if root_license_owned_stale and not self.overwrite:
                    raise FileExistsError(
                        f"Existing root {output_root / 'THIRD_PARTY_LICENSES.md'} differs from requested output. "
                        "Pass --overwrite to replace or choose a fresh --output-dir."
                    )

            planned: list[tuple[str, Path, str, dict[str, bytes], dict[str, str]]] = []
            for assay_id, seed, task_id in episodes:
                info = meta[assay_id]
                mutants = _load_split_episode(resolved_data_root, assay_id, self.size, seed)
                raw_user_prompt, ids = build_user_prompt(info, info["reference_sequence"], mutants)
                adapted_user_prompt = _adapt_prompt_for_file_submission(raw_user_prompt)

                # Descending DMS fitness; tie-break deterministically by variant ID.
                ordered_indices = sorted(
                    range(len(ids)), key=lambda idx: (-mutants[idx][2], ids[idx])
                )
                oracle_ranking = [ids[idx] for idx in ordered_indices]
                oracle_answer_json = json.dumps({"ranking": oracle_ranking})

                labels_payload = {
                    "assay": assay_id,
                    "nominal_size": self.size,
                    "size": self.size,
                    "seed": seed,
                    "uniprot_id": info["uniprot_id"],
                    "selection_type": info["function"],
                    "taxon": info["taxon"],
                    "msa_category": info["msa_category"],
                    "ids": ids,
                    "variants": [
                        {
                            "id": mid,
                            "variant_id": variant_id,
                            "dms_score": dms_score,
                        }
                        for mid, (variant_id, _seq, dms_score) in zip(ids, mutants)
                    ],
                }

                task_toml = _render_template(
                    task_toml_tpl,
                    {
                        "task_id": task_id,
                        "dms_id": json.dumps(assay_id)[1:-1],
                        "uniprot_id": json.dumps(info["uniprot_id"])[1:-1],
                        "selection_type": json.dumps(info["function"])[1:-1],
                        "taxon": json.dumps(info["taxon"])[1:-1],
                        "msa_category": json.dumps(info["msa_category"])[1:-1],
                        "seq_len": str(info["seq_len"]),
                        "n_variants": str(len(ids)),
                        "seed": str(seed),
                        "multi_mutant": "true" if info["multi"] else "false",
                        "network_mode": self.network_mode,
                    },
                )
                instruction_md = _render_template(
                    instruction_tpl,
                    {
                        "system_prompt": SYSTEM_PROMPT,
                        "user_prompt": adapted_user_prompt,
                        "n_variants": str(len(ids)),
                    },
                )
                solve_sh = _render_template(solve_tpl, {"oracle_answer_json": oracle_answer_json})

                expected_files: dict[str, bytes] = {
                    "task.toml": task_toml.encode("utf-8"),
                    "instruction.md": instruction_md.encode("utf-8"),
                    "environment/Dockerfile": dockerfile_bytes,
                    "solution/solve.sh": solve_sh.encode("utf-8"),
                    "tests/test.sh": test_sh_bytes,
                    "tests/grade.py": grade_py_bytes,
                    "tests/THIRD_PARTY_LICENSES.md": license_bytes,
                    "tests/labels.json": (json.dumps(labels_payload, indent=2) + "\n").encode("utf-8"),
                }
                expected_digests = {
                    rel: _sha256_bytes(payload) for rel, payload in expected_files.items()
                }

                task_dir = output_root / task_id
                recorded_rec = manifest_tasks.get(task_id)
                recorded_files = recorded_rec["files"] if recorded_rec is not None else None
                state = _inspect_existing_task(
                    task_dir, task_id, expected_files, recorded_files=recorded_files
                )
                if state.startswith("foreign:"):
                    raise FileExistsError(
                        f"Refusing to overwrite foreign or unverified entry at {task_dir} ({state}). "
                        "Remove it manually or choose a fresh --output-dir."
                    )
                if state.startswith("mismatch:") and not self.overwrite:
                    raise FileExistsError(
                        f"Existing task {task_id!r} differs from requested output ({state}). "
                        "Pass --overwrite to replace or choose a fresh --output-dir."
                    )
                planned.append((task_id, task_dir, state, expected_files, expected_digests))

            output_root.mkdir(parents=True, exist_ok=True)
            journal_root = tx_root / "journal"
            staging_root = tx_root / "staging"
            backups_root = tx_root / "backups"
            tmp_root = tx_root / "tmp"
            journal_root.mkdir(parents=True, exist_ok=True)
            staging_root.mkdir(parents=True, exist_ok=True)
            backups_root.mkdir(parents=True, exist_ok=True)
            tmp_root.mkdir(parents=True, exist_ok=True)

            generated: list[Path] = []
            for task_id, task_dir, state, expected_files, expected_digests in planned:
                if state == "exact" and not self.overwrite:
                    rec = manifest_tasks.get(task_id)
                    if rec is None or rec.get("files") != expected_digests:
                        manifest_tasks[task_id] = {
                            "task_name": f"{_PG_LLM_TASK_NAME_PREFIX}{task_id}",
                            "files": expected_digests,
                        }
                        manifest_dirty = True
                    generated.append(task_dir)
                    continue

                staging_dir = staging_root / task_id
                if staging_dir.exists() or staging_dir.is_symlink():
                    raise RuntimeError(
                        f"Pre-existing unjournaled staging directory blocks swap at {staging_dir}"
                    )

                prev_files: dict[str, str] | None = None
                recorded_rec = manifest_tasks.get(task_id)
                if recorded_rec is not None and task_dir.exists():
                    prev_files = {
                        rel_path: recorded_rec["files"][rel_path]
                        for rel_path in recorded_rec["files"]
                        if (task_dir / rel_path).is_file()
                        and _sha256_file(task_dir / rel_path) == recorded_rec["files"][rel_path]
                    }
                elif state == "exact" and task_dir.exists():
                    prev_files = dict(expected_digests)
                elif recorded_rec is not None and not task_dir.exists() and not task_dir.is_symlink():
                    manifest_tasks.pop(task_id, None)
                    _write_output_manifest(
                        output_root,
                        manifest_tasks,
                        root_license_sha256=effective_license_sha,
                        tmp_root=tmp_root,
                    )
                    manifest_dirty = False

                journal_path = _write_tx_journal(
                    journal_root,
                    output_root,
                    op="replace",
                    task_id=task_id,
                    previous_files=prev_files,
                    replacement_files=expected_digests,
                    tmp_root=tmp_root,
                )
                staging_dir.mkdir(parents=True, exist_ok=False)
                completed_commit = False
                had_existing = task_dir.exists()
                stage_tmp = tmp_root / f"{task_id}.stage.tmp"
                try:
                    staging_dir.chmod(0o755)
                    (staging_dir / "environment").mkdir(parents=True, exist_ok=True)
                    (staging_dir / "solution").mkdir(parents=True, exist_ok=True)
                    (staging_dir / "tests").mkdir(parents=True, exist_ok=True)

                    for rel_path, payload in expected_files.items():
                        target = staging_dir / rel_path
                        fmode = 0o755 if rel_path in _EXECUTABLE_REL_PATHS else 0o644
                        _write_file_atomic_via_tmp(stage_tmp, target, payload, mode=fmode)

                    backup_dir = backups_root / task_id
                    if had_existing:
                        if backup_dir.exists() or backup_dir.is_symlink():
                            raise RuntimeError(
                                f"Pre-existing backup blocks replacement at {backup_dir}"
                            )
                        task_dir.rename(backup_dir)
                    try:
                        staging_dir.rename(task_dir)
                    except BaseException:
                        if had_existing and backup_dir.exists() and not task_dir.exists():
                            backup_dir.rename(task_dir)
                        raise

                    ok, reason = _verify_task_dir_digests(
                        task_dir, task_id, expected_digests, check_exec=True
                    )
                    if not ok:
                        raise RuntimeError(
                            f"Committed task {task_dir} failed verification ({reason}); backup preserved at {backup_dir}"
                        )
                    manifest_tasks[task_id] = {
                        "task_name": f"{_PG_LLM_TASK_NAME_PREFIX}{task_id}",
                        "files": expected_digests,
                    }
                    _write_output_manifest(
                        output_root,
                        manifest_tasks,
                        root_license_sha256=effective_license_sha,
                        tmp_root=tmp_root,
                    )
                    manifest_dirty = False
                    completed_commit = True
                    if had_existing and backup_dir.exists():
                        shutil.rmtree(backup_dir)
                    journal_path.unlink(missing_ok=True)
                finally:
                    stage_tmp.unlink(missing_ok=True)
                    if staging_dir.exists():
                        shutil.rmtree(staging_dir, ignore_errors=True)
                    if (
                        not completed_commit
                        and not had_existing
                        and not task_dir.exists()
                        and not task_dir.is_symlink()
                        and not (backups_root / task_id).exists()
                    ):
                        journal_path.unlink(missing_ok=True)

                generated.append(task_dir)

            if root_license_needs_write:
                _write_file_atomic_via_tmp(
                    tmp_root / _LICENSE_TMP_NAME,
                    output_root / "THIRD_PARTY_LICENSES.md",
                    license_bytes,
                    mode=0o644,
                )
                if effective_license_sha != expected_license_sha:
                    effective_license_sha = expected_license_sha
                    manifest_dirty = True

            for stale_dir in unexpected_owned:
                stale_id = stale_dir.name
                prev_files = manifest_tasks[stale_id]["files"]
                journal_path = _write_tx_journal(
                    journal_root,
                    output_root,
                    op="prune",
                    task_id=stale_id,
                    previous_files=prev_files,
                    replacement_files=None,
                    tmp_root=tmp_root,
                )
                backup_dir = backups_root / stale_id
                if backup_dir.exists() or backup_dir.is_symlink():
                    raise RuntimeError(
                        f"Pre-existing backup blocks stale prune at {backup_dir}"
                    )
                stale_dir.rename(backup_dir)
                manifest_tasks.pop(stale_id, None)
                _write_output_manifest(
                    output_root,
                    manifest_tasks,
                    root_license_sha256=effective_license_sha,
                    tmp_root=tmp_root,
                )
                manifest_dirty = False
                shutil.rmtree(backup_dir)
                journal_path.unlink(missing_ok=True)

            if manifest_only_stale:
                for stale_id in manifest_only_stale:
                    manifest_tasks.pop(stale_id, None)
                manifest_dirty = True

            if manifest_dirty:
                _write_output_manifest(
                    output_root,
                    manifest_tasks,
                    root_license_sha256=effective_license_sha,
                    tmp_root=tmp_root,
                )

            print(f"Generated {len(generated)} PG-LLM task(s) in {output_root}")
            return generated
        finally:
            try:
                if lock_acquired and tx_owned:
                    _cleanup_empty_tx_root(tx_root, output_root)
            finally:
                if lock_acquired:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
