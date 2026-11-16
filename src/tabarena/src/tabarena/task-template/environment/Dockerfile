FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates && rm -rf /var/lib/apt/lists/*
# Modal domain allowlists require TLS; use HTTPS for harness package setup.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources
RUN pip install --no-cache-dir --pre numpy==2.2.6 pandas==2.2.3 scikit-learn==1.7.2 autogluon.core==1.6.4b20261109
WORKDIR /workspace
COPY data /workspace/data
RUN mkdir -p /workspace/submission
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
