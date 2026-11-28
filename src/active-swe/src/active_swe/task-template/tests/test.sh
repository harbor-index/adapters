#!/bin/bash
# Active-SWE Recorded-track verifier.
#
# Mirrors upstream run_eval_container(): take the agent's predicted patch,
# apply it to a clean copy of the base repository (a fresh container from the
# task image, with no network), re-initialise the project
# environment, run the instance eval_script, and grade the log against the
# ground-truth FAIL_TO_PASS / PASS_TO_PASS tests.
set -uo pipefail

LOG_DIR=/logs/verifier
WORK=/tmp/active-swe-verifier
ACTIVE_SWE_HOME=/opt/active-swe
mkdir -p "$LOG_DIR" "$WORK"
echo 0 > /logs/verifier/reward.txt

cd /testbed
git config --global --add safe.directory /testbed >/dev/null 2>&1 || true
BASE_COMMIT=$(cat "$ACTIVE_SWE_HOME/base_commit")

# 1. Collect the predicted patch. Upstream grades only
#    .active-swe-tmp/solution.patch (the prompt's output contract); Harbor
#    restores it into this fresh verifier container as a task artifact.
if [ -s .active-swe-tmp/solution.patch ]; then
    cp .active-swe-tmp/solution.patch "$WORK/pred.patch"
else
    : > "$WORK/pred.patch"
fi
cp "$WORK/pred.patch" "$LOG_DIR/pred.patch"
if [ ! -s "$WORK/pred.patch" ]; then
    echo "No predicted patch: resolved=0"
    exit 1
fi

# 2. Restore the pristine base state, then apply the patch (same fallbacks as upstream).
git reset -q --hard "$BASE_COMMIT"
git clean -fdq
_patch_applied=false
if git apply --whitespace=fix "$WORK/pred.patch" >"$LOG_DIR/patch_apply.log" 2>&1; then _patch_applied=true; fi
if [ "$_patch_applied" = "false" ] && git apply -p1 -v "$WORK/pred.patch" >>"$LOG_DIR/patch_apply.log" 2>&1; then _patch_applied=true; fi
if [ "$_patch_applied" = "false" ] && patch --batch --fuzz=5 -p1 -i "$WORK/pred.patch" >>"$LOG_DIR/patch_apply.log" 2>&1; then _patch_applied=true; fi
if [ "$_patch_applied" = "false" ]; then
    echo "CODE_PATCH_APPLY_FAILED: resolved=0"
    cat "$LOG_DIR/patch_apply.log"
    exit 1
fi

# 3. Re-initialise the project environment (verbatim from upstream REINIT_ENV_BASH).
_reinit_env() {
    if command -v conda >&- 2>&1; then
        eval "$(conda shell.bash hook 2>&-)" || true
        conda activate testbed 2>&- || true
    fi
    source activate testbed 2>&- || true
    export PYTHONPATH="${PYTHONPATH:-}"
    _py_importable=false
    if [ -f "setup.py" ] || [ -f "pyproject.toml" ] || [ -f "setup.cfg" ]; then
        _pkg_name=$(python -c "import setuptools; d=setuptools.find_packages('.'); print(d[0] if d else '')" 2>&- || true)
        if [ -n "$_pkg_name" ] && python -c "import ${_pkg_name}" 2>&-; then
            _py_importable=true
        else
            pip install -e . --no-build-isolation --no-deps 2>&- || \
            pip install -e . --no-deps 2>&- || \
            pip install -e . 2>&- || true
        fi
    fi
    if [ -f "pom.xml" ]; then mvn compile -DskipTests -q 2>&- || true; fi
    if [ -f "build.gradle" ] || [ -f "build.gradle.kts" ]; then
        ./gradlew compileJava 2>&- || gradle compileJava 2>&- || true
    fi
    if [ -f "Cargo.toml" ]; then cargo build 2>&- || true; fi
    if [ -f "Makefile" ]; then
        if [ "$_py_importable" = "true" ]; then
            :
        elif [ -f "go.mod" ]; then
            go build ./... 2>&- || true
        else
            make 2>&- || true
        fi
    fi
    if [ -f "package.json" ] && [ -f "tsconfig.json" ]; then
        npm run build 2>&- || npx tsc 2>&- || true
    fi
}
(_reinit_env) >"$LOG_DIR/reinit_env.log" 2>&1 || true

# 4. Run the instance eval script, retrying on HTTP 429 like upstream _eval_with_retry.
_attempt=0
while [ $_attempt -lt 5 ]; do
    (cd /testbed && bash /tests/eval_script.sh) > "$WORK/after.log" 2>&1 || true
    if ! grep -qiE "status code 429|\b429\b.*Too Many|Too Many Requests|Could not GET.*429" "$WORK/after.log" 2>/dev/null; then
        break
    fi
    _attempt=$((_attempt + 1))
    echo "429 detected (attempt $_attempt/5), retrying in $((_attempt * 30))s..."
    sleep $((_attempt * 30))
done
cp "$WORK/after.log" "$LOG_DIR/after.log"

# 5. Grade with the vendored upstream parser.
"$ACTIVE_SWE_HOME/python3" /tests/grade.py /tests/config.json "$WORK/after.log" "$LOG_DIR"
