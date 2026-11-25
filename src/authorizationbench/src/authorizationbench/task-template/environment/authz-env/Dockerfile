# AuthorizationBench task environment: upstream mock services + Oracle User +
# MCP gateway. The agent never sees this container's filesystem.
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Upstream code at the pinned commit (services, Oracle User, MCP gateway).
# Task bundles and generation artifacts are not needed here and are skipped.
ARG AUTHZ_REPO=https://github.com/PEACH-Research-Lab/AuthorizationBench.git
ARG AUTHZ_COMMIT=__AUTHZ_COMMIT__
RUN git init -q /opt/ab \
    && cd /opt/ab \
    && git remote add origin "$AUTHZ_REPO" \
    && git config core.sparseCheckout true \
    && printf '%s\n' '/authorization_eva_sandbox/*' \
        '!/authorization_eva_sandbox/tasks/' \
        '!/authorization_eva_sandbox/task_generation/' \
        '!/authorization_eva_sandbox/vignettes/' > .git/info/sparse-checkout \
    && git fetch -q --depth 1 --filter=blob:none origin "$AUTHZ_COMMIT" \
    && git checkout -q FETCH_HEAD \
    && rm -rf .git \
    && pip install --no-cache-dir -r /opt/ab/authorization_eva_sandbox/requirements-core.txt

ENV PYTHONPATH=/opt/ab:/opt/ab/authorization_eva_sandbox \
    PYTHONUNBUFFERED=1

COPY server.py collect.py healthcheck.py /opt/authz/
COPY bundle/ /opt/authz/bundle/
# Seeds the shared `workspace` volume on first mount.
COPY workspace/ /workspace/
RUN chmod -R go-rwx /opt/authz

EXPOSE 8000
HEALTHCHECK --interval=2s --timeout=5s --start-period=5s --retries=60 \
    CMD ["python3", "/opt/authz/healthcheck.py"]
CMD ["python3", "/opt/authz/server.py"]
