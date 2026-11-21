# Agent container: the taxpayer's documents under /app/input, answer goes to
# /app/output/return.txt. poppler-utils and pypdf let CLI agents read PDFs.
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pypdf==6.1.1

WORKDIR /app
COPY input/ /app/input/
RUN mkdir -p /app/output
