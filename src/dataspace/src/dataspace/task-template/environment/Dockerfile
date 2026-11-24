FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        bubblewrap \
        coreutils \
        file \
        findutils \
        ffmpeg \
        jq \
        poppler-utils \
        ripgrep \
        socat \
        sqlite3 \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
        numpy==2.4.6 \
        opencv-python-headless==5.0.0.93 \
        pandas==3.0.3 \
        pdfplumber==0.11.10 \
        pillow==12.3.0 \
        pyarrow==25.0.0 \
        pymupdf==1.28.0 \
        pypdf==6.14.2 \
        pytesseract==0.3.13

RUN mkdir -p /opt/dataspace \
    && python -m pip freeze --all | sort > /opt/dataspace/python-packages.txt \
    && dpkg-query -W -f='${Package}\t${Version}\n' \
        | sort > /opt/dataspace/system-packages.txt

COPY TOOLS.md /opt/dataspace/TOOLS.md
COPY capabilities.json /opt/dataspace/capabilities.json

ENV DATASPACE_TOOLS_MANIFEST=/opt/dataspace/TOOLS.md
ENV OPENBLAS_NUM_THREADS=1
ENV OMP_NUM_THREADS=1
ENV MKL_NUM_THREADS=1
ENV NUMEXPR_NUM_THREADS=1

COPY workspace/ /mnt/workspace/
RUN chmod -R a-w /mnt/workspace && mkdir -p /mnt/output

WORKDIR /mnt/output
