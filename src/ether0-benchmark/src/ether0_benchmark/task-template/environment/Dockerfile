FROM python:3.11-slim

# Let apt run its download step as root. This keeps apt working in user-namespace
# container runtimes (e.g. rootless Podman with a single UID mapping), where apt's
# default switch to the unprivileged `_apt` user fails; it has no effect on scoring.
RUN echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99-sandbox-root

# Minimal agent workspace. ether0's reward code and its chemistry dependencies
# (RDKit etc.) are not pre-installed here; tests/test.sh installs them into an
# isolated virtualenv at verification time. git is needed for that install;
# libxrender1/libxext6 are system libraries RDKit's wheels load at import time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
