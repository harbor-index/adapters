# Verifier image. The verifier runs in its own container
# (environment_mode = "separate"); its only input from the agent is the
# collected /app/output artifact. Grading code and data are baked in here.
FROM python:3.13-slim

RUN pip install --no-cache-dir lxml==6.1.0

COPY . /tests/
RUN chmod +x /tests/test.sh && rm -f /tests/Dockerfile
