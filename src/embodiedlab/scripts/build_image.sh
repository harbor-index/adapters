#!/usr/bin/env bash
# Build the image every EmbodiedLab Harbor task runs in, from the build context the adapter wrote.
#
# usage: scripts/build_image.sh <image-context> [tag]
#   image-context  the adapter's --image-context (default there: <output-dir>-image)
#   tag            must equal the tasks' [environment].docker_image (default: embodiedlab/harbor:<version>)
# Build the EmbodiedLab lab image first (`./embodiedlab build` in the EmbodiedLab checkout).
# environment: LAB_IMAGE (default embodiedlab/lab:<version>), PIP_INDEX_URL and APT_MIRROR (package mirrors).
set -euo pipefail
CTX="${1:?usage: build_image.sh <image-context> [tag]}"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$CTX/el-harbor/embodiedlab_api/__init__.py")
TAG="${2:-embodiedlab/harbor:$VERSION}"
docker build -t "$TAG" \
  --build-arg LAB_IMAGE="${LAB_IMAGE:-embodiedlab/lab:$VERSION}" \
  --build-arg PIP_INDEX_URL="${PIP_INDEX_URL:-}" \
  --build-arg APT_MIRROR="${APT_MIRROR:-}" \
  "$CTX"
echo "built $TAG"
