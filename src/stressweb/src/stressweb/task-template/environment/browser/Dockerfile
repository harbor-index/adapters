FROM node:20.20.2-bookworm-slim AS website
WORKDIR /site/frontend
COPY site/frontend/package*.json ./
RUN npm ci --legacy-peer-deps --no-audit --no-fund
COPY site/frontend ./
WORKDIR /site/backend
COPY site/backend/package*.json ./
RUN npm ci --legacy-peer-deps --omit=dev --no-audit --no-fund
COPY site/backend ./

FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir playwright==1.65.2 pillow==13.0.0 \
    && python -m playwright install --with-deps --only-shell chromium
COPY --from=website /usr/local/bin/node /usr/local/bin/node
COPY --from=website /site/frontend /opt/site/frontend
COPY --from=website /site/backend /opt/site/backend
WORKDIR /opt/stressweb
COPY *.py query.json ./
CMD ["python", "-u", "service.py"]
