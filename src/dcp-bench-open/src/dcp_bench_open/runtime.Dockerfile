ARG DOCKER_BINARY_IMAGE=docker:28.2.2-dind
FROM ${DOCKER_BINARY_IMAGE} AS docker-binaries
FROM dcp-eval/cpmpy_python:v1
COPY --from=docker-binaries /usr/local/bin/docker /usr/local/bin/dockerd /usr/local/bin/containerd /usr/local/bin/containerd-shim-runc-v2 /usr/local/bin/ctr /usr/local/bin/runc /usr/local/bin/docker-proxy /usr/local/bin/dind /usr/local/bin/
COPY solver-image.tar /tests/solver-image.tar
ENTRYPOINT []
