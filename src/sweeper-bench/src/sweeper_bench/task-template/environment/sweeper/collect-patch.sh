#!/usr/bin/env bash
# Write the agent's change as /logs/artifacts/prediction.patch.
# Same rules as the upstream runtime/harnesses/prediction/run.sh: tracked edits
# via `git diff --binary`, plus new non-empty files up to 1 MB that are not
# runtime junk, added with intent-to-add. Runs as a [[verifier.collect]] hook
# after the agent phase ends. An empty file means the agent made no change.
set -euo pipefail

PRODUCT_DIR=/workspace/product
OUT_DIR=/logs/artifacts
PATCH_FILE="$OUT_DIR/prediction.patch"
mkdir -p "$OUT_DIR"
cd "$PRODUCT_DIR"
git config --global --add safe.directory "$PRODUCT_DIR"

keep_untracked() {
  local p="$1"
  [ -f "$p" ] || return 1
  local sz
  sz="$(wc -c <"$p")"
  [ "$sz" -gt 0 ] || return 1
  [ "$sz" -le 1048576 ] || return 1
  case "$p" in
    router.php|*/router.php|*-router.php|*/*-router.php) return 1 ;;
    .product-deps-linked|*/.product-deps-linked|.product-deps*|*/.product-deps*) return 1 ;;
  esac
  case "/$p/" in
    */node_modules/*|*/vendor/*|*/dist/*|*/build/*|*/coverage/*|*/__pycache__/*|*/templates_c/*|*/.cache/*) return 1 ;;
  esac
  return 0
}

UNTRACKED_LOG="$OUT_DIR/status.untracked.txt"
: >"$UNTRACKED_LOG"
git status --short >"$OUT_DIR/status.after.txt" || true
while IFS= read -r -d '' path; do
  if keep_untracked "$path"; then
    echo "keep $path" >>"$UNTRACKED_LOG"
    git add -N -- "$path"
  else
    echo "skip $path" >>"$UNTRACKED_LOG"
  fi
done < <(git ls-files -z --others --exclude-standard)

git diff --binary >"$PATCH_FILE"

if [ -s "$PATCH_FILE" ] && grep -qE '^diff --git ' "$PATCH_FILE"; then
  echo "prediction patch: $(grep -c '^diff --git ' "$PATCH_FILE") file(s)"
else
  : >"$PATCH_FILE"
  echo "prediction patch: empty"
fi
