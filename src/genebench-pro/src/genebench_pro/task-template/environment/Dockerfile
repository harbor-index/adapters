FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
COPY --from=ghcr.io/astral-sh/uv:0.8.14@sha256:f3660c56d5b08d6c516360981bedc439f499b9bf37f46a216018da3777a74011 /uv /usr/local/bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates git r-base-core r-cran-mass r-cran-matrix \
    r-cran-survival bedtools tabix \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt

RUN useradd --create-home --shell /bin/bash analyst \
    && install -d -o analyst -g analyst /workspace \
    && install -d -m 0700 /tests

WORKDIR /workspace
COPY LICENSE.dataset /usr/share/licenses/dataset/LICENSE
COPY --chown=analyst:analyst data_files/ /workspace/data_files/
