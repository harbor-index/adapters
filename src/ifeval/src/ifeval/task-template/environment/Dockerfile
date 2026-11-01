FROM python:3.12-slim
WORKDIR /workspace
RUN apt-get update \
    && apt-get install -y --no-install-recommends tmux procps \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir langdetect==1.0.9 nltk==3.9.4 six>=1.16.0 \
    && python -m nltk.downloader -d /usr/local/share/nltk_data punkt punkt_tab
