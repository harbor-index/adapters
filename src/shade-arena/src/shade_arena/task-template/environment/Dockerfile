FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0
WORKDIR /workspace

COPY requirements.txt /tmp/shade-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/shade-requirements.txt

COPY upstream /opt/shade/upstream
COPY shade_runtime.py task.json /opt/shade/
COPY shade /usr/local/bin/shade
RUN chmod 755 /usr/local/bin/shade
