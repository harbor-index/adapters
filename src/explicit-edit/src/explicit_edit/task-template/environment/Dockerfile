FROM ubuntu:24.04

# python3 is required by the verifier (tests/verify.py); curl and ca-certificates
# let Harbor install agent CLIs. No other tooling is added on purpose.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# The agent's working directory, matching the original benchmark's sandbox.
WORKDIR /workspace

# Initial workspace files for this task (byte-exact upstream fixture input).
COPY workspace/ /workspace/
