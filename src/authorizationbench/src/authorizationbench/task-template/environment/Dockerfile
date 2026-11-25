# Agent container. /workspace is a volume shared with the authz-env sidecar
# (seeded from the task bundle there), as upstream mounts one workspace copy
# into the agent container and exposes it to host-side services.
FROM python:3.12-slim-bookworm

# poppler-utils: upstream's agent image adds it so agents can read PDFs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq poppler-utils procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
__EXTRA_LAYERS__
