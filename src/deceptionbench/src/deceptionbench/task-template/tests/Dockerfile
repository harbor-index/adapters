FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
RUN pip install --no-cache-dir openai==2.54.0
WORKDIR /workspace
COPY . /tests
