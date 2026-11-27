# Agent environment for SWEeper-Bench case {case_id}.
# Base: the upstream *prediction* product image (dependencies and the
# `product-*` wrappers, no evaluation seed data). The upstream runner mounts a
# fresh checkout of the buggy commit at /workspace/product; here it is cloned at
# build time with the same flattened, history-free layout.
FROM {prediction_image}

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive

# Tools the upstream prediction agent container provides next to the product:
# git, curl, a TCP forwarder for 127.0.0.1:13200, Node {node_version} with the
# Playwright {playwright_version} CLI, and Chromium under /ms-playwright.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git procps socat xz-utils \
    && rm -rf /var/lib/apt/lists/*

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY sweeper/ /opt/sweeper/
RUN chmod 0755 /opt/sweeper/*.sh \
    && curl -fsSL "https://nodejs.org/dist/v{node_version}/node-v{node_version}-linux-x64.tar.xz" \
        | tar -xJ -C /opt \
    && mv "/opt/node-v{node_version}-linux-x64" /opt/sweeper-agent-node \
    && /opt/sweeper/install-browser.sh "{playwright_version}"

RUN /opt/sweeper/checkout.sh "{repo}" "{base_commit}" /workspace/product \
    && install -m 0755 /opt/sweeper/task-shell.sh /workspace/task-shell

WORKDIR /workspace/product
ENTRYPOINT ["/opt/sweeper/agent-entrypoint.sh"]
CMD ["sleep", "infinity"]
