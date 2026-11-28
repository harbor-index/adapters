#!/bin/bash
# Oracle: apply the upstream gold patch and write it to the prompt's output
# location (.active-swe-tmp/solution.patch), exactly as an agent would.
set -euo pipefail

cd /testbed
mkdir -p .active-swe-tmp
cp /solution/gold.patch .active-swe-tmp/solution.patch

git apply --whitespace=fix .active-swe-tmp/solution.patch \
    || patch --batch --fuzz=5 -p1 -i .active-swe-tmp/solution.patch

git --no-pager diff --stat
