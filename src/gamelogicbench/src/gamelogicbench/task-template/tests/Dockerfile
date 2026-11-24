FROM __GLB_RUNTIME_IMAGE__

USER root
WORKDIR /tests
COPY test.sh verify.py controller_worker.gd controller_proxy.gd /tests/
COPY private/ /tests/private/
RUN chmod 0700 /tests && chmod 0500 /tests/test.sh \
    && command -v python3 && command -v setpriv && command -v godot
