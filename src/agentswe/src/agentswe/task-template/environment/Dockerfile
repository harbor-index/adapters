{{ENV_BUILD_STAGES}}

{{BASE_STAGE}}
# Builder: run-private writable copy of the task environment (upstream one_stop.prepare_private_environment),
# reachable at every path where upstream mounts it (one_stop.builder_compose + runtime_contract.prepare_compose).
COPY --from=envbuild {{ENV_PREFIX}} {{ENV_PREFIX}}
RUN {{ENV_LAYOUT}}
COPY builder-package /builder-package
RUN chmod -R a-w /builder-package && mkdir -p /workspace/submission /run/agentswe-controller /var/agentswe-env
COPY submit_dev_candidate.upstream /usr/local/libexec/agentswe/submit_dev_candidate
COPY submit_dev_candidate /usr/local/bin/submit_dev_candidate
RUN chmod 0755 /usr/local/libexec/agentswe/submit_dev_candidate /usr/local/bin/submit_dev_candidate
ENV {{BUILDER_ENV}}
WORKDIR /workspace
