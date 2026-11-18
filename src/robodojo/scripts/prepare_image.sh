#!/usr/bin/env bash
# Build pinned upstream source, then add revision markers without changing it.
set -euo pipefail

revision="f57e1633ffed59761bc8e568cfe7530c4b33381c"
xpolicylab_revision="5d0be1499634d3176258c8f2e31f5fe816497b0a"
upstream_url="https://github.com/RoboDojo-Benchmark/RoboDojo.git"
default_image="robodojo-harbor:f57e1633ffed"

usage() {
  cat <<'HELP'
Usage: bash scripts/prepare_image.sh [IMAGE_TAG [CHECKOUT_DIR]]

Build the pinned RoboDojo simulator on a Linux x86_64 NVIDIA host with Docker.
The default image is robodojo-harbor:f57e1633ffed. The default checkout is a
revision-specific directory under TMPDIR (or /tmp). Existing checkouts must be
clean and already at the pinned commit; they are never reset.

This builds the upstream Dockerfile, including its license-consent environment
variables. Review that file and applicable terms before running this script.
No Assets, policy checkpoints, or policy-specific dependencies are downloaded.
HELP
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi
if [[ $# -gt 2 ]]; then
  usage >&2
  exit 2
fi

image_tag="${1:-${default_image}}"
checkout_dir="${2:-${TMPDIR:-/tmp}/robodojo-harbor-${revision}}"

if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "x86_64" ]]; then
  echo "The upstream Isaac Sim image requires a Linux x86_64 build host." >&2
  exit 1
fi
for command_name in git docker; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Missing required command: ${command_name}" >&2
    exit 1
  fi
done

if [[ -e "${checkout_dir}" ]]; then
  if [[ ! -d "${checkout_dir}/.git" ]]; then
    echo "Existing checkout path is not a Git clone: ${checkout_dir}" >&2
    exit 1
  fi
  if [[ "$(git -C "${checkout_dir}" rev-parse HEAD)" != "${revision}" ]]; then
    echo "Existing checkout is not at ${revision}; choose a new directory." >&2
    exit 1
  fi
  if [[ -n "$(git -C "${checkout_dir}" status --porcelain)" ]]; then
    echo "Existing checkout has local changes; choose a new directory." >&2
    exit 1
  fi
else
  GIT_LFS_SKIP_SMUDGE=1 git clone --no-checkout "${upstream_url}" "${checkout_dir}"
  GIT_LFS_SKIP_SMUDGE=1 git -C "${checkout_dir}" checkout --detach "${revision}"
fi

# Gitlinks pin IsaacLab, CuRobo, and XPolicyLab. Never use submodule --remote.
GIT_LFS_SKIP_SMUDGE=1 git -C "${checkout_dir}" submodule update --init --recursive
if [[ "$(git -C "${checkout_dir}/XPolicyLab" rev-parse HEAD)" != "${xpolicylab_revision}" ]]; then
  echo "XPolicyLab checkout does not match the expected pinned revision." >&2
  exit 1
fi

build_context="$(mktemp -d "${TMPDIR:-/tmp}/robodojo-image.XXXXXX")"
base_image="robodojo-harbor-build:${revision}-$$"
cleanup() {
  rm -rf "${build_context}"
}
trap cleanup EXIT

# A separate local tag works with both classic Docker and BuildKit builders.
docker build --tag "${base_image}" "${checkout_dir}"
cat > "${build_context}/Dockerfile" <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG ROBODOJO_REVISION
ARG XPOLICYLAB_REVISION
LABEL org.opencontainers.image.revision=${ROBODOJO_REVISION}
LABEL org.opencontainers.image.source="https://github.com/RoboDojo-Benchmark/RoboDojo"
RUN printf '%s\n' "${ROBODOJO_REVISION}" > /workspace/RoboDojo/.harbor-upstream-revision \
    && printf '%s\n' "${XPOLICYLAB_REVISION}" > /workspace/RoboDojo/.harbor-xpolicylab-revision
DOCKERFILE

docker build \
  --build-arg "BASE_IMAGE=${base_image}" \
  --build-arg "ROBODOJO_REVISION=${revision}" \
  --build-arg "XPOLICYLAB_REVISION=${xpolicylab_revision}" \
  --tag "${image_tag}" \
  "${build_context}"

printf 'Built %s from %s\n' "${image_tag}" "${revision}"
printf 'Supply an existing Assets directory separately with robodojo --assets-dir.\n'
printf 'Intermediate local image: %s\n' "${base_image}"
