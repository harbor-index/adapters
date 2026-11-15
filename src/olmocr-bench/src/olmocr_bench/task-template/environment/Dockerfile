# Python 3.11.15 on Debian bookworm, pinned by digest (multi-architecture index).
FROM python:3.11-slim-bookworm@sha256:e2d3af735aff6eeee600b1933bedd99da6645fedf572cc12ef4cc1331f2ceebe

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# PDF utilities (pdftoppm, pdftotext, pdfimages) for inspecting or rasterising the page.
# libatomic1 is needed by the Node.js driver bundled with Playwright.
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Pinned Python packages plus a headless Chromium, installed at build time so that
# nothing has to be downloaded after the container starts.
RUN pip install \
        playwright==1.65.1 \
        rapidfuzz==3.14.6 \
        fuzzysearch==0.8.1 \
        beautifulsoup4==4.15.0 \
        lxml==6.1.3 \
        tqdm==4.70.1 \
        attrs==26.1.0 \
        greenlet==3.5.6 \
        soupsieve==4.1 \
        typing-extensions==4.16.0 \
    && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

# The container's main process only idles, so stop it immediately instead of
# waiting out the default 10 s grace period on shutdown.
STOPSIGNAL SIGKILL

WORKDIR /workspace

# Everything above is identical for every task; only this layer differs.
COPY document.pdf /workspace/document.pdf
