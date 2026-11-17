#!/bin/bash
# Verifier for one ether0-benchmark task.
# Installs ether0's reward code (pinned commit) with ether0's locked dependency
# versions into an isolated virtualenv, then scores /app/answer.txt.
# uv (pinned) performs the install because it is ~10x faster than pip for this
# dependency set; the installed package versions are identical.
#
# Optional: set ETHER0_VERIFIER_WHEELHOUSE (a directory or URL, passed through
# Harbor's verifier environment, e.g. `--ve ETHER0_VERIFIER_WHEELHOUSE=...`) to
# install the same pinned packages from a local wheelhouse instead of PyPI and
# GitHub. The wheelhouse must contain the wheels for requirements-verifier.txt,
# uv==${UV_VERSION}, and an ether0 wheel built from ETHER0_COMMIT.
set -uo pipefail

ETHER0_COMMIT="b77e0d8eb06f62c8528da72bff22d4c7fee11371"
UV_VERSION="0.9.5"
VENV=/tmp/ether0-verifier
PIP_FLAGS=(--isolated --quiet --no-cache-dir --disable-pip-version-check)
UV_FLAGS=(--no-config --no-cache --quiet --python "$VENV/bin/python")

mkdir -p /logs/verifier
echo 0 > /logs/verifier/reward.txt  # fail closed until scoring succeeds

if [ -n "${ETHER0_VERIFIER_WHEELHOUSE:-}" ]; then
    PIP_SOURCE=(--no-index --find-links "$ETHER0_VERIFIER_WHEELHOUSE")
    UV_SOURCE=(--no-index --find-links "$ETHER0_VERIFIER_WHEELHOUSE")
    case "$ETHER0_VERIFIER_WHEELHOUSE" in
        http://*)  # plain-HTTP links are ignored unless the host is trusted explicitly
            WHEELHOUSE_HOST="${ETHER0_VERIFIER_WHEELHOUSE#http://}"
            WHEELHOUSE_HOST="${WHEELHOUSE_HOST%%/*}"
            PIP_SOURCE+=(--trusted-host "${WHEELHOUSE_HOST%%:*}")
            UV_SOURCE+=(--allow-insecure-host "${WHEELHOUSE_HOST%%:*}")
            ;;
    esac
    ETHER0_SPEC="ether0"
else
    PIP_SOURCE=()
    UV_SOURCE=()
    ETHER0_SPEC="ether0 @ git+https://github.com/Future-House/ether0.git@${ETHER0_COMMIT}"
fi

python3 -I -m venv "$VENV" || exit 1
"$VENV/bin/pip" install "${PIP_FLAGS[@]}" "${PIP_SOURCE[@]}" "uv==${UV_VERSION}" || exit 1
"$VENV/bin/uv" pip install "${UV_FLAGS[@]}" "${UV_SOURCE[@]}" \
    -r /tests/requirements-verifier.txt || exit 1
"$VENV/bin/uv" pip install "${UV_FLAGS[@]}" "${UV_SOURCE[@]}" \
    --no-deps "$ETHER0_SPEC" || exit 1

"$VENV/bin/python" -I /tests/grade.py
