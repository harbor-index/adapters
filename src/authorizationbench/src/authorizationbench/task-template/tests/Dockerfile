# Separate verifier: upstream post-hoc detectors and LLM judges.
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

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
