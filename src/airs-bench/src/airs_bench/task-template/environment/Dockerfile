FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir datasets==3.6.0 "numpy<2" "pyarrow<18" "pandas<2.2" scipy scikit-learn \
        torchmetrics tqdm transformers matplotlib pyyaml \
        rouge_score evaluate sktime nltk absl-py

WORKDIR /workspace

# Build the agent-visible data with the ORIGINAL prepare.py, then drop raw data
# (which contains test labels) so only train/test-inputs remain in the image.
COPY build /opt/airs_build
RUN python /opt/airs_build/download_data.py --out /opt/airs_raw \
 && mkdir -p /workspace/data \
 && cd /opt/airs_build \
 && python prepare.py \
        --global-shared-data-dir /opt/airs_raw \
        --agent-data-mount-dir /workspace/data \
 && rm -rf /opt/airs_raw /opt/airs_build /root/.cache/huggingface
