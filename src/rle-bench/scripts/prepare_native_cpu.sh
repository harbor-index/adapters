#!/bin/bash
set -euo pipefail
rle_source_root=${1:?Usage: prepare_native_cpu.sh /path/to/RLE-Bench}
cd "$rle_source_root"
if [ "$(git rev-parse HEAD)" != c6bf572283b5453c0ee76eaa3054f23d691e433d ]; then
    echo "Expected the pinned RLE-Bench commit" >&2
    exit 1
fi
git diff --quiet HEAD --
make task06-assets task07 task08 task09
for variant in rgb-only rgb-depth; do
    docker build -t "rlebench-task06-$variant-agent:dev" "tasks/task06/$variant/environment"
    docker build -f "tasks/task06/$variant/tests/Dockerfile" \
        -t "rlebench-task06-$variant-verifier:dev" .
done
