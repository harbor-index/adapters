# Verifier environment, main service: a fresh copy of the upstream *evaluation*
# product image for case {case_id} with a clean checkout of the buggy commit.
# The agent never runs in this image. The browser verifier runs next to it as
# the `browser-verifier` service (see docker-compose.yaml).
FROM {evaluation_image}

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive

RUN missing=""; \
    for tool in git setsid pkill; do command -v "$tool" >/dev/null 2>&1 || missing=1; done; \
    if [ -n "$missing" ]; then \
      apt-get update && apt-get install -y --no-install-recommends ca-certificates git procps util-linux \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY sweeper/checkout.sh sweeper/image-setup.sh /opt/sweeper/
RUN chmod 0755 /opt/sweeper/checkout.sh /opt/sweeper/image-setup.sh \
    && /opt/sweeper/checkout.sh "{repo}" "{base_commit}" /workspace/product

COPY test.sh /tests/test.sh
COPY workflows.yaml /tests/workflows.yaml
RUN chmod 0755 /tests/test.sh

WORKDIR /workspace/product
# Some evaluation images set ENTRYPOINT ["sleep", "infinity"], which would turn
# Harbor's keep-alive command into extra `sleep` arguments. test.sh starts the
# product itself, so the container only has to stay up.
ENTRYPOINT []
CMD ["sleep", "infinity"]
