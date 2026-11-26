FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    CHROME_PATH=/usr/bin/chromium

# Headless Chromium (Debian package: available for both amd64 and arm64) plus fonts so
# non-Latin pages render legibly in screenshots.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        fonts-liberation fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# WebJudge dependencies, pinned to upstream requirements.txt, in their own venv.
RUN python3 -m venv /opt/webjudge \
    && /opt/webjudge/bin/pip install --no-cache-dir \
        backoff==2.2.1 openai==1.68.2 Pillow==11.1.0

WORKDIR /app
RUN mkdir -p /app/output

# Task metadata (task_id, confirmed_task, website, reference_length). Last layer so the
# image layers above are shared across all tasks.
COPY task.json /app/task.json
