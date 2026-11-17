# Agent container. It holds only the bridge client; the simulator, its assets
# and all grading output live in the separate simulator service.
FROM python:3.12-slim
COPY bridge_client.py /opt/robodojo/bridge_client.py
ENV ROBODOJO_BRIDGE_URL=http://simulator:19100
WORKDIR /workspace
