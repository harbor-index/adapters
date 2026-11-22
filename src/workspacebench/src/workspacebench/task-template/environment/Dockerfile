# Workspace-Bench task image. Everything above the workspace step is the same
# for every task, and the workspace step is the same for every task of one
# persona and language, so Docker's layer cache keeps it to 10 workspace layers.
#
# Tooling follows upstream evaluation/docker/Dockerfile (Workspace-Bench @
# 3fbd0f1), minus the Codex / OpenClaw / DeepAgents / DeepSeek harnesses.
FROM node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54

ENV DEBIAN_FRONTEND=noninteractive
ENV NODE_PATH=/opt/workspace-bench/office-runtime/node_modules

RUN apt-get update && apt-get install -y --no-install-recommends \
    antiword bash ca-certificates curl fonts-noto-cjk fonts-noto-core ghostscript git graphviz \
    libreoffice-calc libreoffice-common libreoffice-core libreoffice-impress libreoffice-writer \
    pandoc pdftk-java poppler-utils python3 python3-pip python3-venv python-is-python3 qpdf \
    tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng unzip wget zip build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pip-constraints.txt /tmp/pip-constraints.txt
RUN python3 -m pip install --no-cache-dir --break-system-packages -c /tmp/pip-constraints.txt \
    defusedxml lxml "markitdown[pptx]" numpy openpyxl pandas pdf2image pdfplumber pypdf pypdfium2 \
    Pillow python-docx python-pptx pytesseract reportlab tqdm pyyaml uv remotezip==0.12.6

COPY office-runtime.package.json /opt/workspace-bench/office-runtime/package.json
COPY office-runtime.package-lock.json /opt/workspace-bench/office-runtime/package-lock.json
RUN npm ci --prefix /opt/workspace-bench/office-runtime --omit=dev

# Upstream evaluation code at a pinned commit: the output collector and the
# agent-as-a-judge used by the verifier, with its Claude Agent SDK.
# judge-copy-view.patch: the judge view copies inputs/outputs instead of
# symlinking them (the same patch is applied on the original side for parity).
# judge-subscription-auth.patch: optional Claude subscription auth for the judge
# (used only when the key is "__SUBSCRIPTION__"; the API-key path is unchanged).
COPY judge-copy-view.patch /tmp/judge-copy-view.patch
COPY judge-subscription-auth.patch /tmp/judge-subscription-auth.patch
ARG WSB_COMMIT=3fbd0f1a136720fece86786545983e26642c3db2
RUN git init -q /opt/wsb \
    && git -C /opt/wsb remote add origin https://github.com/OpenDataBox/Workspace-Bench.git \
    && git -C /opt/wsb sparse-checkout set evaluation/src evaluation/baselines evaluation/skills \
       evaluation/package.json evaluation/package-lock.json \
    && git -C /opt/wsb fetch -q --depth 1 origin "$WSB_COMMIT" \
    && git -C /opt/wsb checkout -q FETCH_HEAD \
    && git -C /opt/wsb apply /tmp/judge-copy-view.patch \
    && git -C /opt/wsb apply /tmp/judge-subscription-auth.patch \
    && npm ci --prefix /opt/wsb/evaluation --omit=dev \
    && rm -rf /opt/wsb/.git

# Upstream's office skill pack, split per tool exactly as upstream's image does
# (evaluation/docker/Dockerfile @ 3fbd0f1, the two loops after COPY skills/office):
# - Claude Code gets docx, pdf, pptx, xlsx: the agent via ~/.claude/skills (Harbor's
#   claude-code agent copies it into its config dir) and the judge via its own
#   config dir.
# - Codex gets docx, pdfs, slides, spreadsheets in ~/.agents/skills, where Harbor's
#   codex agent registers skills.
RUN mkdir -p /root/.claude/skills /opt/wsb/judge-claude/skills /root/.agents/skills \
    && for s in docx pdf pptx xlsx; do \
         cp -r "/opt/wsb/evaluation/skills/office/$s" /root/.claude/skills/ \
         && cp -r "/opt/wsb/evaluation/skills/office/$s" /opt/wsb/judge-claude/skills/; \
       done \
    && for s in docx pdfs slides spreadsheets; do \
         cp -r "/opt/wsb/evaluation/skills/office/$s" /root/.agents/skills/; \
       done

# Upstream's output collector, run by the task's [[verifier.collect]] hook.
COPY collect_outputs.py /opt/wsb/collect_outputs.py

# The persona workspace, unpacked into /app from the pinned Hugging Face zip.
COPY fetch_workspace.py /opt/wsb/fetch_workspace.py
RUN python3 /opt/wsb/fetch_workspace.py --language {language} --persona {persona_dir} --dest /app \
    && mkdir -p /app/model_output

WORKDIR /app
