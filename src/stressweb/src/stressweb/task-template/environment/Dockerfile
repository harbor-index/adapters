FROM python:3.11-slim-bookworm
RUN pip install --no-cache-dir pillow==13.0.0
COPY browser_cli.py /usr/local/bin/browser
RUN chmod 755 /usr/local/bin/browser && useradd -m -s /bin/bash agent && mkdir /app && chown agent:agent /app
WORKDIR /app
USER agent
