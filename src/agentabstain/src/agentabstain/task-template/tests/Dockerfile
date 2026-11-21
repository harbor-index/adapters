FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir pyyaml==6.0.3
RUN mkdir -p /logs/agent /logs/verifier /runtime_logs
COPY . /tests/
