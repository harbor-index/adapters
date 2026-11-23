FROM python:3.13-slim

WORKDIR /workspace

RUN pip install --no-cache-dir pypdf==7.1.1

COPY macbeth.txt /app/macbeth.txt
COPY macbeth.pdf /app/macbeth.pdf
COPY motifs.json /app/motifs.json
COPY queries.json /app/queries.json

RUN chmod -R a-w /app
