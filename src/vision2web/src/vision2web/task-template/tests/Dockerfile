FROM {{RUNTIME_IMAGE}}
RUN pip install --no-cache-dir openai==3.54.0 Pillow==12.3.0 pyyaml==6.0.3 \
    && useradd --create-home --uid 1000 website
COPY . /tests
RUN chmod -R go-rwx /tests
WORKDIR /workspace
