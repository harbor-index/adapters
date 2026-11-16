FROM $image
USER root
SHELL ["/bin/bash", "-c"]
RUN test "$$(cat /workspace/RoboDojo/.harbor-upstream-revision)" = "$upstream_revision"
# Docker exec does not inherit activation performed by the image entrypoint.
ENV PATH=/root/miniconda3/envs/RoboDojo/bin:/root/miniconda3/bin:$${PATH}
WORKDIR /workspace/RoboDojo
