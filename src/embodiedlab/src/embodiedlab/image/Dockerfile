# The image shared by every EmbodiedLab Harbor task (one container per trial, the GPU in that container):
#   - the lab image: Isaac Sim with the EmbodiedLab environment runtime and benches (root only at run time)
#   - an unprivileged user `agent` with its own Python (numpy, scipy, pillow, opencv); the agent runs as this user
#   - /opt/el-harbor: the start script, the public-tool gateway, the workspace renderer and the agent client, plus the
#     instance record and settings of every generated task under tasks/ (readable by root only)
# Build it with scripts/build_image.sh after generating the tasks.
ARG LAB_IMAGE=embodiedlab/lab:0.2.1
FROM ${LAB_IMAGE}
USER root
ARG PIP_INDEX_URL=
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|http://archive.ubuntu.com/ubuntu|$APT_MIRROR|g; s|http://security.ubuntu.com/ubuntu|$APT_MIRROR|g" \
        /etc/apt/sources.list.d/ubuntu.sources; fi \
 && apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/agent-python \
 && /opt/agent-python/bin/pip install --no-cache-dir ${PIP_INDEX_URL:+-i $PIP_INDEX_URL} \
        numpy==1.26.4 scipy==1.15.3 pillow==12.3.0 opencv-python-headless==4.10.0.84 \
 && useradd -m -u 1001 -s /bin/bash agent
COPY el-harbor/ /opt/el-harbor/
RUN chmod -R go-rwx /opt/el-harbor/tasks \
 && chmod 755 /opt/el-harbor/up.sh \
 && chmod 700 /var/lib/embodiedlab \
 && install -d -o agent -g agent /home/agent/workspace
ENV PATH=/opt/agent-python/bin:$PATH \
    NVIDIA_DRIVER_CAPABILITIES=all
HEALTHCHECK NONE
WORKDIR /home/agent
ENTRYPOINT []
CMD ["sleep", "infinity"]
