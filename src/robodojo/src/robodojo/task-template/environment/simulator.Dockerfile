FROM $image
USER root
SHELL ["/bin/bash", "-c"]
RUN test "$$(cat /workspace/RoboDojo/.harbor-upstream-revision)" = "$upstream_revision"
COPY simulator /opt/harbor-robodojo
ENV HARBOR_ROBODOJO_HOME=/opt/harbor-robodojo
ENV PYTHONPATH=/opt/harbor-robodojo:/workspace/RoboDojo
WORKDIR /workspace/RoboDojo
# The image entrypoint activates the RoboDojo conda environment first.
CMD ["python", "-m", "robodojo.direct.supervisor"]
