"""Shared evaluation utilities for dispatcher scripts.

Provides:
  - eval_with_retry_bash: generate bash code for 429-retry eval_script execution
  - is_synthetic_status / detect_build_failure: detect build/env failures from test logs
  - SANITIZE_GIT_BASH: bash snippet to hide original git history at runtime
  - REINIT_ENV_BASH: bash snippet to reinit project env after git checkout/apply
  - get_thinking_tokens: per-model thinking-token config
"""

import re


# =============================================================================
# Thinking tokens config (single source of truth)
# Claude model thinking configuration is passed through MAX_THINKING_TOKENS.
# Non-Claude models' reasoning is native to their APIs and not controlled here.
# =============================================================================
THINKING_TOKENS_MAP = {
    "claude-sonnet-4-6": 63000,
    "claude-opus-4-6": 63000,
    "claude-sonnet-4-5": 63000,
    "claude-opus-4-5": 63000,
}
THINKING_TOKENS_DEFAULT = 0  # safe default for unknown models


def get_thinking_tokens(model: str) -> int:
    """Look up thinking tokens for a model. Falls back to prefix match, then default."""
    if model in THINKING_TOKENS_MAP:
        return THINKING_TOKENS_MAP[model]
    for prefix, tokens in THINKING_TOKENS_MAP.items():
        if model.startswith(prefix):
            return tokens
    return THINKING_TOKENS_DEFAULT


# =============================================================================
# Runtime git sanitization
# =============================================================================
SANITIZE_GIT_BASH = r'''
_sanitize_git_repo() {
    if ! command -v git >&- 2>&1; then
        echo "WARNING: git not found; skipping runtime git sanitize" >&2
        return 0
    fi
    rm -rf .git
    git init -q
    git checkout -B main >&- 2>&1 || true
    git config user.email benchmark@example.com
    git config user.name benchmark
    git add -A
    git commit -q --allow-empty -m "benchmark base state"
    git remote remove origin >&- 2>&1 || true
    if [ -n "$(git status --short)" ]; then
        echo "ERROR: runtime git sanitize left a dirty worktree" >&2
        git status --short >&2
        return 2
    fi
}
'''


# =============================================================================
# Reinit env after git checkout / apply patch (single source of truth)
# =============================================================================
REINIT_ENV_BASH = r'''
_reinit_env() {
    if command -v conda >&- 2>&1; then
        eval "$(conda shell.bash hook 2>&-)" || true
        conda activate testbed 2>&- || true
    fi
    source activate testbed 2>&- || true
    export PYTHONPATH="${PYTHONPATH:-}"
    _py_importable=false
    # Python: skip pip install -e . if package already importable (avoids Cython/C recompilation)
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
    # Makefile: skip for Python projects already importable; for Go use go build instead of make
    if [ -f "Makefile" ]; then
        if [ "$_py_importable" = "true" ]; then
            : # skip Makefile (pandas Makefile triggers setup.py build_ext → Cython recompilation)
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
'''


# =============================================================================
# Eval With Retry (429 retry inside container)
# =============================================================================
def eval_with_retry_bash():
    """Generate a bash function '_eval_with_retry' for running eval_script with 429 retry.

    The function checks the runner's relative temporary directory first, then
    the relative output directory.
    It retries up to 5 times with exponential backoff (30s, 60s, 90s, 120s, 150s)
    when a 429 (Too Many Requests) response is detected in the output.

    Usage in container scripts:
        1. Include the function definition:  eval_with_retry_bash()
        2. Call it:  _eval_with_retry "$OUTPUT_DIR/after.log"
    """
    return (
        '_eval_with_retry() {\n'
        '    _log_path="$1"\n'
        '    _attempt=0\n'
        '    _temp_dir="${TEMP_DIR:-.active-swe-tmp}"\n'
        '    if [ -s "${_temp_dir}/eval_script.sh" ]; then\n'
        '        _eval_script="${_temp_dir}/eval_script.sh"\n'
        '    elif [ -s "${OUTPUT_DIR}/eval_script.sh" ]; then\n'
        '        _eval_script="${OUTPUT_DIR}/eval_script.sh"\n'
        '    else\n'
        '        echo "No eval_script found" > "$_log_path"\n'
        '        return\n'
        '    fi\n'
        '    while [ $_attempt -lt 5 ]; do\n'
        '        bash "$_eval_script" > "$_log_path" 2>&1 || true\n'
        '        if ! grep -qiE "status code 429|\\b429\\b.*Too Many|Too Many Requests|Could not GET.*429" "$_log_path" 2>&-; then\n'
        '            break\n'
        '        fi\n'
        '        _attempt=$((_attempt + 1))\n'
        '        echo "429 detected (attempt $_attempt/5), retrying in $((_attempt * 30))s..."\n'
        '        sleep $((_attempt * 30))\n'
        '    done\n'
        '}\n'
    )


# =============================================================================
# Build failure detection
# =============================================================================
_BUILD_FAIL_PATTERNS = [
    r'BUILD FAILURE',
    r'BUILD FAILED',
    r'compilation failed', r'Compilation failed',
    r'could not compile',
    r'\[ERROR\].*Cannot access.*in offline mode',
    r'\[ERROR\].*Missing.*artifact',
    r'429.*Too Many Requests',
    r'Could not resolve.*artifact',
    r'Could not GET.*status code 429',
]


def is_synthetic_status(status_map):
    """Check if status_map only contains synthetic entries like _maven_build_failure_."""
    if not status_map:
        return True
    return all(k.startswith("_") and k.endswith("_") for k in status_map.keys())


def detect_build_failure(log_text, status_map):
    """Detect build/compile failure from log text and parser status.

    Returns True if the test run failed due to a build/environment issue
    (not a code bug), which should trigger a retry.

    Key logic:
    1. If status_map has REAL test results (non-synthetic), the tests ran —
       this is NOT a build failure regardless of synthetic markers.
    2. If status_map is empty AND log is empty/missing, it's ambiguous —
       could be a timeout, script error, etc. NOT a build failure (retrying
       won't fix a missing script or wrong path).
    3. If _BUILD_FAIL_PATTERNS are found in the log AND no real results exist,
       this IS a build failure (Maven BUILD FAILURE, compilation error, etc.).
    4. If status_map only has synthetic entries but the log contains recognizable
       test output signals (failures, passes, test counts), the tests actually
       ran — treat as NOT a build failure.
    """
    real_results = {k: v for k, v in status_map.items()
                    if not (k.startswith("_") and k.endswith("_"))}

    # Rule 1: Real test results exist → tests ran, not a build failure
    if real_results:
        return False

    # Rule 2: No log content at all → cannot determine, not a build failure
    if not log_text or not log_text.strip():
        return False

    # Rule 3: Explicit build failure patterns in log → true build failure
    if any(re.search(p, log_text, re.IGNORECASE) for p in _BUILD_FAIL_PATTERNS):
        return True

    # Rule 4: Synthetic-only status but log has test output indicators —
    # the parser didn't fully recognize the format, but tests clearly ran.
    # Common signals: test counts, pass/fail keywords, test runner output
    _TEST_RAN_PATTERNS = [
        # Test count summaries (strong signal: tests actually ran)
        r'\d+\s+(?:test|tests|example|examples|spec|specs|scenario|scenarios)\s+(?:passed|failed|run)',
        r'Tests?\s+run\s*:',
        r'(?:Ran|ran)\s+\d+\s+tests?',
        r'\d+\s+passed,\s*\d+\s+failed',
        r'\d+\s+(?:failure|failures|error|errors)\b',
        r'Test Summary\s*:',
        r'Count of (?:all |)(?:run |)(?:unit |)tests?\s*:\s*\d',
        r'All tests passed',
        r'Failed examples:',
        # Test runner/framework output markers
        r'test\s+result\s*:',
        r'\[ok\]:|\[err\]:',
        r'✘|✔|✓|✗|⨉',
        r'(?:FAIL|FAILURES?)\s*[\(„]',
        # Shell exit markers from eval_script (strong signal: script completed execution)
        r'OMNIGRIL_EXIT_CODE',
        # Command-not-found (wrong PATH, not a build failure)
        r'command not found',
    ]
    if any(re.search(p, log_text, re.IGNORECASE) for p in _TEST_RAN_PATTERNS):
        return False

    return False
