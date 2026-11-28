{{ENV_BUILD_STAGES}}

{{EXTRA_STAGES}}
{{BASE_STAGE}}
# Trusted runtime: the pristine task environment, never visible to the Builder.
COPY --from=envbuild {{ENV_PREFIX}} /opt/agentswe-trusted-runtime
{{EXTRA_FINAL}}
RUN useradd --uid 10001 --create-home agentswe-candidate
COPY upstream {{UPSTREAM_ROOT}}
COPY agentswe_harbor /opt/agentswe-harbor/agentswe_harbor
COPY bin /opt/agentswe-harbor/bin
# Rubrics, evaluator assets and staged jobs stay unreadable to Candidates, which run unprivileged.
RUN chmod 0700 {{UPSTREAM_ROOT}} && chmod 0755 /opt/agentswe-harbor/bin/harbor /opt/agentswe-harbor/bin/docker
ENV PYTHONPATH=/opt/agentswe-harbor \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENTSWE_HARBOR_ROOT=/opt/agentswe-harbor \
    AGENTSWE_TASK_DIR={{TASK_DIR}} \
    AGENTSWE_CREATION_SHARED={{UPSTREAM_ROOT}}/runners/creation \
    AGENTSWE_PROVIDER_BROKER_DIR={{UPSTREAM_ROOT}}/broker \
    AGENTSWE_CANDIDATE_ENV_PREFIX=/var/agentswe/builder-env
RUN mkdir -p /var/agentswe /workspace
WORKDIR /var/agentswe
CMD ["python3", "-m", "agentswe_harbor.devserver"]
