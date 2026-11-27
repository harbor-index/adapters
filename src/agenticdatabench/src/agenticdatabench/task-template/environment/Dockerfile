# AgenticDataBench task image. Follows upstream's agent image
# (testbed/da_agent/images/claude-code/Dockerfile @ e81dda8): Python 3.10 on
# Debian bullseye, the same system and Python packages, work dir /workspace.
# Left out: Node.js and the Claude Code CLI (Harbor installs each agent itself)
# and the non-root host-user account (only needed for upstream's bind mount).
FROM python:3.10-bullseye@sha256:65c6bdcca22db1a3468a0262ae05f38a0a718f51e9818cd09c12094aaae28a33

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Debian 11 (bullseye) packages now live only on archive.debian.org; the base
# image's apt sources still name deb.debian.org, where they return 404. Same
# packages, new mirror (upstream's Dockerfile fails at this step today).
RUN sed -i 's|http://deb.debian.org|http://archive.debian.org|g' /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev libssl-dev libffi-dev libxml2-dev libxslt1-dev zlib1g-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Upstream's package list, pinned to the versions current at its last commit
# (2026-10-13), since upstream installs them unpinned.
COPY requirements-agent.txt /tmp/requirements-agent.txt
RUN pip install -r /tmp/requirements-agent.txt && rm /tmp/requirements-agent.txt

# The grader's own environment (upstream grades outside the agent container,
# with testbed/requirements.txt). Used only by tests/test.sh, which Harbor runs
# in a separate container from this image.
COPY requirements-verifier.txt /tmp/requirements-verifier.txt
RUN python -m venv /opt/adb-verifier \
    && /opt/adb-verifier/bin/pip install -r /tmp/requirements-verifier.txt \
    && rm /tmp/requirements-verifier.txt

# The task's input data: the contents of datasets/{domain}/ at the pinned
# Hugging Face revision. Identical for every task of a domain, so Docker's
# layer cache keeps one copy per domain.
COPY fetch_data.py data_manifest.tsv /tmp/adb-data/
RUN python /tmp/adb-data/fetch_data.py /tmp/adb-data/data_manifest.tsv /workspace \
    && rm -rf /tmp/adb-data
{image_helper}
WORKDIR /workspace
