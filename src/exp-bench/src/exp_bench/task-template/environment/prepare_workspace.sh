#!/bin/bash
# Clone the paper repository at the pinned commit and mask it like upstream
# (evaluation/eval.py: git_clone_repo + mask_repo). Generated per task.
set -euo pipefail

REPO_URL="{repo_url}"
COMMIT="{commit}"
MASKED_FILES=(
{masked_files}
)

# Network retries only (large fetches can fail transiently); HTTP/1.1 avoids the GnuTLS
# "Error decoding the received TLS packet" failures seen with some proxies.
retry() {
    local attempt
    for attempt in 1 2 3 4 5; do
        "$@" && return 0
        echo "attempt $attempt failed: $*" >&2
        sleep $((attempt * 10))
    done
    return 1
}
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.version GIT_CONFIG_VALUE_0=HTTP/1.1

cd /workspace
git init -q .
git remote add origin "$REPO_URL"
retry git fetch -q --depth 1 origin "$COMMIT"
git checkout -q FETCH_HEAD

# Upstream convert_ssh_submodules_to_https + update_submodules.
if [ -f .gitmodules ]; then
    sed -i -E 's#url = git@github\.com:(.+)\.git#url = https://github.com/\1.git#' .gitmodules
    git submodule sync -q
    submodules() {
        git submodule update -q --init --recursive --depth 1 || git submodule update -q --init --recursive
    }
    retry submodules
fi
unset GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0

# Mask the README first, then the task-specific source files.
if [ -f README.md ]; then
    git rm -q --cached -f README.md
    rm -f README.md
fi
for rel in "${MASKED_FILES[@]}"; do
    if [ ! -f "$rel" ]; then
        echo "EXP-Bench masking failed: $rel not found at $COMMIT" >&2
        exit 1
    fi
    dir="$(dirname "$rel")"
    name="$(basename "$rel")"
    # Enter the file's directory so files inside submodules are removed from the right index.
    (cd "$dir" && git rm -q --cached -f "$name")
    rm -f "$rel"
done

# Upstream copies its LLM credential files into the repo as setup_apis_exp/.
mkdir -p setup_apis_exp
cat > setup_apis_exp/env.sh <<'EOF'
# LLM credentials for experiments that need to call an LLM API (may be empty).
export OPENAI_API_KEY="${EXP_BENCH_TASK_OPENAI_API_KEY:-}"
export OPENAI_BASE_URL="${EXP_BENCH_TASK_OPENAI_BASE_URL:-}"
export OPENAI_API_BASE="${EXP_BENCH_TASK_OPENAI_BASE_URL:-}"
EOF
echo "setup_apis_exp/" >> .git/info/exclude

git config --global --add safe.directory '*'
chmod -R 777 /workspace
