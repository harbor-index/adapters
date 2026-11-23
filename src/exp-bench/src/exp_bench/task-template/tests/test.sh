#!/bin/bash
# EXP-Bench verifier. Judge dependencies (pinned as in upstream's environment.yml) go into
# a fresh venv, ignoring pip configuration, so nothing the agent installed affects the judge.
set -uo pipefail

mkdir -p /logs/verifier

PYTHON_BIN="$(command -v python3 || command -v python)"
JUDGE_VENV="$(mktemp -d /tmp/exp-bench-judge-XXXXXXXX)"
"$PYTHON_BIN" -I -m venv --clear "$JUDGE_VENV"

installed=0
for attempt in 1 2 3 4; do
    # tiktoken downloads its encodings on first use; fetch them inside the retry.
    if PIP_CONFIG_FILE=/dev/null "$JUDGE_VENV/bin/pip" install --isolated -q \
        --disable-pip-version-check --timeout 60 --retries 10 \
        "openai==1.66.3" "tiktoken==0.9.0" "langchain-text-splitters==0.3.6" \
        && "$JUDGE_VENV/bin/python" -I -c \
            "import tiktoken; [tiktoken.get_encoding(e) for e in ('o200k_base', 'cl100k_base')]"; then
        installed=1
        break
    fi
    echo "judge dependency install failed (attempt $attempt); retrying" >&2
    sleep $((attempt * 15))
done
if [ "$installed" != 1 ]; then
    echo "failed to install judge dependencies" >&2
    exit 1
fi

"$JUDGE_VENV/bin/python" -I /tests/judge.py
