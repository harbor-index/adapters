# Toolchain image for one programming language.
#
# The first part of this file is the same for all six languages; exactly one
# language section follows it. Every task of a language therefore has a
# byte-identical build context, and the image is built once per language.
#
# Ubuntu 22.04 (jammy), pinned to the multi-platform image index of 2026-11-10.
FROM ubuntu:22.04@sha256:74b12b11ffa550fba1929ad4d8d16533c34f6612728469671afba1b03b66881c

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# CPython 3.9.13 runs the test driver in every image; 3.9.13 is the version the
# reference results were produced with. It is only put on PATH in the Python
# image, where it is also the interpreter the solutions run in.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) triple=x86_64-unknown-linux-gnu; sha256=ce1cfca2715e7e646dd618a8cb9baff93000e345ccc979b801fc6ccde7ce97df ;; \
      arm64) triple=aarch64-unknown-linux-gnu; sha256=80415aac1b96255b9211f6a4c300f31e9940c7e07a23d0dec12b53aa52c0d25e ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/python.tar.gz \
      "https://github.com/astral-sh/python-build-standalone/releases/download/20220802/cpython-3.9.13%2B20220802-${triple}-install_only.tar.gz"; \
    echo "${sha256}  /tmp/python.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/toolchain; \
    tar -xzf /tmp/python.tar.gz -C /opt/toolchain --no-same-owner; \
    rm /tmp/python.tar.gz; \
    ln -s python3 /opt/toolchain/python/bin/python; \
    /opt/toolchain/python/bin/python3 --version

# The grader compiles and runs the submitted code as this unprivileged account.
RUN groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin sandbox

WORKDIR /app

# The container's main process is a `sleep` that ignores SIGTERM as PID 1.
# Stopping with SIGKILL avoids a 10 second wait whenever a container is torn
# down; nothing in it needs a graceful shutdown.
STOPSIGNAL SIGKILL
