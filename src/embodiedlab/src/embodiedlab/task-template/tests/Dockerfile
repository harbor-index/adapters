# Verifier: the EmbodiedLab offline evaluator (benchmark package) and this task's instance record.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ARG PIP_INDEX_URL=
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+-i $PIP_INDEX_URL} \
        numpy==1.26.4 scipy==1.15.3 pyyaml==6.0.3 jsonschema==4.26.0 pillow==12.3.0
COPY el/ /opt/el/
COPY score.py test.sh /tests/
# Harbor copies the collected artifacts to their source paths and reads /logs/verifier back; docker cp needs the
# parent directories to exist (the separate verifier gets no /logs mounts).
RUN mkdir -p /logs/verifier /logs/artifacts /opt/embodiedlab /home/agent
