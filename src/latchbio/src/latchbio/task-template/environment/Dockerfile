# LatchBio evaluation environment.
#
# Base image: the agent container LatchBio publishes for its benchmarks
# (latch-eval-tools/agent_env/Dockerfile: Python 3.12 with scanpy, squidpy,
# scvi-tools, R, and common bioinformatics libraries preinstalled), pinned by
# digest. The image is linux/amd64 only, so the platform is fixed here to keep
# builds working on arm64 hosts (under emulation).
FROM --platform=linux/amd64 {agent_image}

# Interpreter reserved for the verifier, separate from the analysis environment
# the agent works in (/root/.venv). pydantic and anthropic are the only imports
# the latch-eval-tools graders need; versions match upstream's uv.lock.
RUN uv venv --python 3.12 /opt/latchbio-verifier \
    && uv pip install --no-cache --python /opt/latchbio-verifier/bin/python \
        "pydantic==2.12.5" \
        "anthropic==0.86.0"

# Input data for this evaluation, staged where the upstream harness mounts it.
{data_stage}

# LatchBio's graders: the PyPI release the adapter pins, accepted only with the
# recorded SHA-256 so that a different file fails the build. The verifier imports
# only its `graders` subpackage, so the agent-harness dependencies are not
# installed. tests/grade.py checks the installed modules against
# tests/graders.lock.json again before grading. Installed after the data so that
# moving to another grader release does not download the data again.
RUN printf '%s\n' "latch-eval-tools=={latch_eval_tools_version} --hash=sha256:{latch_eval_tools_wheel_sha256}" \
        > /tmp/latchbio-graders.txt \
    && uv pip install --no-cache --no-deps --only-binary :all: --require-hashes \
        --python /opt/latchbio-verifier/bin/python -r /tmp/latchbio-graders.txt \
    && rm /tmp/latchbio-graders.txt

# The upstream harness starts every agent in /workspace.
WORKDIR /workspace
