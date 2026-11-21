#!/usr/bin/env bash
# Deterministic source-equivalence check; this does not call any model API.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
CHECK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agenteval-upstream-check.XXXXXX")"
trap 'rm -rf -- "$CHECK_DIR"' EXIT
AGENTGYM_REVISION=3ef9235d23e68e7c2920c5422ad957dc8ced5c6c
LMRL_REVISION=83abeedb3a461c3d7d20572b73318a259d85f2ac

git clone --filter=blob:none --no-checkout https://github.com/WooooDyy/AgentGym.git "$CHECK_DIR/upstream"
git -C "$CHECK_DIR/upstream" checkout --detach "$AGENTGYM_REVISION"
git -C "$CHECK_DIR/upstream" submodule update --init agentenv-lmrlgym/lmrlgym
test "$(git -C "$CHECK_DIR/upstream" rev-parse HEAD)" = "$AGENTGYM_REVISION"
test "$(git -C "$CHECK_DIR/upstream/agentenv-lmrlgym/lmrlgym" rev-parse HEAD)" = "$LMRL_REVISION"

docker build --file "$SCRIPT_DIR/Dockerfile.verification" --iidfile "$CHECK_DIR/image.id" "$SCRIPT_DIR"
mkdir -p "$ADAPTER_DIR/validation"
docker run --rm --user "$(id -u):$(id -g)"   --volume "$CHECK_DIR/upstream:/upstream:ro"   --volume "$ADAPTER_DIR:/adapter:ro"   --volume "$ADAPTER_DIR/validation:/output"   "$(cat "$CHECK_DIR/image.id")"   python /adapter/scripts/check_upstream.py --upstream /upstream --adapter /adapter   --output /output/differential.json
