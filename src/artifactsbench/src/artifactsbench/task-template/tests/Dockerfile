FROM mcr.microsoft.com/playwright/python:v1.55.0-noble@sha256:640d578aae63cfb632461d1b0aecb01414e4e020864ac3dd45a868dc0eff3078

RUN python -m pip install --no-cache-dir playwright==1.55.0 transformers==4.55.4 requests==2.32.5
ARG UPSTREAM_REV=88c968b87e150e63de7660937e6dcfb8e7d643cf
ARG TOKENIZER_REV=a09a35458c702b33eeacc393d103063234e8bc28
WORKDIR /opt/artifactsbench
RUN mkdir -p src/prompts tokenizer && \
    for path in LICENSE src/code_parser.py src/extract_ans.py src/utils.py src/prompts/prompt_mllm_check.py; do \
        curl --retry 3 --fail --silent --show-error --location \
            "https://raw.githubusercontent.com/Tencent-Hunyuan/ArtifactsBenchmark/${UPSTREAM_REV}/${path}" -o "${path}" || exit 1; \
    done && \
    for path in LICENSE config.json merges.txt tokenizer.json tokenizer_config.json vocab.json; do \
        curl --retry 3 --fail --silent --show-error --location \
            "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct/resolve/${TOKENIZER_REV}/${path}" -o "tokenizer/${path}" || exit 1; \
    done && \
    python -c 'from transformers import AutoTokenizer; AutoTokenizer.from_pretrained("tokenizer", local_files_only=True)' && \
    PYTHONPATH=src python -c 'import utils; import prompts.prompt_mllm_check' && id pwuser
COPY . /tests/
