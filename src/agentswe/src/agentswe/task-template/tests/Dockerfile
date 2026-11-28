{{ENV_BUILD_STAGES}}

{{EXTRA_STAGES}}
{{BASE_STAGE}}
# Separate verifier: trusted runtime, held-out cases and evaluator assets. The frozen
# delivery and the Builder's private environment arrive as Harbor artifacts.
COPY --from=envbuild {{ENV_PREFIX}} /opt/agentswe-trusted-runtime
{{EXTRA_FINAL}}
RUN useradd --uid 10001 --create-home agentswe-candidate
COPY upstream {{UPSTREAM_ROOT}}
COPY agentswe_harbor /opt/agentswe-harbor/agentswe_harbor
ENV PYTHONPATH=/opt/agentswe-harbor \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENTSWE_TASK_DIR={{TASK_DIR}} \
    AGENTSWE_CREATION_SHARED={{UPSTREAM_ROOT}}/runners/creation \
    AGENTSWE_PROVIDER_BROKER_DIR={{UPSTREAM_ROOT}}/broker \
    AGENTSWE_CANDIDATE_ENV_PREFIX=/var/agentswe/builder-root{{ENV_PREFIX}}
RUN mkdir -p /var/agentswe /workspace
# A bundled separate verifier owns /tests: Harbor does not upload tests/ into it.
COPY test.sh /tests/test.sh
RUN chmod 0755 /tests/test.sh
WORKDIR /var/agentswe
