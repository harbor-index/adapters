{{ENV_BUILDER_STAGE}}
COPY env-spec /tmp/agentswe-env-spec
RUN agentswe-build-env /tmp/agentswe-env-spec {{ENV_PREFIX}}

{{BASE_STAGE}}
# Builder: run-private writable copy of the task environment (upstream one_stop.prepare_private_environment).
COPY --from=envbuild {{ENV_PREFIX}} {{ENV_PREFIX}}
RUN {{ENV_ALIASES}}
COPY builder-package /builder-package
RUN chmod -R a-w /builder-package && mkdir -p /workspace/submission /run/agentswe-controller
COPY submit_dev_candidate.upstream /usr/local/libexec/agentswe/submit_dev_candidate
COPY submit_dev_candidate /usr/local/bin/submit_dev_candidate
RUN chmod 0755 /usr/local/libexec/agentswe/submit_dev_candidate /usr/local/bin/submit_dev_candidate
ENV AGENTSWE_RUNTIME_PREFIX={{DOCUMENTED_PREFIX}} \
    AGENTSWE_DEV_CONTROLLER_SOCKET=/run/agentswe-controller/dev-controller.sock
WORKDIR /workspace
