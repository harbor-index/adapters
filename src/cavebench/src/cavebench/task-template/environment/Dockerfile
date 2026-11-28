# Stage 1: download the pinned CaveBench test-frame archive for this apparatus
# and check its size and SHA-256. The stage is identical for every task of the
# same apparatus, so Docker builds it once and reuses it from the cache.
FROM python:3.13-slim-bookworm@sha256:f0fa218d5b06f9b9235024fa061cde2cb141f726de52134471053d2d9b5324a3 AS frames
COPY fetch_frames.py /opt/fetch_frames.py
RUN python3 /opt/fetch_frames.py {paper_scene} {archive_sha256} {archive_bytes}

# Stage 2: the task image. It holds only this question's frames F0..F{k};
# later frames of the episode are not copied.
FROM python:3.13-slim-bookworm@sha256:f0fa218d5b06f9b9235024fa061cde2cb141f726de52134471053d2d9b5324a3

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
{frame_copies}
COPY question.json /opt/cavebench/question.json
