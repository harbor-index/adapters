#!/usr/bin/env python3
"""Build one arena's native image with every cloned repository pinned.

Uses the complete tracked upstream build context, including bundled runtime
COPY sources. Base images, apt repositories and upstream toolchain installers
remain mutable: this is source pinning, not a hermetic benchmark release.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tarfile
import urllib.request


_SPEC = importlib.util.spec_from_file_location(
    "harbor_codeclash_catalog",
    Path(__file__).resolve().parents[1] / "src/codeclash/catalog.py",
)
catalog = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(catalog)

PROVENANCE_PATH = "/codeclash-image-provenance.json"


def default_image(arena: str) -> str:
    return f"codeclash-adapter/{arena}:{catalog.UPSTREAM_COMMIT[:12]}"


def image_platform(arena: str) -> str | None:
    # The pinned RobotRumble repository supplies rumblebot as an ELF x86-64
    # executable; its native Dockerfile does not rebuild it for the host CPU.
    return "linux/amd64" if arena == "robotrumble" else None


def runtime_preparation(arena: str) -> list[str]:
    # Native BattleCode run depends on Gradle build, including engine, Scala,
    # test and client artifacts. Upstream's `update` only selects a version.
    # Resolve these at image-build time and prove a fresh build works offline.
    if arena in {"battlecode23", "battlecode24"}:
        return [
            "./gradlew --no-daemon clean build",
            "./gradlew --offline --no-daemon clean build",
        ]
    if arena == "halite3":
        return [
            "cd submission && cargo build",
            "cd submission && cargo clean && cargo build --offline",
        ]
    return []


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def pinned_dockerfile(original: str, arena: dict) -> str:
    """Pin each clone before any following build, retaining native instructions."""
    if hashlib.sha256(original.encode()).hexdigest() != arena["dockerfile_sha256"]:
        raise ValueError("Native Dockerfile differs from the catalog source snapshot")
    sources = list(arena["external_sources"])
    workspace = arena["workspace_source"]
    if workspace["kind"] == "repository":
        sources.append(workspace)
    expected = {(s["repository"], s["destination"]): s["commit"] for s in sources}
    seen = set()

    def replace(match: re.Match) -> str:
        key = (match[1], match[2])
        if key not in expected or key in seen:
            raise ValueError(f"Unexpected or duplicate clone instruction: {key}")
        seen.add(key)
        revision = expected[key]
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Invalid source revision")
        # Insert directly after clone, before cd/make/pip or any original checkout.
        return match[0] + f" && git -C {match[2]} checkout --detach {revision}"

    result = re.sub(
        r"git clone (https://github\.com/[^\s]+) (/[^\s]+)", replace, original
    )
    if seen != set(expected):
        raise ValueError("Not every catalog source matched a native clone instruction")
    if arena["id"] == "huskybench":
        # eval7 has no ARM64 wheel for the native Python version. Its published
        # source-install instructions require Cython before setup.py runs.
        marker = "RUN pip install -r engine/requirements.txt"
        if result.count(marker) != 1:
            raise ValueError(
                "Expected native HuskyBench requirements install is missing"
            )
        result = result.replace(marker, "RUN pip install Cython\n" + marker)
    if arena["id"] == "halite2":
        marker = (
            "RUN curl --proto '=https' --tlsv1.2 -sSf https://get-ghcup.haskell.org |"
        )
        if (
            result.count(marker) != 1
            or result.count("BOOTSTRAP_HASKELL_NONINTERACTIVE=1") != 1
        ):
            raise ValueError("Expected native GHCup bootstrap instruction is missing")
        result = result.replace(
            marker,
            "COPY harbor-build-assets/ghcup-0.1.0.yaml /opt/harbor-build-assets/ghcup-0.1.0.yaml\n"
            + marker,
        ).replace(
            "BOOTSTRAP_HASKELL_NONINTERACTIVE=1",
            "BOOTSTRAP_HASKELL_YAML=file:///opt/harbor-build-assets/ghcup-0.1.0.yaml "
            "BOOTSTRAP_HASKELL_NONINTERACTIVE=1",
        )
    for command in runtime_preparation(arena["id"]):
        result += f"\nRUN {command}\n"
    # Git HEAD/tree are source identities, not hashes of generated binaries.
    result += (
        '\nRUN printf \'{"workspace_head":"%s","workspace_tree":"%s"}\\n\' '
        '"$(git -C /workspace rev-parse HEAD)" '
        '"$(git -C /workspace rev-parse HEAD^{tree})" '
        f"> {PROVENANCE_PATH}\n"
        f'LABEL org.codeclash.source-revision="{catalog.UPSTREAM_COMMIT}" \\\n'
        f'      org.harbor.codeclash.arena="{arena["id"]}"\n'
    )
    if workspace["kind"] == "repository":
        result += f'LABEL org.codeclash.workspace-revision="{workspace["commit"]}"\n'
    else:
        result += f'LABEL org.codeclash.workspace-tree="{workspace["tree"]}"\n'
    if arena["id"] == "battlesnake":
        result += f'LABEL org.codeclash.battlesnake-revision="{workspace["commit"]}"\n'
    return result


def tracked_context(repo: Path, build_dir: Path) -> Path:
    """Export exactly the pin; untracked/ignored local files never enter COPY."""
    archive = build_dir / "upstream-context.tar"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "archive",
            "--format=tar",
            "-o",
            str(archive),
            catalog.UPSTREAM_COMMIT,
        ],
        check=True,
    )
    context = build_dir / "context"
    context.mkdir()
    with tarfile.open(archive) as source:
        # This pin contains only regular files/directories. Reject other types
        # rather than depending on extractall(filter=...), absent in macOS's
        # system Python. The exported source must never escape its context.
        for member in source.getmembers():
            path = Path(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError(f"Unsupported archive member: {member.name}")
        source.extractall(context)
    archive.unlink()
    return context


def prepare_build_assets(context: Path, arena: str) -> list[dict]:
    """Fetch official GHCup metadata through GitHub's API when raw is unreachable.

    The native installer supports BOOTSTRAP_HASKELL_YAML. Use the same metadata
    repository/file and preserve its recommended-version selection. Record the
    exact revision and bytes; do not substitute a different Haskell toolchain.
    """
    if arena != "halite2":
        return []
    api = "https://api.github.com/repos/haskell/ghcup-metadata"
    headers = {"User-Agent": "harbor-codeclash-image-builder"}
    with urllib.request.urlopen(
        urllib.request.Request(f"{api}/commits/master", headers=headers), timeout=30
    ) as response:
        revision = json.load(response)["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Invalid GHCup metadata revision")
    url = f"{api}/contents/ghcup-0.1.0.yaml?ref={revision}"
    with urllib.request.urlopen(
        urllib.request.Request(
            url, headers={**headers, "Accept": "application/vnd.github.raw+json"}
        ),
        timeout=60,
    ) as response:
        data = response.read()
    if b"ghcupDownloads:" not in data:
        raise ValueError("GitHub did not return the expected GHCup YAML metadata")
    target = context / "harbor-build-assets/ghcup-0.1.0.yaml"
    target.parent.mkdir()
    target.write_bytes(data)
    return [
        {
            "purpose": "official GHCup metadata via alternate GitHub delivery endpoint",
            "repository": "https://github.com/haskell/ghcup-metadata",
            "commit": revision,
            "url": url,
            "context_path": str(target.relative_to(context)),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    ]


def inspect_workspace(image_id: str, build_dir: Path) -> dict:
    """Read build-time identities from an unstarted, owned temporary container."""
    container_id = run(
        "docker", "create", "--network=none", "--entrypoint=/bin/true", image_id
    )
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("Docker create did not return a full container ID")
    try:
        target = build_dir / "workspace-provenance.json"
        subprocess.run(
            ["docker", "cp", f"{container_id}:{PROVENANCE_PATH}", str(target)],
            check=True,
        )
        provenance = json.loads(target.read_text())
        if set(provenance) != {"workspace_head", "workspace_tree"} or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value)
            for value in provenance.values()
        ):
            raise ValueError("Invalid build-time workspace provenance")
        return provenance
    finally:
        subprocess.run(
            ["docker", "rm", container_id], check=True, stdout=subprocess.DEVNULL
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arena", required=True, choices=tuple(catalog.ARENAS))
    parser.add_argument("--upstream-repo", required=True, type=Path)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument(
        "--image", help="Default: codeclash-adapter/<arena>:<upstream revision prefix>"
    )
    args = parser.parse_args()
    args.image = args.image or default_image(args.arena)
    arena = catalog.get_arena(args.arena)
    repo = args.upstream_repo.resolve()
    if run("git", "-C", str(repo), "rev-parse", "HEAD") != catalog.UPSTREAM_COMMIT:
        parser.error(f"Expected upstream commit {catalog.UPSTREAM_COMMIT}")
    if run("git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"):
        parser.error("Upstream checkout has tracked modifications")
    build_dir = args.build_dir.resolve()
    build_dir.mkdir(parents=True, exist_ok=False)
    original = (repo / arena["dockerfile_path"]).read_text()
    dockerfile = pinned_dockerfile(original, arena)
    (build_dir / "Dockerfile").write_text(dockerfile)
    context = tracked_context(repo, build_dir)
    build_assets = prepare_build_assets(context, arena["id"])
    command = ["docker", "build", "--progress=plain"]
    if platform := image_platform(arena["id"]):
        command += ["--platform", platform]
    command += ["-f", str(build_dir / "Dockerfile"), "-t", args.image, str(context)]
    subprocess.run(command, check=True)
    image = json.loads(run("docker", "image", "inspect", args.image))[0]
    image_id = image["Id"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Unexpected Docker image ID")
    labels = image["Config"].get("Labels") or {}
    if (
        labels.get("org.codeclash.source-revision") != catalog.UPSTREAM_COMMIT
        or labels.get("org.harbor.codeclash.arena") != arena["id"]
    ):
        raise ValueError("Built image labels do not match the requested source")
    workspace = inspect_workspace(image_id, build_dir)
    source = arena["workspace_source"]
    if (
        source["kind"] == "repository"
        and workspace["workspace_head"] != source["commit"]
    ):
        raise ValueError("Built workspace HEAD differs from its repository pin")
    if (
        source["kind"] == "bundled_runtime"
        and workspace["workspace_tree"] != source["tree"]
    ):
        raise ValueError("Built workspace tree differs from its bundled runtime pin")
    manifest = {
        "schema_version": 1,
        "arena": arena["id"],
        "upstream_commit": catalog.UPSTREAM_COMMIT,
        "image_tag": args.image,
        "image_id": image_id,
        "platform": {"os": image["Os"], "architecture": image["Architecture"]},
        "requested_platform": image_platform(arena["id"]),
        "workspace_source": source,
        "external_sources": arena["external_sources"],
        **workspace,
        "dockerfile_sha256": hashlib.sha256(dockerfile.encode()).hexdigest(),
        "native_dockerfile_sha256": arena["dockerfile_sha256"],
        "runtime_preparation": runtime_preparation(arena["id"]),
        "build_preparation": ["pip install Cython (eval7 source-build prerequisite)"]
        if arena["id"] == "huskybench"
        else [],
        "build_assets": build_assets,
        "build_context": "git archive of the full pinned upstream repository plus recorded build metadata assets",
        "limitations": [
            "Base images, apt packages and upstream toolchain installers remain unpinned",
            "Source pins and a successful image build do not establish runtime or benchmark parity",
        ],
    }
    (build_dir / "build_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
