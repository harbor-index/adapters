#!/usr/bin/env bash
# Oracle: apply the upstream reference fix (git diff base_commit..reference_commit).
#
# Upstream's `reference` phase applies this patch to the clean base commit before
# the product starts. Here the product is already running when the oracle runs,
# and some product start scripts rewrite tracked source files (compatibility
# shims). Undo those tracked edits first so the patch lands on the clean base;
# the evaluation container replays the start script on top of the patch, as
# upstream does. Untracked files the product created are left alone.
set -euo pipefail
cd /workspace/product
git config --global --add safe.directory /workspace/product
if ! git diff --quiet; then
  git diff > /tmp/product-startup-edits.diff
  git checkout -q -- .
fi
# Files the product start script created (e.g. generated settings) are not part
# of the reference fix; remove them so the collected patch equals the upstream
# reference patch. Ignored files (dependencies, build output) stay.
git clean -q -fd
patch=/solution/reference.patch
if ! git apply "$patch"; then
  echo "git apply failed; retry excluding images" >&2
  git checkout -q -- .
  git apply \
    --exclude='*.png' --exclude='*.PNG' --exclude='*.jpg' --exclude='*.jpeg' \
    --exclude='*.JPG' --exclude='*.JPEG' --exclude='*.gif' --exclude='*.GIF' \
    --exclude='*.webp' --exclude='*.WEBP' --exclude='*.ico' --exclude='*.ICO' \
    --exclude='*.bmp' --exclude='*.BMP' "$patch"
fi
git status --short
