#!/usr/bin/env bash
# Clone REPO at COMMIT into DEST with no remote, no refs and no other history.
# Mirrors clone_workspace()/reset_workspace() in the upstream runtime/worker.py:
# shallow fetch first, full clone + flatten as the fallback, then require that
# HEAD is the base commit and that it is the only commit in the object store.
set -euo pipefail

repo="$1"
commit="$2"
dest="$3"

flatten() {
  git -C "$dest" checkout -q --force --detach "$commit"
  git -C "$dest" remote remove origin 2>/dev/null || true
  git -C "$dest" for-each-ref --format='%(refname)' | while read -r ref; do
    [ -n "$ref" ] && git -C "$dest" update-ref -d "$ref" || true
  done
  git -C "$dest" reflog expire --expire=now --all
  git -C "$dest" gc -q --prune=now
}

cd /
rm -rf "$dest"
mkdir -p "$dest"
git -C "$dest" init -q
git -C "$dest" remote add origin "$repo"
if git -C "$dest" fetch -q --depth 1 origin "$commit"; then
  git -C "$dest" checkout -q --force FETCH_HEAD
  git -C "$dest" remote remove origin
else
  echo "shallow fetch failed, full clone then flatten" >&2
  rm -rf "$dest"
  git clone -q "$repo" "$dest"
  flatten
fi

git config --global --add safe.directory "$dest"
git -C "$dest" reset -q --hard "$commit"
git -C "$dest" clean -q -fdx

head="$(git -C "$dest" rev-parse HEAD)"
if [ "$head" != "$commit" ]; then
  echo "HEAD $head != $commit" >&2
  exit 1
fi
count="$(git -C "$dest" rev-list --count --all)"
if [ "$count" != "1" ]; then
  echo "workspace has $count commits; expected only the base commit" >&2
  exit 1
fi
if git -C "$dest" remote | grep -q .; then
  echo "workspace still has a remote" >&2
  exit 1
fi
