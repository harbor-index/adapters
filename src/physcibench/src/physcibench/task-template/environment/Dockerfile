FROM python:3.12-slim

WORKDIR /workspace
RUN apt-get update && apt-get install -y --no-install-recommends git poppler-utils \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/physcibench-requirements.txt
RUN pip install --no-cache-dir -r /tmp/physcibench-requirements.txt
COPY files/ /workspace/files/
