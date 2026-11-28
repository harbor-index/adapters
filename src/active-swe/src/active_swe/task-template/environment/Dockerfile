# Active-SWE instance image (prebuilt upstream, linux/amd64 only).
FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:0.8.17 AS uv

FROM --platform=linux/amd64 {docker_image}

WORKDIR /testbed

# Standalone Python for the verifier's log parser, kept off PATH so the
# agent's environment matches upstream. Language images (Go, Rust, Java, ...)
# do not ship Python.
COPY --from=uv /uv /opt/active-swe/uv
RUN UV_PYTHON_INSTALL_DIR=/opt/active-swe/pythons /opt/active-swe/uv python install 3.12 \
    && ln -s "$(UV_PYTHON_INSTALL_DIR=/opt/active-swe/pythons /opt/active-swe/uv python find 3.12)" /opt/active-swe/python3 \
    && /opt/active-swe/python3 --version

# Upstream sanitizes git at container start ("benchmark base state"): the
# original history is replaced by one clean commit so the agent cannot read
# the fix from the log. Done at build time here; the base commit is recorded
# so the verifier can restore it.
RUN git config --global --add safe.directory /testbed \
    && rm -rf .git \
    && git init -q \
    && (git checkout -B main >/dev/null 2>&1 || true) \
    && git config user.email benchmark@example.com \
    && git config user.name benchmark \
    && git add -A \
    && git commit -q --allow-empty -m "benchmark base state" \
    && test -z "$(git status --short)" \
    && git rev-parse HEAD > /opt/active-swe/base_commit

RUN mkdir -p /logs
