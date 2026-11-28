"""Give LoopsBench's local COPY inputs content-addressed BuildKit paths."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from tempfile import TemporaryDirectory

_PREFIX = ".loopsbench-context-"
_CONTROL_FILES = {"Dockerfile", "dockerfile", ".dockerignore", "Dockerfile.dockerignore"}
_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?")
_IGNORE_GLOB = re.compile(r"\*\*/[A-Za-z0-9_.?*-]+")
_COPY_ARGS = re.compile(r"(?P<source>\S+)[ \t]+(?P<destination>\S+)[ \t]*")
_CONTINUATION = re.compile(r"(?<!\\)\\[ \t]*$")
_DIRECTIVE = re.compile(r"#\s*(syntax|escape)\s*=\s*(.*?)\s*$", re.IGNORECASE)
_WORD = r"""(?:[^\s'"\\]+|'[^']*'|"(?:\\.|[^"\\])*"|\\.)+"""
_WORDS = re.compile(rf"{_WORD}(?:[ \t]+{_WORD})*")
_HEREDOC_START = re.compile(r"[0-9]*<<-?")
_HEREDOC = re.compile(r"""[0-9]*<<(-?)(['"]?)([A-Za-z0-9_][A-Za-z0-9_.-]*)\2""")
_OTHER_INSTRUCTIONS = frozenset(
    "ARG CMD ENTRYPOINT ENV EXPOSE FROM HEALTHCHECK LABEL MAINTAINER SHELL "
    "STOPSIGNAL USER VOLUME WORKDIR".split()
)


def _literal_path(value: str) -> bool:
    return bool(_PATH.fullmatch(value)) and not {".", ".."}.intersection(value.split("/"))


def _run_heredocs(args: str, location: str) -> list[tuple[str, bool]]:
    if "<<" not in args:
        return []
    if args.startswith("["):
        command = json.loads(args)
        if isinstance(command, list) and all(isinstance(arg, str) for arg in command):
            return []
        raise ValueError(f"{location}: unsupported RUN exec form")

    args = args.rstrip(" \t")
    if not _WORDS.fullmatch(args):
        raise ValueError(f"{location}: unsupported RUN heredoc syntax")
    words = iter(re.findall(_WORD, args))
    documents = []
    for word in words:
        if _HEREDOC_START.fullmatch(word):
            word += next(words, "")
        match = _HEREDOC.fullmatch(word)
        if match:
            documents.append((match[3], bool(match[1])))
        elif re.match(r"[0-9]*<<", word):
            raise ValueError(f"{location}: unsupported RUN heredoc delimiter {word!r}")
    return documents


def _copy_sources(lines: list[str], dockerfile: Path) -> list[tuple[int, int, str]]:
    """Find source spans without rewriting heredoc bodies or RUN continuations."""
    physical_lines = iter(enumerate(lines))
    copies = []
    started = False
    for index, line in physical_lines:
        location = f"{dockerfile}:{index + 1}"
        logical = line.removesuffix("\r").lstrip()
        if not logical or logical.startswith("#"):
            if not started and (directive := _DIRECTIVE.fullmatch(logical)):
                name, value = directive.groups()
                if value != {"syntax": "docker/dockerfile:1", "escape": "\\"}[name.lower()]:
                    raise ValueError(f"{location}: unsupported {name} directive {value!r}")
            continue
        started = True
        continued = bool(_CONTINUATION.search(logical))
        while continuation := _CONTINUATION.search(logical):
            for _, following in physical_lines:
                if following.strip() and not following.lstrip().startswith("#"):
                    logical = logical[:continuation.start()] + following.removesuffix("\r")
                    break
            else:
                raise ValueError(f"{location}: unterminated line continuation")

        instruction = re.match(r"(\w+)[ \t]+", logical)
        if not instruction:
            raise ValueError(f"{location}: unsupported Dockerfile instruction {logical!r}")
        opcode = instruction[1].upper()
        args = logical[instruction.end():]
        documents = []
        if opcode == "COPY":
            copy = _COPY_ARGS.fullmatch(args)
            if continued or not copy or args.startswith("--"):
                raise ValueError(f"{location}: expected COPY with one literal source and one destination")
            source = copy["source"]
            if heredoc := _HEREDOC.fullmatch(source):
                documents.append((heredoc[3], bool(heredoc[1])))
            elif _literal_path(source):
                copies.append((index, len(line) - len(line.lstrip()) + instruction.end(), source))
            else:
                raise ValueError(f"{location}: unsupported COPY source {source!r}")
        elif opcode == "RUN":
            if args.startswith("--"):
                raise ValueError(f"{location}: unsupported RUN options")
            documents = _run_heredocs(args, location)
        elif opcode not in _OTHER_INSTRUCTIONS:
            raise ValueError(f"{location}: unsupported Dockerfile instruction {opcode}")

        for delimiter, strip_tabs in documents:
            for _, body in physical_lines:
                body = body.removesuffix("\r")
                if (body.lstrip("\t") if strip_tabs else body) == delimiter:
                    break
            else:
                raise ValueError(f"{location}: unterminated heredoc {delimiter!r}")
    return copies


def _ignore_patterns(path: Path) -> list[str]:
    if not os.path.lexists(path):
        return []
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"{path}: expected a regular .dockerignore")
    patterns = []
    lines = path.read_bytes().decode("utf-8-sig").split("\n")
    for index, line in enumerate(lines, 1):
        if line.startswith("#") or not line.strip():
            continue
        pattern = line.strip().removeprefix("/").removesuffix("/")
        recursive_glob = (
            _IGNORE_GLOB.fullmatch(pattern)
            and pattern[3:] not in {".", ".."}
            and "**" not in pattern[3:]
        )
        if not (_literal_path(pattern) or recursive_glob):
            raise ValueError(f"{path}:{index}: unsupported ignore pattern {line!r}")
        patterns.append(pattern)
    return patterns


def _source_roots(root: Path, sources: set[str]) -> set[str]:
    roots = set()
    for source in sorted(sources):
        parts = source.rstrip("/").split("/")
        roots.add(parts[0])
        if not parts[0] or parts[0] in _CONTROL_FILES or parts[0].startswith(_PREFIX):
            raise ValueError(f"{root}: unsupported COPY source {source!r}")
        path = root
        for part in parts:
            path /= part
            if path.is_symlink():
                raise ValueError(f"{path}: COPY source traverses a symlink")
        if not path.exists():
            raise ValueError(f"{path}: COPY source does not exist")
    return roots


def _namespace_name(root: Path, roots: set[str], patterns: list[str]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(patterns).encode("ascii") + b"\n")
    hardlinks: dict[tuple[int, int], str] = {}

    def add(path: Path) -> None:
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = info.st_mode
        payload = None
        if stat.S_ISREG(mode):
            with path.open("rb") as stream:
                payload = hashlib.file_digest(stream, "sha256").hexdigest()
        elif stat.S_ISLNK(mode):
            payload = os.readlink(path)
        elif not stat.S_ISDIR(mode):
            raise ValueError(f"{path}: unsupported build context entry")
        link = None
        if stat.S_ISREG(mode) and info.st_nlink > 1:
            link = hardlinks.setdefault((info.st_dev, info.st_ino), relative)
            if link == relative:
                link = None
        record = (relative, mode, info.st_mtime_ns, link, payload)
        digest.update(json.dumps(record).encode("ascii") + b"\n")
        if stat.S_ISDIR(mode):
            for child in sorted(path.iterdir()):
                add(child)

    for name in sorted(roots):
        add(root / name)
    return _PREFIX + digest.hexdigest()


def isolate_build_context(env_dir: Path) -> None:
    """Prepare a generated environment directory after ``_write_environment``.

    BuildKit's local sync can reuse bytes at a matching path with the same
    size and mtime. Move the local COPY roots under a digest of their input
    trees so different bytes arrive at different paths. Native COPY still
    supplies the original basenames, modification times and links to the image.

    Call while the adapter owns the directory, before Harbor reads it. The
    supported Dockerfile and ignore syntax is limited to the adapter's use.
    """
    env_dir = Path(env_dir)
    dockerfile = env_dir / "Dockerfile"
    if not stat.S_ISREG(dockerfile.lstat().st_mode):
        raise ValueError(f"{dockerfile}: expected a regular Dockerfile")
    lines = dockerfile.read_bytes().decode("utf-8").split("\n")
    copies = _copy_sources(lines, dockerfile)
    sources = {source for _, _, source in copies}
    existing = list(env_dir.glob(f"{_PREFIX}*"))
    sidecar = env_dir / "Dockerfile.dockerignore"
    patterns = _ignore_patterns(env_dir / ".dockerignore")

    if existing:
        namespace = existing[0]
        prefix = namespace.name + "/"
        if (
            len(existing) != 1
            or namespace.is_symlink()
            or not namespace.is_dir()
            or not sources
            or not all(source.startswith(prefix) for source in sources)
        ):
            raise ValueError(f"{env_dir}: unexpected build context namespace")
        original_sources = {source.removeprefix(prefix) for source in sources}
        roots = _source_roots(namespace, original_sources)
        expected_ignores = _namespaced_ignores(namespace.name, patterns)
        actual_ignores = None
        if os.path.lexists(sidecar):
            if not stat.S_ISREG(sidecar.lstat().st_mode):
                raise ValueError(f"{sidecar}: expected a regular ignore file")
            actual_ignores = sidecar.read_bytes()
        if (
            namespace.name != _namespace_name(namespace, roots, patterns)
            or {p.name for p in namespace.iterdir()} != roots
            or any(os.path.lexists(env_dir / name) for name in roots)
            or actual_ignores != expected_ignores
        ):
            raise ValueError(f"{env_dir}: isolated build context was modified")
        return

    if os.path.lexists(sidecar):
        raise ValueError(f"{sidecar}: unsupported Dockerfile-specific ignore file")
    if not copies:
        return
    roots = _source_roots(env_dir, sources)
    namespace = env_dir / _namespace_name(env_dir, roots, patterns)
    for index, offset, _ in copies:
        line = lines[index]
        lines[index] = f"{line[:offset]}{namespace.name}/{line[offset:]}"

    with TemporaryDirectory(dir=env_dir) as temporary:
        staged_dockerfile = Path(temporary) / dockerfile.name
        staged_dockerfile.write_bytes("\n".join(lines).encode("utf-8"))
        staged_dockerfile.chmod(stat.S_IMODE(dockerfile.stat().st_mode))
        staged_ignore = Path(temporary) / sidecar.name
        if data := _namespaced_ignores(namespace.name, patterns):
            staged_ignore.write_bytes(data)

        namespace.mkdir()
        moved = []
        installed_ignore = False
        try:
            for name in sorted(roots):
                (env_dir / name).rename(namespace / name)
                moved.append(name)
            if data:
                staged_ignore.replace(sidecar)
                installed_ignore = True
            staged_dockerfile.replace(dockerfile)
        except BaseException:
            if installed_ignore:
                sidecar.unlink()
            for name in reversed(moved):
                (namespace / name).rename(env_dir / name)
            namespace.rmdir()
            raise


def _namespaced_ignores(namespace: str, patterns: list[str]) -> bytes | None:
    if not patterns:
        return None
    # The Dockerfile-specific file takes precedence over .dockerignore.
    # Re-include the wrapper in case an original recursive glob matches it.
    scoped = [*patterns, f"!{namespace}", *(f"{namespace}/{pattern}" for pattern in patterns)]
    return ("\n".join(scoped) + "\n").encode("ascii")
