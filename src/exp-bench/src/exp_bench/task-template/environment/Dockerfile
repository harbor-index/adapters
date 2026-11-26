# EXP-Bench task environment: the paper's code repository at a pinned commit, with the
# README and the task's source files masked, as upstream evaluation/eval.py:mask_repo does.
# Base image mirrors upstream ExpDockerfile_default (miniconda + build tools); this tag
# ships Python 3.12.9, the version pinned in upstream's environment.yml.
FROM continuumio/miniconda3:25.1.1-2

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget git gnupg lsb-release build-essential vim make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY prepare_workspace.sh /tmp/prepare_workspace.sh
RUN bash /tmp/prepare_workspace.sh && rm -f /tmp/prepare_workspace.sh

SHELL ["/bin/bash", "-c"]
CMD ["tail", "-f", "/dev/null"]
