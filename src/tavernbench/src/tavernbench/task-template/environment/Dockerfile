FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive

# Python runs the verifier. The other packages are what command-line agents need
# to install and run, so that agent setup does not depend on apt at trial time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        nodejs \
        npm \
        procps \
    && rm -rf /var/lib/apt/lists/*

# The agent writes its answer to /app/answer.json.
WORKDIR /app
