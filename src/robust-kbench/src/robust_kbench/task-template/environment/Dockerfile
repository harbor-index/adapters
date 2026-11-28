FROM nvcr.io/nvidia/pytorch:25.01-py3

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0;12.0+PTX"
ENV TORCH_EXTENSIONS_DIR="/tmp/torch_extensions"
ENV CUDA_HOME="/usr/local/cuda"
ENV PATH="${CUDA_HOME}/bin:${PATH}"

RUN pip install --no-cache-dir \
    ninja \
    dotmap \
    pycuda \
    pytest

# Disable TF32 to preserve full FP32 precision for numerical parity checks
ENV NVIDIA_TF32_OVERRIDE=0

WORKDIR /app
